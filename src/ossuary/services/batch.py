"""Batch scoring service for ossuary.

Scores packages from a discovery JSON file (e.g., suse_packages.json)
with concurrency control, resume support, and progress tracking.
"""

import asyncio
import json
import logging
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
    """A package entry from the discovery JSON."""

    obs_package: str
    github_owner: str
    github_repo: str
    repo_url: str
    source: str  # "service" or "spec"
    obs_project: str = ""


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

        pkg_name = f"{entry.github_owner}/{entry.github_repo}"

        # Check freshness
        if skip_fresh and is_fresh(pkg_name, "github", fresh_days):
            completed += 1
            return pkg_name, "skipped"

        async with semaphore:
            try:
                scoring_result = await score_package(
                    pkg_name,
                    "github",
                    repo_url=entry.repo_url,
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
