"""GitHub API collector - maintainer info, issues, sponsors status."""

import asyncio
import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from ossuary.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


def _url_path(url: str) -> str:
    """Return just the path portion of a URL for short error labels."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).path or url
    except Exception:
        return url


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
    """Data collected from GitHub API.

    Two parallel error lists carry the data-completeness contract:

    - ``fetch_errors``: an essential call failed in a known transient
      way (rate limit / 5xx / network). The scoring engine treats this
      as ``INSUFFICIENT_DATA`` because we cannot even identify the
      repo. Permanent failures (404) are *not* listed here — they
      surface as "Repository not found" via the upstream collector.
    - ``provisional_reasons``: a non-essential call failed (sponsors,
      maintainer profile, orgs, issues, CII). The corresponding
      protective factor defaults to 0, raising the score
      conservatively. The engine still produces a number but flags it
      ``is_provisional=True`` so the user can rescore later. (Both
      lists describe failures whose missing protective factor raises
      the score; the split is about signal magnitude, not direction —
      see ``services.scorer.CollectedData``.)
    """

    # Repository info
    owner: str = ""
    repo: str = ""
    owner_type: str = ""  # User or Organization
    # GitHub's ``pushed_at`` for the repo — the timestamp of the last
    # push to any branch. Used by the snapshot cache as a cheap
    # freshness probe: if upstream ``pushed_at`` matches the value we
    # recorded at snapshot time, the repo hasn't changed and the
    # cached blob is still valid, even if it's past the SLA.
    # Stored as ISO-8601 string to match the rest of the dataclass's
    # date fields.
    pushed_at: str = ""

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

    # Data-completeness tracking (see class docstring)
    fetch_errors: list[str] = field(default_factory=list)
    provisional_reasons: list[str] = field(default_factory=list)


class GitHubCollector(BaseCollector):
    """Collector for GitHub API data."""

    API_BASE = "https://api.github.com"
    GRAPHQL_URL = "https://api.github.com/graphql"

    # Rate limiting — with token: 5000 req/hr (~1.4/sec), without: 60/hr
    REQUEST_DELAY = 0.1
    REQUEST_DELAY_UNAUTHENTICATED = 1.0
    RATE_LIMIT_PAUSE = 60

    # Tier-1 thresholds
    TIER1_REPOS = 500
    TIER1_STARS = 100_000

    def __init__(self, token: Optional[str] = None):
        """
        Initialize GitHub collector.

        Args:
            token: GitHub personal access token. Defaults to GITHUB_TOKEN env var.
                   Multiple tokens can be provided via GITHUB_TOKEN, GITHUB_TOKEN_SUSE, etc.
        """
        self.tokens = self._collect_tokens(token)
        self.token_index = 0
        self.token = self.tokens[0] if self.tokens else None
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

        if self.token:
            self.client.headers["Authorization"] = f"Bearer {self.token}"
        self.client.headers["Accept"] = "application/vnd.github.v3+json"

        # ``last_error`` carries the failure description from the most
        # recent ``_request`` / ``_graphql`` call. ``None`` means the
        # call either succeeded or returned a permanent ``404`` (not a
        # transient failure). Public methods leave their existing
        # return signature unchanged; ``collect()`` reads this between
        # calls to classify each failure as essential or provisional.
        self.last_error: Optional[str] = None

    @staticmethod
    def _collect_tokens(explicit_token: Optional[str] = None) -> list[str]:
        """Collect all available GitHub tokens from env vars."""
        tokens = []
        if explicit_token:
            tokens.append(explicit_token)
        else:
            for key, val in os.environ.items():
                if key.startswith("GITHUB_TOKEN") and val:
                    tokens.append(val)
        return tokens

    def _rotate_token(self) -> bool:
        """Switch to next available token. Returns True if rotated, False if no more tokens."""
        if len(self.tokens) <= 1:
            return False
        self.token_index = (self.token_index + 1) % len(self.tokens)
        self.token = self.tokens[self.token_index]
        self.client.headers["Authorization"] = f"Bearer {self.token}"
        logger.info(f"Rotated to GitHub token {self.token_index + 1}/{len(self.tokens)}")
        return True

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

    async def _request(self, method: str, url: str, _rotated: bool = False, **kwargs) -> Optional[dict]:
        """Make a rate-limit-aware request.

        Sets ``self.last_error`` to a single-line failure string when
        the call fails in a transient way (rate limit / 5xx / network /
        malformed JSON). ``404`` is intentionally *not* a transient
        failure — callers rely on the ``None`` return to mean
        "doesn't exist", and ``last_error`` stays ``None``. Successful
        calls clear ``last_error`` to ``None`` so callers can read it
        after each call without holding a stale value.
        """
        self.last_error = None
        delay = self.REQUEST_DELAY if self.token else self.REQUEST_DELAY_UNAUTHENTICATED
        await asyncio.sleep(delay)

        try:
            response = await self.client.request(method, url, **kwargs)

            # Check rate limit
            remaining = int(response.headers.get("X-RateLimit-Remaining", 1))
            if remaining == 0:
                reset_time = int(response.headers.get("X-RateLimit-Reset", 0))
                wait_time = max(reset_time - time.time(), self.RATE_LIMIT_PAUSE)
                if not _rotated and self._rotate_token():
                    logger.warning("Rate limited. Rotated token, retrying immediately.")
                    return await self._request(method, url, _rotated=True, **kwargs)
                logger.warning(f"Rate limited. Waiting {wait_time:.0f} seconds...")
                await asyncio.sleep(wait_time)
                return await self._request(method, url, **kwargs)

            if response.status_code == 404:
                return None

            if response.status_code >= 400:
                self.last_error = (
                    f"HTTP {response.status_code} from api.github.com "
                    f"({_url_path(url)})"
                )
                response.raise_for_status()
            return response.json()

        except httpx.HTTPError as e:
            if self.last_error is None:
                self.last_error = f"transport error from api.github.com ({e})"
            logger.error(f"GitHub API error: {e}")
            return None
        except ValueError as e:
            self.last_error = f"malformed JSON from api.github.com ({e})"
            logger.error(self.last_error)
            return None

    async def _get(self, endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET request to GitHub REST API."""
        url = f"{self.API_BASE}{endpoint}" if not endpoint.startswith("http") else endpoint
        return await self._request("GET", url, params=params)

    async def _graphql(self, query: str, variables: Optional[dict] = None,
                       _rotated: bool = False) -> Optional[dict]:
        """Execute GraphQL query.

        Same ``last_error`` contract as ``_request``: cleared on
        success, set to a transient failure string on 4xx/5xx /
        network / malformed payload. Returns ``None`` on failure.
        """
        self.last_error = None
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        await asyncio.sleep(self.REQUEST_DELAY)

        try:
            response = await self.client.post(self.GRAPHQL_URL, json=payload)

            if response.status_code == 403 and not _rotated and self._rotate_token():
                logger.warning("GraphQL rate limited. Rotated token, retrying.")
                return await self._graphql(query, variables, _rotated=True)

            if response.status_code >= 400:
                self.last_error = (
                    f"HTTP {response.status_code} from api.github.com (graphql)"
                )
                response.raise_for_status()
            data = response.json()

            if "errors" in data:
                self.last_error = "graphql errors: " + str(data["errors"])[:200]
                logger.error(f"GraphQL errors: {data['errors']}")
                return None

            return data.get("data")

        except httpx.HTTPError as e:
            if not _rotated and self._rotate_token():
                logger.warning("GraphQL error, rotated token, retrying.")
                return await self._graphql(query, variables, _rotated=True)
            if self.last_error is None:
                self.last_error = f"transport error from api.github.com (graphql: {e})"
            logger.error(f"GraphQL error: {e}")
            return None
        except ValueError as e:
            self.last_error = f"malformed JSON from api.github.com (graphql: {e})"
            logger.error(self.last_error)
            return None

    async def get_user(self, username: str) -> Optional[dict]:
        """Get GitHub user profile."""
        return await self._get(f"/users/{username}")

    async def get_user_repos(self, username: str, max_pages: int = 3) -> list[dict]:
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

    async def search_user_by_email(self, email: str) -> Optional[str]:
        """
        Search for GitHub username by email address.

        Args:
            email: Email address to search for

        Returns:
            GitHub username if found, None otherwise
        """
        if not email:
            return None

        # GitHub search API for users by email
        result = await self._get("/search/users", params={"q": f"{email} in:email"})
        if result and result.get("total_count", 0) > 0:
            items = result.get("items", [])
            if items:
                return items[0].get("login")

        return None

    async def get_repo_contributors(self, owner: str, repo: str, limit: int = 10) -> list[dict]:
        """
        Get top contributors for a repository.

        Args:
            owner: Repository owner
            repo: Repository name
            limit: Maximum number of contributors to return

        Returns:
            List of contributor dicts with login, contributions count
        """
        contributors = await self._get(
            f"/repos/{owner}/{repo}/contributors",
            params={"per_page": limit}
        )
        if not contributors or not isinstance(contributors, list):
            return []
        return contributors

    async def get_repo_info(self, owner: str, repo: str) -> Optional[dict]:
        """Get repository information."""
        return await self._get(f"/repos/{owner}/{repo}")

    @staticmethod
    async def probe_pushed_at(repo_url: str) -> Optional[str]:
        """Single-call freshness probe used by the snapshot cache.

        Returns the upstream ``pushed_at`` ISO string for ``repo_url``,
        or ``None`` if the URL doesn't parse, the repo is gone, or the
        request errored. The caller compares the result against the
        ``upstream_pushed_at`` recorded on the snapshot — equality
        means the repo is unchanged and the cached blob is still
        valid.

        Owns the collector lifecycle (creates and closes a fresh
        instance) so the cache layer doesn't need to plumb collectors
        around. Cost is one GET /repos/{owner}/{repo}, much cheaper
        than the full collect path it short-circuits.
        """
        owner, repo = GitHubCollector.parse_repo_url(repo_url)
        if not owner or not repo:
            return None
        collector = GitHubCollector()
        try:
            info = await collector.get_repo_info(owner, repo)
        finally:
            await collector.close()
        if not info:
            return None
        value = info.get("pushed_at")
        return value or None

    async def get_cii_badge_level(self, owner: str, repo: str) -> str:
        """Detect CII/Best Practices badge presence from the repository README.

        The badge markup in README files is a stable signal that the project
        participates in the program. The public badge variants do not reliably
        encode bronze/silver/gold in the URL, so we conservatively map presence
        to ``passing`` which is sufficient for the current scoring model.
        """
        readme = await self._get(f"/repos/{owner}/{repo}/readme")
        if not readme:
            return "none"

        content = readme.get("content", "")
        encoding = readme.get("encoding", "")
        if not content or encoding != "base64":
            return "none"

        try:
            decoded = base64.b64decode(content, validate=False).decode("utf-8", errors="ignore")
        except (ValueError, TypeError):
            return "none"

        if "bestpractices.coreinfrastructure.org/projects/" in decoded:
            return "passing"

        return "none"

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
        per_page: int = 30,
        max_comment_fetches: int = 10,
    ) -> list[IssueData]:
        """
        Get issues and PRs from a repository.

        Args:
            owner: Repository owner
            repo: Repository name
            state: Issue state filter (all, open, closed)
            per_page: Number of issues to fetch
            max_comment_fetches: Max issues to fetch comments for (API call each)

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
        comment_fetches = 0
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

            # Fetch comments for a limited number of issues (each is an API call)
            if comment_fetches < max_comment_fetches and issue.get("comments", 0) > 0:
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
                comment_fetches += 1

            issues.append(issue_obj)

        return issues

    def _record_failure(
        self, data: GitHubData, label: str, *, essential: bool
    ) -> None:
        """Append ``self.last_error`` to the right bucket on ``data``.

        Essential = the call is load-bearing for scoring; missing it
        leaves us without a usable signal so we mark INSUFFICIENT_DATA.
        Non-essential = the missing signal would default to "no
        protective factor", raising the score conservatively. We mark
        provisional and let the score still compute.

        Idempotent: if ``last_error`` is ``None`` (success or 404)
        nothing is recorded.
        """
        err = self.last_error
        if not err:
            return
        bucket = data.fetch_errors if essential else data.provisional_reasons
        bucket.append(f"github.{label}: {err}")

    # ------------------------------------------------------------------
    # Per-signal-family collection (v0.10.1 phase 3 step 2)
    # ------------------------------------------------------------------
    #
    # ``collect()`` orchestrates these helpers; each is independently
    # callable so a future cache layer can refresh just one family on
    # its own cadence (the v0.11 design — see
    # ``docs/data_reuse_design.md`` §4 and the GPT-roadmap rationale
    # for un-shipping the freshness probe at commit 7093f36).
    #
    # Each family method takes the in-flight ``GitHubData`` and mutates
    # it with the family's signals plus any failure records. They DO
    # NOT compose family-to-family dependencies internally — the
    # orchestrator threads the canonical ``(owner, repo)`` and resolved
    # maintainer username between them — so per-family refresh in a
    # future cache layer doesn't have to know the dependency order.

    async def collect_repo_meta(
        self, owner: str, repo: str, data: GitHubData,
    ) -> tuple[str, str, Optional[dict]]:
        """Fetch repo metadata: owner type + canonical owner/repo names.

        Returns ``(canonical_owner, canonical_repo, repo_info_dict)``.
        The dict is returned so the orchestrator can use it for
        last-resort maintainer resolution on org repos without paying
        a second ``get_repo_info`` call. Mutates ``data`` with the
        owner type and canonical names, and records the essential
        failure flag if the repo info is unavailable transiently.
        """
        repo_info = await self.get_repo_info(owner, repo)
        if repo_info is None:
            self._record_failure(data, "repo_info", essential=True)
            return owner, repo, None

        data.owner_type = repo_info.get("owner", {}).get("type", "")
        canonical_owner = repo_info.get("owner", {}).get("login") or owner
        canonical_repo = repo_info.get("name") or repo
        if canonical_owner != owner or canonical_repo != repo:
            logger.info(
                f"Repo redirected: {owner}/{repo} -> "
                f"{canonical_owner}/{canonical_repo}"
            )
        data.owner = canonical_owner
        data.repo = canonical_repo
        return canonical_owner, canonical_repo, repo_info

    async def resolve_maintainer(
        self,
        owner: str,
        repo: str,
        data: GitHubData,
        top_contributor_username: Optional[str],
        top_contributor_email: Optional[str],
        repo_info: Optional[dict],
    ) -> str:
        """Decide which GitHub username represents the project's
        maintainer, in priority order:

        1. ``top_contributor_username`` (from git commit history)
        2. For orgs: top contributor returned by GitHub API
        3. Email-based GitHub user search
        4. Repo owner (only if not an organization)
        5. Last-resort fallback to the org's login from repo_info

        Returns the resolved username and stores it on ``data``.
        """
        maintainer_username: Optional[str] = None

        if top_contributor_username:
            maintainer_username = top_contributor_username
            logger.info(f"Using provided top contributor: {maintainer_username}")

        if not maintainer_username and data.owner_type == "Organization":
            logger.info(f"Repo is org-owned, finding top contributor...")
            contributors = await self.get_repo_contributors(owner, repo, limit=1)
            if not contributors:
                self._record_failure(data, "contributors", essential=False)
            if contributors:
                maintainer_username = contributors[0].get("login")
                logger.info(f"Top contributor from GitHub API: {maintainer_username}")

        if not maintainer_username and top_contributor_email:
            logger.info(
                f"Searching GitHub for user with email: {top_contributor_email}"
            )
            maintainer_username = await self.search_user_by_email(top_contributor_email)
            # email search is best-effort; failures silently fine.
            if maintainer_username:
                logger.info(f"Found user by email: {maintainer_username}")

        if not maintainer_username:
            if data.owner_type != "Organization":
                maintainer_username = owner
                logger.info(f"Using repo owner as maintainer: {maintainer_username}")
            else:
                maintainer_username = (
                    repo_info.get("owner", {}).get("login", owner)
                    if repo_info else owner
                )
                logger.warning(
                    f"Could not determine maintainer for org repo, "
                    f"using: {maintainer_username}"
                )

        data.maintainer_username = maintainer_username
        logger.info(f"Final maintainer: {data.maintainer_username}")
        return maintainer_username

    async def collect_maintainer_profile(
        self, username: str, data: GitHubData,
    ) -> None:
        """Fetch the maintainer's GitHub profile, public repo list,
        sponsorship state, and org memberships. All non-essential —
        each failure leaves a protective factor at 0 and raises the
        score conservatively (see class docstring)."""
        # Account age + public repo count.
        user_profile = await self.get_user(username)
        if user_profile is None:
            self._record_failure(data, "user_profile", essential=False)
        if user_profile:
            data.maintainer_account_created = user_profile.get("created_at", "")
            data.maintainer_public_repos = user_profile.get("public_repos", 0)

        # Repo list for reputation scoring.
        logger.info(f"Fetching repos for {username}...")
        repos = await self.get_user_repos(username)
        if not repos:
            self._record_failure(data, "user_repos", essential=False)
        data.maintainer_repos = repos
        data.maintainer_total_stars = sum(
            r.get("stargazers_count", 0) for r in repos
        )

        # Sponsorship.
        if username and "[bot]" not in username:
            logger.info(f"Checking sponsors for {username}...")
            data.has_github_sponsors = await self.get_sponsors_status(username)
            if self.last_error:
                self._record_failure(data, "sponsors_status", essential=False)
            if data.has_github_sponsors:
                data.maintainer_sponsor_count = await self.get_sponsor_count(username)
                if self.last_error:
                    self._record_failure(data, "sponsor_count", essential=False)

        # Org memberships (different from "repo is org-owned").
        logger.info(f"Fetching orgs for {username}...")
        data.maintainer_orgs = await self.get_user_orgs(username)
        if not data.maintainer_orgs and self.last_error:
            self._record_failure(data, "user_orgs", essential=False)

        # Legacy tier-1 derivation; ReputationScorer now does the work
        # but the field still exists for back-compat with older blobs.
        data.is_tier1_maintainer = (
            data.maintainer_public_repos > self.TIER1_REPOS
            or data.maintainer_total_stars > self.TIER1_STARS
        )

    async def collect_org_admins_family(
        self, owner: str, repo: str, data: GitHubData,
    ) -> None:
        """Determine whether the repo is owned by a GitHub organisation
        and, if so, estimate the admin count. Non-essential."""
        logger.info(f"Checking organization status for {owner}/{repo}...")
        org_info = await self.get_org_admins(owner, repo)
        if self.last_error:
            self._record_failure(data, "org_admins", essential=False)
        data.is_org_owned = org_info["is_org"]
        data.org_admin_count = org_info["admin_count"]

    async def collect_cii_family(
        self, owner: str, repo: str, data: GitHubData,
    ) -> None:
        """Detect CII / Best Practices badge presence. Non-essential."""
        logger.info(f"Checking CII badge for {owner}/{repo}...")
        data.cii_badge_level = await self.get_cii_badge_level(owner, repo)
        if data.cii_badge_level == "none" and self.last_error:
            self._record_failure(data, "cii_badge", essential=False)

    async def collect_issues_family(
        self, owner: str, repo: str, data: GitHubData,
    ) -> None:
        """Fetch recent issues + comments for sentiment analysis.
        Non-essential — missing issues disables the frustration /
        sentiment layers but leaves the rest of the score intact."""
        logger.info(f"Fetching issues for {owner}/{repo}...")
        data.issues = await self.get_issues(owner, repo)
        if not data.issues and self.last_error:
            self._record_failure(data, "issues", essential=False)

    async def collect(
        self,
        repo_url: str,
        top_contributor_username: str = None,
        top_contributor_email: str = None,
    ) -> GitHubData:
        """
        Collect all GitHub data for a repository.

        Composes the per-signal-family methods declared above. Failure
        classification (see ``GitHubData`` docstring):
        - repo_info transient failure: ESSENTIAL — without it we don't
          know owner type, can't find the canonical name, can't run
          the org-admin check. Marked ``fetch_errors`` →
          INSUFFICIENT_DATA upstream.
        - everything else (user profile, repos, sponsors, orgs, org
          admins, CII, issues, contributors): NON-ESSENTIAL — failure
          defaults the corresponding protective factor to 0, which
          raises the score conservatively. Marked
          ``provisional_reasons``.

        Args:
            repo_url: GitHub repository URL
            top_contributor_username: Override maintainer username (e.g., from git history)
            top_contributor_email: Top contributor's email for GitHub lookup

        Returns:
            GitHubData with all collected information
        """
        owner, repo = self.parse_repo_url(repo_url)
        if not owner or not repo:
            logger.error(f"Could not parse repository URL: {repo_url}")
            return GitHubData()

        data = GitHubData(owner=owner, repo=repo)

        # Family 1: repo metadata. Resolves canonical owner/repo + owner type.
        owner, repo, repo_info = await self.collect_repo_meta(owner, repo, data)

        # Family 2a: maintainer identity. Depends on family 1 (owner_type).
        maintainer = await self.resolve_maintainer(
            owner, repo, data,
            top_contributor_username, top_contributor_email, repo_info,
        )

        # Family 2b: maintainer profile signals.
        await self.collect_maintainer_profile(maintainer, data)

        # Family 3-5: independent of maintainer; can refresh on their
        # own cadence in a future per-family cache layer.
        await self.collect_org_admins_family(owner, repo, data)
        await self.collect_cii_family(owner, repo, data)
        await self.collect_issues_family(owner, repo, data)

        return data

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
