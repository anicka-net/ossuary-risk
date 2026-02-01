"""GitHub API collector - maintainer info, issues, sponsors status."""

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from ossuary.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


@dataclass
class IssueData:
    """Extracted issue/PR data."""

    number: int
    title: str
    body: str
    state: str
    is_pull_request: bool
    author_login: str
    created_at: str
    updated_at: str
    closed_at: Optional[str]
    comments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GitHubData:
    """Data collected from GitHub API."""

    # Repository info
    owner: str = ""
    repo: str = ""
    owner_type: str = ""  # User or Organization

    # Maintainer info
    maintainer_username: str = ""
    maintainer_public_repos: int = 0
    maintainer_total_stars: int = 0
    maintainer_account_created: str = ""  # ISO date string
    maintainer_repos: list[dict] = field(default_factory=list)  # Full repo data for reputation
    maintainer_sponsor_count: int = 0
    maintainer_orgs: list[str] = field(default_factory=list)
    is_tier1_maintainer: bool = False  # Deprecated, use reputation scorer
    has_github_sponsors: bool = False

    # Organization info
    is_org_owned: bool = False
    org_admin_count: int = 0

    # CII badge
    cii_badge_level: str = "none"

    # Issues and PRs
    issues: list[IssueData] = field(default_factory=list)


class GitHubCollector(BaseCollector):
    """Collector for GitHub API data."""

    API_BASE = "https://api.github.com"
    GRAPHQL_URL = "https://api.github.com/graphql"

    # Rate limiting
    REQUEST_DELAY = 0.5
    RATE_LIMIT_PAUSE = 60

    # Tier-1 thresholds
    TIER1_REPOS = 500
    TIER1_STARS = 100_000

    def __init__(self, token: Optional[str] = None):
        """
        Initialize GitHub collector.

        Args:
            token: GitHub personal access token. Defaults to GITHUB_TOKEN env var.
        """
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.client = httpx.AsyncClient(timeout=30.0)

        if self.token:
            self.client.headers["Authorization"] = f"Bearer {self.token}"
        self.client.headers["Accept"] = "application/vnd.github.v3+json"

    def is_available(self) -> bool:
        """Check if GitHub token is available."""
        return bool(self.token)

    @staticmethod
    def parse_repo_url(repo_url: str) -> tuple[Optional[str], Optional[str]]:
        """
        Parse owner and repo from GitHub URL.

        Args:
            repo_url: GitHub repository URL

        Returns:
            Tuple of (owner, repo) or (None, None) if parsing fails
        """
        patterns = [
            r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$",
            r"github\.com[:/]([^/]+)/([^/]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, repo_url)
            if match:
                return match.group(1), match.group(2).replace(".git", "")

        return None, None

    async def _request(self, method: str, url: str, **kwargs) -> Optional[dict]:
        """Make a rate-limit-aware request."""
        time.sleep(self.REQUEST_DELAY)

        try:
            response = await self.client.request(method, url, **kwargs)

            # Check rate limit
            remaining = int(response.headers.get("X-RateLimit-Remaining", 1))
            if remaining == 0:
                reset_time = int(response.headers.get("X-RateLimit-Reset", 0))
                wait_time = max(reset_time - time.time(), self.RATE_LIMIT_PAUSE)
                logger.warning(f"Rate limited. Waiting {wait_time:.0f} seconds...")
                time.sleep(wait_time)
                return await self._request(method, url, **kwargs)

            if response.status_code == 404:
                return None

            response.raise_for_status()
            return response.json()

        except httpx.HTTPError as e:
            logger.error(f"GitHub API error: {e}")
            return None

    async def _get(self, endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET request to GitHub REST API."""
        url = f"{self.API_BASE}{endpoint}" if not endpoint.startswith("http") else endpoint
        return await self._request("GET", url, params=params)

    async def _graphql(self, query: str, variables: Optional[dict] = None) -> Optional[dict]:
        """Execute GraphQL query."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        time.sleep(self.REQUEST_DELAY)

        try:
            response = await self.client.post(self.GRAPHQL_URL, json=payload)
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                logger.error(f"GraphQL errors: {data['errors']}")
                return None

            return data.get("data")

        except httpx.HTTPError as e:
            logger.error(f"GraphQL error: {e}")
            return None

    async def get_user(self, username: str) -> Optional[dict]:
        """Get GitHub user profile."""
        return await self._get(f"/users/{username}")

    async def get_user_repos(self, username: str, max_pages: int = 10) -> list[dict]:
        """Get all public repos for a user."""
        repos = []
        page = 1

        while page <= max_pages:
            data = await self._get(
                f"/users/{username}/repos",
                params={"per_page": 100, "page": page, "type": "owner"},
            )

            if not data:
                break

            repos.extend(data)

            if len(data) < 100:
                break

            page += 1

        return repos

    async def get_maintainer_reputation(self, username: str) -> dict:
        """Get maintainer reputation metrics."""
        user = await self.get_user(username)
        if not user:
            return {"public_repos": 0, "total_stars": 0, "is_tier1": False}

        public_repos = user.get("public_repos", 0)

        # Calculate total stars
        repos = await self.get_user_repos(username)
        total_stars = sum(r.get("stargazers_count", 0) for r in repos)

        is_tier1 = public_repos > self.TIER1_REPOS or total_stars > self.TIER1_STARS

        return {
            "public_repos": public_repos,
            "total_stars": total_stars,
            "is_tier1": is_tier1,
        }

    async def get_sponsors_status(self, username: str) -> bool:
        """Check if user has GitHub Sponsors enabled."""
        query = """
        query($login: String!) {
            user(login: $login) {
                hasSponsorsListing
            }
        }
        """

        data = await self._graphql(query, {"login": username})
        if not data:
            return False

        user = data.get("user", {})
        return user.get("hasSponsorsListing", False)

    async def get_sponsor_count(self, username: str) -> int:
        """Get count of sponsors for a user."""
        query = """
        query($login: String!) {
            user(login: $login) {
                sponsors {
                    totalCount
                }
            }
        }
        """

        data = await self._graphql(query, {"login": username})
        if not data:
            return 0

        user = data.get("user", {})
        sponsors = user.get("sponsors", {})
        return sponsors.get("totalCount", 0)

    async def get_user_orgs(self, username: str) -> list[str]:
        """Get list of organizations a user belongs to."""
        orgs_data = await self._get(f"/users/{username}/orgs")
        if not orgs_data or not isinstance(orgs_data, list):
            return []
        return [org.get("login", "") for org in orgs_data if org.get("login")]

    async def get_repo_info(self, owner: str, repo: str) -> Optional[dict]:
        """Get repository information."""
        return await self._get(f"/repos/{owner}/{repo}")

    async def get_org_admins(self, owner: str, repo: str) -> dict:
        """Check if repo is org-owned and estimate admin count."""
        repo_data = await self.get_repo_info(owner, repo)
        if not repo_data:
            return {"is_org": False, "admin_count": 0}

        owner_type = repo_data.get("owner", {}).get("type")

        if owner_type != "Organization":
            return {"is_org": False, "admin_count": 0}

        # Try to get org members (may require permissions)
        members = await self._get(f"/orgs/{owner}/members", params={"role": "admin"})
        admin_count = len(members) if isinstance(members, list) else 1

        return {"is_org": True, "admin_count": max(admin_count, 1)}

    async def get_issues(
        self,
        owner: str,
        repo: str,
        state: str = "all",
        per_page: int = 100,
        include_comments: bool = True,
    ) -> list[IssueData]:
        """
        Get issues and PRs from a repository.

        Args:
            owner: Repository owner
            repo: Repository name
            state: Issue state filter (all, open, closed)
            per_page: Number of issues per page
            include_comments: Whether to fetch comments for each issue

        Returns:
            List of IssueData objects
        """
        issues_data = await self._get(
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": per_page, "sort": "updated"},
        )

        if not issues_data:
            return []

        issues = []
        for issue in issues_data:
            issue_obj = IssueData(
                number=issue.get("number"),
                title=issue.get("title", ""),
                body=issue.get("body", "") or "",
                state=issue.get("state", ""),
                is_pull_request="pull_request" in issue,
                author_login=issue.get("user", {}).get("login", ""),
                created_at=issue.get("created_at", ""),
                updated_at=issue.get("updated_at", ""),
                closed_at=issue.get("closed_at"),
            )

            # Fetch comments if requested
            if include_comments and issue.get("comments", 0) > 0:
                comments = await self._get(f"/repos/{owner}/{repo}/issues/{issue['number']}/comments")
                if comments:
                    issue_obj.comments = [
                        {
                            "id": c.get("id"),
                            "author": c.get("user", {}).get("login", ""),
                            "body": c.get("body", ""),
                            "created_at": c.get("created_at", ""),
                        }
                        for c in comments
                    ]

            issues.append(issue_obj)

        return issues

    async def collect(self, repo_url: str, top_contributor_username: str = None) -> GitHubData:
        """
        Collect all GitHub data for a repository.

        Args:
            repo_url: GitHub repository URL
            top_contributor_username: Override maintainer username (e.g., from git history)

        Returns:
            GitHubData with all collected information
        """
        owner, repo = self.parse_repo_url(repo_url)
        if not owner or not repo:
            logger.error(f"Could not parse repository URL: {repo_url}")
            return GitHubData()

        data = GitHubData(owner=owner, repo=repo)

        # Get repo info
        repo_info = await self.get_repo_info(owner, repo)
        if repo_info:
            data.owner_type = repo_info.get("owner", {}).get("type", "")
            # Use provided top contributor or fall back to repo owner
            data.maintainer_username = top_contributor_username or repo_info.get("owner", {}).get("login", owner)

        # If owner is an org, we should use top_contributor_username if provided
        if data.owner_type == "Organization" and top_contributor_username:
            data.maintainer_username = top_contributor_username

        logger.info(f"Fetching data for maintainer: {data.maintainer_username}...")

        # Get user profile (for account age)
        user_profile = await self.get_user(data.maintainer_username)
        if user_profile:
            data.maintainer_account_created = user_profile.get("created_at", "")
            data.maintainer_public_repos = user_profile.get("public_repos", 0)

        # Get full repo list for reputation scoring
        logger.info(f"Fetching repos for {data.maintainer_username}...")
        repos = await self.get_user_repos(data.maintainer_username)
        data.maintainer_repos = repos
        data.maintainer_total_stars = sum(r.get("stargazers_count", 0) for r in repos)

        # Check sponsors status and count
        logger.info(f"Checking sponsors for {data.maintainer_username}...")
        data.has_github_sponsors = await self.get_sponsors_status(data.maintainer_username)
        if data.has_github_sponsors:
            data.maintainer_sponsor_count = await self.get_sponsor_count(data.maintainer_username)

        # Get user's organizations
        logger.info(f"Fetching orgs for {data.maintainer_username}...")
        data.maintainer_orgs = await self.get_user_orgs(data.maintainer_username)

        # Legacy tier-1 check (deprecated, use ReputationScorer instead)
        data.is_tier1_maintainer = (
            data.maintainer_public_repos > self.TIER1_REPOS
            or data.maintainer_total_stars > self.TIER1_STARS
        )

        # Check organization ownership of repo
        logger.info(f"Checking organization status for {owner}/{repo}...")
        org_info = await self.get_org_admins(owner, repo)
        data.is_org_owned = org_info["is_org"]
        data.org_admin_count = org_info["admin_count"]

        # Get issues
        logger.info(f"Fetching issues for {owner}/{repo}...")
        data.issues = await self.get_issues(owner, repo)

        return data

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
