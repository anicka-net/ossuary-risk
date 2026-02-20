"""Batch scoring service for ossuary.

Scores packages from discovery JSON or custom YAML seed files
with concurrency control, resume support, and progress tracking.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ossuary.services.cache import ScoreCache
from ossuary.services.scorer import score_package, ScoringResult
from ossuary.db.session import session_scope

logger = logging.getLogger(__name__)


@dataclass
class BatchResult:
    """Summary of a batch scoring run."""

    total: int = 0
    scored: int = 0
    skipped: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)


@dataclass
class PackageEntry:
    """A package entry for batch scoring."""

    obs_package: str
    github_owner: str
    github_repo: str
    repo_url: str
    source: str  # "service", "spec", or "custom"
    obs_project: str = ""
    ecosystem: str = "github"  # "github", "npm", "pypi"


def load_discovery_file(path: str) -> list[PackageEntry]:
    """Load packages from a discovery JSON file."""
    with open(path) as f:
        data = json.load(f)

    return [
        PackageEntry(
            obs_package=item["obs_package"],
            github_owner=item["github_owner"],
            github_repo=item["github_repo"],
            repo_url=item["repo_url"],
            source=item["source"],
            obs_project=item.get("obs_project", ""),
        )
        for item in data
    ]


_GITHUB_URL_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")


def load_custom_seed(path: str) -> list[PackageEntry]:
    """Load packages from a custom YAML seed file.

    Expected format:
        packages:
          - name: owner/repo
            repo: https://github.com/owner/repo
          - name: numpy
            ecosystem: pypi

    Rules:
      - name is required
      - For github ecosystem: repo URL required, must be valid github.com URL
      - ecosystem defaults to github when repo is a github.com URL
      - For npm/pypi: repo is optional (collectors discover it)
    """
    import yaml

    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "packages" not in data:
        raise ValueError(f"Invalid seed file: expected top-level 'packages' key in {path}")

    raw_packages = data["packages"]
    if not isinstance(raw_packages, list):
        raise ValueError(f"Invalid seed file: 'packages' must be a list in {path}")

    entries = []
    seen = set()

    for i, item in enumerate(raw_packages):
        if not isinstance(item, dict):
            raise ValueError(f"Entry {i + 1}: expected a mapping, got {type(item).__name__}")

        name = item.get("name", "").strip()
        if not name:
            raise ValueError(f"Entry {i + 1}: 'name' is required")

        repo_url = item.get("repo", "").strip()
        ecosystem = item.get("ecosystem", "").strip().lower()

        # Infer ecosystem from repo URL if not specified
        if not ecosystem:
            if repo_url and _GITHUB_URL_RE.match(repo_url):
                ecosystem = "github"
            elif not repo_url:
                raise ValueError(
                    f"Entry {i + 1} ({name}): must specify 'ecosystem' or provide a GitHub 'repo' URL"
                )
            else:
                raise ValueError(
                    f"Entry {i + 1} ({name}): non-GitHub repo URL requires explicit 'ecosystem'"
                )

        supported = {"github", "npm", "pypi", "cargo", "rubygems", "packagist", "nuget", "go"}
        if ecosystem not in supported:
            raise ValueError(f"Entry {i + 1} ({name}): unsupported ecosystem '{ecosystem}'")

        # GitHub entries need a valid repo URL
        if ecosystem == "github":
            if not repo_url:
                raise ValueError(f"Entry {i + 1} ({name}): GitHub packages require a 'repo' URL")
            m = _GITHUB_URL_RE.match(repo_url)
            if not m:
                raise ValueError(
                    f"Entry {i + 1} ({name}): invalid GitHub URL '{repo_url}' "
                    f"(expected https://github.com/owner/repo)"
                )
            owner, repo = m.group(1), m.group(2)
            pkg_name = f"{owner}/{repo}"
        else:
            owner, repo = "", ""
            pkg_name = name

        # Deduplicate
        key = (pkg_name, ecosystem)
        if key in seen:
            raise ValueError(f"Entry {i + 1}: duplicate package {pkg_name} ({ecosystem})")
        seen.add(key)

        entries.append(
            PackageEntry(
                obs_package=name,
                github_owner=owner,
                github_repo=repo,
                repo_url=repo_url,
                source="custom",
                ecosystem=ecosystem,
            )
        )

    return entries


# ---------------------------------------------------------------------------
# Dependency file parsers for `ossuary scan`
# ---------------------------------------------------------------------------

@dataclass
class ParsedPackage:
    """A package extracted from a dependency file."""
    name: str
    is_dev: bool = False


def _parse_requirements_txt(path: str) -> list[ParsedPackage]:
    """Parse requirements.txt / constraints.txt."""
    packages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Strip version specifiers: requests>=2.28,<3 → requests
            name = re.split(r"[><=!~;@\[]", line)[0].strip()
            if name:
                packages.append(ParsedPackage(name=name))
    return packages


def _parse_package_json(path: str) -> list[ParsedPackage]:
    """Parse package.json for npm dependencies."""
    with open(path) as f:
        data = json.load(f)
    packages = []
    for name in data.get("dependencies", {}):
        packages.append(ParsedPackage(name=name, is_dev=False))
    for name in data.get("devDependencies", {}):
        packages.append(ParsedPackage(name=name, is_dev=True))
    return packages


def _parse_cargo_toml(path: str) -> list[ParsedPackage]:
    """Parse Cargo.toml for Rust crate dependencies."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as f:
        data = tomllib.load(f)
    packages = []
    for name in data.get("dependencies", {}):
        packages.append(ParsedPackage(name=name, is_dev=False))
    for name in data.get("dev-dependencies", {}):
        packages.append(ParsedPackage(name=name, is_dev=True))
    for name in data.get("build-dependencies", {}):
        packages.append(ParsedPackage(name=name, is_dev=True))
    return packages


def _parse_go_mod(path: str) -> list[ParsedPackage]:
    """Parse go.mod for Go module dependencies."""
    packages = []
    in_require = False
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("require ("):
                in_require = True
                continue
            if in_require and line == ")":
                in_require = False
                continue
            if in_require:
                # e.g. "github.com/gin-gonic/gin v1.9.1"
                parts = line.split()
                if parts and not parts[0].startswith("//"):
                    packages.append(ParsedPackage(name=parts[0]))
            elif line.startswith("require "):
                # Single-line require: "require github.com/foo/bar v1.0.0"
                parts = line.split()
                if len(parts) >= 2:
                    packages.append(ParsedPackage(name=parts[1]))
    return packages


def _parse_gemfile(path: str) -> list[ParsedPackage]:
    """Parse Gemfile for Ruby gem dependencies."""
    packages = []
    gem_re = re.compile(r"""^\s*gem\s+['"]([^'"]+)['"]""")
    # Dev groups
    in_dev_group = False
    group_re = re.compile(r"""^\s*group\s+.*:(?:development|test)""")
    with open(path) as f:
        for line in f:
            if group_re.search(line):
                in_dev_group = True
            elif line.strip() == "end":
                in_dev_group = False
            m = gem_re.match(line)
            if m:
                packages.append(ParsedPackage(name=m.group(1), is_dev=in_dev_group))
    return packages


def _parse_composer_json(path: str) -> list[ParsedPackage]:
    """Parse composer.json for PHP Packagist dependencies."""
    with open(path) as f:
        data = json.load(f)
    packages = []
    for name in data.get("require", {}):
        # Skip PHP itself and extensions
        if name == "php" or name.startswith("ext-"):
            continue
        packages.append(ParsedPackage(name=name, is_dev=False))
    for name in data.get("require-dev", {}):
        if name == "php" or name.startswith("ext-"):
            continue
        packages.append(ParsedPackage(name=name, is_dev=True))
    return packages


def _parse_csproj(path: str) -> list[ParsedPackage]:
    """Parse .csproj or packages.config for NuGet dependencies."""
    import xml.etree.ElementTree as ET
    tree = ET.parse(path)
    root = tree.getroot()
    packages = []
    # .csproj format: <PackageReference Include="Newtonsoft.Json" Version="13.0.1" />
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "PackageReference":
            name = elem.get("Include")
            if name:
                packages.append(ParsedPackage(name=name))
        # packages.config format: <package id="Newtonsoft.Json" version="13.0.1" />
        elif tag == "package":
            name = elem.get("id")
            if name:
                packages.append(ParsedPackage(name=name))
    return packages


def _parse_pyproject_toml(path: str) -> list[ParsedPackage]:
    """Parse pyproject.toml for Python dependencies."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as f:
        data = tomllib.load(f)
    packages = []
    project = data.get("project", {})
    for dep in project.get("dependencies", []):
        name = re.split(r"[><=!~;\[@ ]", dep)[0].strip()
        if name:
            packages.append(ParsedPackage(name=name, is_dev=False))
    for group_deps in project.get("optional-dependencies", {}).values():
        for dep in group_deps:
            name = re.split(r"[><=!~;\[@ ]", dep)[0].strip()
            if name:
                packages.append(ParsedPackage(name=name, is_dev=True))
    return packages


# Map filename patterns to (ecosystem, parser)
_FILE_PARSERS: dict[str, tuple[str, callable]] = {
    "requirements.txt": ("pypi", _parse_requirements_txt),
    "constraints.txt": ("pypi", _parse_requirements_txt),
    "package.json": ("npm", _parse_package_json),
    "pyproject.toml": ("pypi", _parse_pyproject_toml),
    "cargo.toml": ("cargo", _parse_cargo_toml),
    "go.mod": ("go", _parse_go_mod),
    "gemfile": ("rubygems", _parse_gemfile),
    "composer.json": ("packagist", _parse_composer_json),
}


def parse_dependency_file(
    path: str,
    ecosystem_override: Optional[str] = None,
    include_dev: bool = True,
) -> tuple[str, list[PackageEntry]]:
    """Parse a dependency file and return (ecosystem, package_entries).

    Detects file type from filename. Use ecosystem_override for
    non-standard filenames (e.g. 'deps.txt' with -e pypi).
    """
    filename = Path(path).name.lower()

    # Detect parser
    parser_fn = None
    ecosystem = ecosystem_override

    if ecosystem_override:
        # Use override ecosystem with matching parser
        eco_to_parser = {
            "pypi": _parse_requirements_txt,
            "npm": _parse_package_json,
            "cargo": _parse_cargo_toml,
            "go": _parse_go_mod,
            "rubygems": _parse_gemfile,
            "packagist": _parse_composer_json,
            "nuget": _parse_csproj,
        }
        parser_fn = eco_to_parser.get(ecosystem_override)
        if not parser_fn:
            raise ValueError(f"No parser for ecosystem '{ecosystem_override}'")
    else:
        # Auto-detect from filename
        if filename in _FILE_PARSERS:
            ecosystem, parser_fn = _FILE_PARSERS[filename]
        elif filename.endswith(".txt") and "requirements" in filename:
            ecosystem, parser_fn = "pypi", _parse_requirements_txt
        elif filename.endswith(".txt") and "constraints" in filename:
            ecosystem, parser_fn = "pypi", _parse_requirements_txt
        elif filename.endswith("package.json"):
            ecosystem, parser_fn = "npm", _parse_package_json
        elif filename.endswith("composer.json"):
            ecosystem, parser_fn = "packagist", _parse_composer_json
        elif filename.endswith(".csproj") or filename == "packages.config":
            ecosystem, parser_fn = "nuget", _parse_csproj
        else:
            raise ValueError(
                f"Cannot detect ecosystem from '{filename}'. "
                f"Use -e/--ecosystem to specify (pypi, npm, cargo, go, rubygems, packagist, nuget)."
            )

    parsed = parser_fn(path)

    if not include_dev:
        parsed = [p for p in parsed if not p.is_dev]

    # Deduplicate
    seen = set()
    entries = []
    for p in parsed:
        key = p.name.lower()
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            PackageEntry(
                obs_package=p.name,
                github_owner="",
                github_repo="",
                repo_url="",
                source="scan",
                ecosystem=ecosystem,
            )
        )

    return ecosystem, entries


def is_fresh(package_name: str, ecosystem: str, max_age_days: int = 7) -> bool:
    """Check if a package has been scored recently enough to skip."""
    try:
        from ossuary.db.models import Package
        with session_scope() as session:
            pkg = (
                session.query(Package)
                .filter(Package.name == package_name, Package.ecosystem == ecosystem)
                .first()
            )
            if pkg is None:
                return False
            if pkg.last_analyzed is None:
                return False
            age = (datetime.utcnow() - pkg.last_analyzed).days
            return age < max_age_days
    except Exception:
        return False


async def batch_score(
    packages: list[PackageEntry],
    max_concurrent: int = 3,
    max_packages: int = 0,
    skip_fresh: bool = True,
    fresh_days: int = 7,
    progress_callback: Optional[callable] = None,
) -> BatchResult:
    """
    Score a batch of packages with concurrency control.

    Args:
        packages: List of PackageEntry from discovery JSON
        max_concurrent: Maximum parallel scoring operations
        max_packages: Limit how many to score (0 = all)
        skip_fresh: Skip packages scored within fresh_days
        fresh_days: Number of days before a score is considered stale
        progress_callback: Optional callback(current, total, pkg_name, status)

    Returns:
        BatchResult with counts and error details
    """
    if max_packages > 0:
        packages = packages[:max_packages]

    result = BatchResult(total=len(packages))
    semaphore = asyncio.Semaphore(max_concurrent)
    completed = 0

    async def score_one(entry: PackageEntry) -> tuple[str, str]:
        """Score a single package, returning (pkg_name, status)."""
        nonlocal completed

        eco = entry.ecosystem
        if eco == "github":
            pkg_name = f"{entry.github_owner}/{entry.github_repo}"
        else:
            pkg_name = entry.obs_package

        # Check freshness
        if skip_fresh and is_fresh(pkg_name, eco, fresh_days):
            completed += 1
            return pkg_name, "skipped"

        async with semaphore:
            try:
                kwargs = {}
                if entry.repo_url:
                    kwargs["repo_url"] = entry.repo_url
                scoring_result = await score_package(
                    pkg_name,
                    eco,
                    force=not skip_fresh,
                    **kwargs,
                )
                completed += 1

                if scoring_result.success:
                    return pkg_name, "scored"
                else:
                    return pkg_name, f"error: {scoring_result.error}"
            except Exception as e:
                completed += 1
                return pkg_name, f"error: {e}"

    # Process all packages
    tasks = [score_one(entry) for entry in packages]

    for coro in asyncio.as_completed(tasks):
        pkg_name, status = await coro

        if status == "scored":
            result.scored += 1
        elif status == "skipped":
            result.skipped += 1
        else:
            result.errors += 1
            result.error_details.append(f"{pkg_name}: {status}")

        if progress_callback:
            progress_callback(completed, result.total, pkg_name, status)

    return result
