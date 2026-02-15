"""Git repository collector - extracts commit history and metadata."""

import hashlib
import logging
import os
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
            # We only need author/date/message — never actual code.
            Repo.clone_from(
                repo_url,
                repo_path,
                multi_options=[
                    "--filter=blob:none",
                    "--shallow-since=3years",
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

        # Filter commits for last year
        recent_commits = [c for c in commits if c.authored_date >= one_year_ago and c.authored_date <= cutoff]

        # Count commits by author email
        author_counts: dict[str, int] = defaultdict(int)
        author_names: dict[str, str] = {}

        for commit in recent_commits:
            email = commit.author_email.lower()
            author_counts[email] += 1
            author_names[email] = commit.author_name

        # Find top contributor
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

        # Sort commits by date
        sorted_commits = sorted(commits, key=lambda c: c.authored_date)

        return GitMetrics(
            total_commits=len(commits),
            commits_last_year=total_recent,
            unique_contributors=unique_contributors,
            maintainer_concentration=concentration,
            top_contributor_email=top_email,
            top_contributor_name=author_names.get(top_email, ""),
            top_contributor_commits=top_commits,
            last_commit_date=sorted_commits[-1].authored_date if sorted_commits else None,
            first_commit_date=sorted_commits[0].authored_date if sorted_commits else None,
            commits=recent_commits,  # Store only recent commits for sentiment analysis
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
