"""Git repository collector - extracts commit history and metadata."""

import hashlib
import logging
import os
import re
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError

from ossuary.collectors.base import BaseCollector

logger = logging.getLogger(__name__)

# GitHub noreply format: 12345+username@users.noreply.github.com
_GITHUB_NOREPLY_RE = re.compile(r"^\d+\+(.+)@users\.noreply\.github\.com$")


def _normalize_email(email: str) -> str:
    """Normalize an email address to a canonical identity key.

    Only handles the unambiguous GitHub noreply case:
      - 12345+cfconrad@users.noreply.github.com → cfconrad@users.noreply.github.com

    General emails are lowercased but otherwise preserved. Merging by local
    part (e.g. user@suse.de + user@suse.com) was too aggressive — it falsely
    merges unrelated people who share common usernames.
    """
    email = email.lower().strip()
    if not email or "@" not in email:
        return email

    # Handle GitHub noreply: strip numeric prefix
    # 12345+user@users.noreply.github.com → user@users.noreply.github.com
    m = _GITHUB_NOREPLY_RE.match(email)
    if m:
        return f"{m.group(1)}@users.noreply.github.com"

    return email


@dataclass
class CommitData:
    """Extracted commit data."""

    sha: str
    author_name: str
    author_email: str
    authored_date: datetime
    committer_name: str
    committer_email: str
    committed_date: datetime
    message: str


@dataclass
class GitMetrics:
    """Metrics extracted from git history."""

    total_commits: int = 0
    commits_last_year: int = 0
    unique_contributors: int = 0
    maintainer_concentration: float = 0.0
    top_contributor_email: str = ""
    top_contributor_name: str = ""
    top_contributor_commits: int = 0
    last_commit_date: Optional[datetime] = None
    first_commit_date: Optional[datetime] = None
    commits: list[CommitData] = None

    # Maturity detection fields
    lifetime_contributors: int = 0
    lifetime_concentration: float = 0.0
    is_mature: bool = False
    repo_age_years: float = 0.0

    # Takeover detection (proportion shift)
    takeover_shift: float = 0.0       # max % shift of any contributor (historical→recent)
    takeover_suspect: str = ""        # email of the contributor with highest shift
    takeover_suspect_name: str = ""   # display name

    def __post_init__(self):
        if self.commits is None:
            self.commits = []


class GitCollector(BaseCollector):
    """Collector for git repository data."""

    def __init__(self, repos_path: Optional[str] = None):
        """
        Initialize the git collector.

        Args:
            repos_path: Path to store cloned repositories. Defaults to ./repos
        """
        self.repos_path = Path(repos_path or os.getenv("REPOS_PATH", "./repos"))
        self.repos_path.mkdir(parents=True, exist_ok=True)

    def is_available(self) -> bool:
        """Git collector is always available."""
        return True

    def _get_repo_path(self, repo_url: str) -> Path:
        """Get local path for a repository."""
        # Create a hash-based directory name to avoid path issues
        url_hash = hashlib.md5(repo_url.encode()).hexdigest()[:12]
        # Extract repo name for readability
        repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        return self.repos_path / f"{repo_name}_{url_hash}"

    def clone_or_update(self, repo_url: str) -> Path:
        """
        Clone a repository or update if it already exists.

        Args:
            repo_url: Git repository URL

        Returns:
            Path to the local repository
        """
        repo_path = self._get_repo_path(repo_url)

        if repo_path.exists():
            try:
                logger.info(f"Updating existing repository: {repo_path}")
                repo = Repo(repo_path)
                repo.remotes.origin.fetch()
                return repo_path
            except (InvalidGitRepositoryError, GitCommandError) as e:
                logger.warning(f"Failed to update repository, re-cloning: {e}")
                shutil.rmtree(repo_path)

        logger.info(f"Cloning repository: {repo_url}")
        try:
            # Blobless partial clone: fetches commit metadata only, no file content.
            # We need full commit history for maturity detection (repo age,
            # lifetime contributors) but never actual file blobs.
            Repo.clone_from(
                repo_url,
                repo_path,
                multi_options=[
                    "--filter=blob:none",
                    "--single-branch",
                ],
            )
            return repo_path
        except GitCommandError as e:
            logger.error(f"Failed to clone repository: {e}")
            raise

    def extract_commits(
        self,
        repo_path: Path,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[CommitData]:
        """
        Extract commit data from a repository.

        Args:
            repo_path: Path to the local repository
            since: Only include commits after this date
            until: Only include commits before this date

        Returns:
            List of CommitData objects
        """
        repo = Repo(repo_path)
        commits = []

        for commit in repo.iter_commits("--all"):
            authored_date = datetime.fromtimestamp(commit.authored_date)
            committed_date = datetime.fromtimestamp(commit.committed_date)

            # Filter by date range
            if since and authored_date < since:
                continue
            if until and authored_date > until:
                continue

            commits.append(
                CommitData(
                    sha=commit.hexsha,
                    author_name=commit.author.name or "",
                    author_email=commit.author.email or "",
                    authored_date=authored_date,
                    committer_name=commit.committer.name or "",
                    committer_email=commit.committer.email or "",
                    committed_date=committed_date,
                    message=commit.message,
                )
            )

        return commits

    def calculate_metrics(
        self,
        commits: list[CommitData],
        cutoff_date: Optional[datetime] = None,
    ) -> GitMetrics:
        """
        Calculate metrics from commit data.

        Args:
            commits: List of commits to analyze
            cutoff_date: Date to use as "now" for calculations (for T-1 analysis)

        Returns:
            GitMetrics with calculated values
        """
        if not commits:
            return GitMetrics()

        cutoff = cutoff_date or datetime.now()
        one_year_ago = cutoff - timedelta(days=365)

        # Sort commits by date (needed for first/last and historical analysis)
        sorted_commits = sorted(commits, key=lambda c: c.authored_date)
        first_commit_date = sorted_commits[0].authored_date
        last_commit_date = sorted_commits[-1].authored_date

        # --- Lifetime stats (all commits) ---
        # Use normalized email to merge identities (e.g. user@suse.de + user@suse.com)
        lifetime_author_counts: dict[str, int] = defaultdict(int)
        for commit in commits:
            lifetime_author_counts[_normalize_email(commit.author_email)] += 1

        lifetime_contributors = len(lifetime_author_counts)
        if lifetime_author_counts:
            lt_top_id = max(lifetime_author_counts, key=lifetime_author_counts.get)
            lifetime_concentration = (lifetime_author_counts[lt_top_id] / len(commits) * 100)
        else:
            lifetime_concentration = 100.0

        # --- Recent stats (last 12 months) ---
        recent_commits = [c for c in commits if c.authored_date >= one_year_ago and c.authored_date <= cutoff]

        author_counts: dict[str, int] = defaultdict(int)
        author_names: dict[str, str] = {}

        for commit in recent_commits:
            identity = _normalize_email(commit.author_email)
            author_counts[identity] += 1
            author_names[identity] = commit.author_name

        total_recent = len(recent_commits)
        unique_contributors = len(author_counts)

        if author_counts:
            top_email = max(author_counts, key=author_counts.get)
            top_commits = author_counts[top_email]
            concentration = (top_commits / total_recent * 100) if total_recent > 0 else 0
        else:
            top_email = ""
            top_commits = 0
            concentration = 100  # No commits = maximum concentration (abandoned)

        # --- Maturity detection ---
        repo_age_years = (cutoff - first_commit_date).days / 365.25
        days_since_last_commit = (cutoff - last_commit_date).days

        is_mature = (
            repo_age_years >= 5
            and len(commits) >= 30
            and days_since_last_commit < 5 * 365  # not truly dead
        )

        # --- Takeover detection: proportion shift ---
        # Detects when a minor historical contributor suddenly dominates recent commits.
        # This is the xz/Jia Tan pattern: 0.8% historical → 50% recent = +49% shift.
        takeover_shift = 0.0
        takeover_suspect = ""
        takeover_suspect_name = ""

        if recent_commits and is_mature and total_recent >= 5:
            historical_commits = [c for c in commits if c.authored_date < one_year_ago]
            hist_total = len(historical_commits)

            # Historical share per contributor (using normalized identities)
            hist_counts: dict[str, int] = defaultdict(int)
            for c in historical_commits:
                hist_counts[_normalize_email(c.author_email)] += 1

            # Find the contributor with the largest upward shift.
            # Only flag genuinely minor/new contributors — not established
            # maintainers whose share naturally fluctuates.
            for identity, recent_count in author_counts.items():
                # Skip bots (dependabot, renovate, etc.)
                name = author_names.get(identity, "")
                if "[bot]" in identity or "[bot]" in name:
                    continue

                recent_pct = recent_count / total_recent * 100
                hist_pct = (hist_counts.get(identity, 0) / hist_total * 100) if hist_total > 0 else 0
                shift = recent_pct - hist_pct

                # Only flag if the contributor was minor historically (<5% of commits).
                # Established maintainers (e.g. project creator at 20%) naturally
                # fluctuate — that's not a takeover signal.
                if hist_pct >= 5:
                    continue

                if shift > takeover_shift:
                    takeover_shift = shift
                    takeover_suspect = identity
                    takeover_suspect_name = name

        return GitMetrics(
            total_commits=len(commits),
            commits_last_year=total_recent,
            unique_contributors=unique_contributors,
            maintainer_concentration=concentration,
            top_contributor_email=top_email,
            top_contributor_name=author_names.get(top_email, ""),
            top_contributor_commits=top_commits,
            last_commit_date=last_commit_date,
            first_commit_date=first_commit_date,
            commits=recent_commits,
            lifetime_contributors=lifetime_contributors,
            lifetime_concentration=lifetime_concentration,
            is_mature=is_mature,
            repo_age_years=repo_age_years,
            takeover_shift=takeover_shift,
            takeover_suspect=takeover_suspect,
            takeover_suspect_name=takeover_suspect_name,
        )

    async def collect(self, repo_url: str, cutoff_date: Optional[datetime] = None) -> GitMetrics:
        """
        Collect git data for a repository.

        Args:
            repo_url: Git repository URL
            cutoff_date: Date to use as "now" for T-1 analysis

        Returns:
            GitMetrics with all calculated values
        """
        repo_path = self.clone_or_update(repo_url)
        commits = self.extract_commits(repo_path)
        return self.calculate_metrics(commits, cutoff_date)
