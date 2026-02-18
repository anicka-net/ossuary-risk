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

        if ecosystem not in ("github", "npm", "pypi"):
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
