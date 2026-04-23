"""Tests for GitHub collector."""

import asyncio
import base64
from unittest.mock import AsyncMock

from ossuary.collectors.github import GitHubCollector


class TestParseRepoUrl:
    """Tests for parse_repo_url static method."""

    def test_standard_https_url(self):
        owner, repo = GitHubCollector.parse_repo_url("https://github.com/pallets/flask")
        assert owner == "pallets"
        assert repo == "flask"

    def test_https_with_trailing_slash(self):
        owner, repo = GitHubCollector.parse_repo_url("https://github.com/pallets/flask/")
        assert owner == "pallets"
        assert repo == "flask"

    def test_https_with_git_suffix(self):
        owner, repo = GitHubCollector.parse_repo_url("https://github.com/pallets/flask.git")
        assert owner == "pallets"
        assert repo == "flask"

    def test_ssh_url(self):
        owner, repo = GitHubCollector.parse_repo_url("git@github.com:pallets/flask.git")
        assert owner == "pallets"
        assert repo == "flask"

    def test_ssh_url_without_git_suffix(self):
        owner, repo = GitHubCollector.parse_repo_url("git@github.com:pallets/flask")
        assert owner == "pallets"
        assert repo == "flask"

    def test_invalid_url_returns_none(self):
        owner, repo = GitHubCollector.parse_repo_url("https://example.com/foo")
        assert owner is None
        assert repo is None

    def test_empty_string(self):
        owner, repo = GitHubCollector.parse_repo_url("")
        assert owner is None
        assert repo is None

    def test_url_with_extra_path(self):
        owner, repo = GitHubCollector.parse_repo_url("https://github.com/pallets/flask/tree/main")
        assert owner == "pallets"
        assert repo == "flask"

    def test_org_repo_style(self):
        owner, repo = GitHubCollector.parse_repo_url("https://github.com/kubernetes/kubernetes")
        assert owner == "kubernetes"
        assert repo == "kubernetes"


class TestCiiBadgeDetection:
    """Tests for README-based CII badge detection."""

    def test_detects_cii_badge_from_readme(self):
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                readme = (
                    "[![CII Best Practices]"
                    "(https://bestpractices.coreinfrastructure.org/projects/1234/badge)]"
                    "(https://bestpractices.coreinfrastructure.org/projects/1234)"
                )
                collector._get = AsyncMock(
                    return_value={
                        "encoding": "base64",
                        "content": base64.b64encode(readme.encode()).decode(),
                    }
                )

                level = await collector.get_cii_badge_level("owner", "repo")

                assert level == "passing"
            finally:
                await collector.close()

        asyncio.run(run())

    def test_returns_none_when_badge_missing(self):
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                collector._get = AsyncMock(
                    return_value={
                        "encoding": "base64",
                        "content": base64.b64encode(b"# Example project").decode(),
                    }
                )

                level = await collector.get_cii_badge_level("owner", "repo")

                assert level == "none"
            finally:
                await collector.close()

        asyncio.run(run())


class TestPerFamilySeams:
    """The v0.10.1 phase 3 step 2 refactor split ``collect()`` into
    per-signal-family methods so a future cache layer can refresh just
    one family on its own cadence (commits push-coupled, sponsors
    weekly, CII rare, etc.). These tests pin that the family methods
    exist as public callables on ``GitHubCollector``, that they
    populate the right slice of ``GitHubData``, and that the
    orchestrator ``collect()`` composes them in the documented order.

    These are CONTRACT tests, not implementation tests — they don't
    care how the methods fetch data, they just care that the seam
    exists and is stable. A future per-family cache integration
    (re-enabling the freshness probe safely) depends on this contract."""

    def test_per_family_methods_are_public_on_collector(self):
        """Future per-family cache integration calls these methods
        directly. If any of them gets renamed or made private, the
        cache integration breaks silently — pin the names."""
        for name in (
            "collect_repo_meta",
            "resolve_maintainer",
            "collect_maintainer_profile",
            "collect_org_admins_family",
            "collect_cii_family",
            "collect_issues_family",
        ):
            assert hasattr(GitHubCollector, name), (
                f"GitHubCollector.{name} is part of the per-family "
                f"refresh seam — see services/repo_cache.py phase-3 "
                f"step 3 commit message about why item 2 is the "
                f"precondition for re-enabling the freshness probe."
            )

    def test_collect_repo_meta_populates_owner_type_and_canonical(self):
        """Family 1: repo metadata. Returns canonical (owner, repo)
        and populates owner_type on the data object."""
        from ossuary.collectors.github import GitHubData
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                collector.get_repo_info = AsyncMock(return_value={
                    "owner": {"type": "Organization", "login": "PSF"},
                    "name": "Requests",
                })
                data = GitHubData(owner="psf", repo="requests")
                owner, repo, info = await collector.collect_repo_meta(
                    "psf", "requests", data,
                )
                assert owner == "PSF"
                assert repo == "Requests"
                assert data.owner == "PSF"
                assert data.repo == "Requests"
                assert data.owner_type == "Organization"
                assert info is not None
            finally:
                await collector.close()
        asyncio.run(run())

    def test_collect_repo_meta_records_essential_failure(self):
        """If repo_info comes back None (transient — not 404), the
        family records an *essential* failure so upstream marks
        INSUFFICIENT_DATA."""
        from ossuary.collectors.github import GitHubData
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                collector.get_repo_info = AsyncMock(return_value=None)
                collector.last_error = "HTTP 503"
                data = GitHubData(owner="x", repo="y")
                await collector.collect_repo_meta("x", "y", data)
                assert any("repo_info" in e for e in data.fetch_errors)
            finally:
                await collector.close()
        asyncio.run(run())

    def test_resolve_maintainer_priority_provided_username_wins(self):
        """Maintainer resolution priority order is the contract. Top-
        priority is the explicitly-provided username from git history."""
        from ossuary.collectors.github import GitHubData
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                # Should never be called when top_contributor_username given.
                collector.get_repo_contributors = AsyncMock(
                    side_effect=AssertionError(
                        "must not call API when username provided"
                    )
                )
                data = GitHubData(owner="acme", repo="widget")
                resolved = await collector.resolve_maintainer(
                    "acme", "widget", data,
                    top_contributor_username="alice",
                    top_contributor_email=None,
                    repo_info={"owner": {"login": "acme"}},
                )
                assert resolved == "alice"
                assert data.maintainer_username == "alice"
            finally:
                await collector.close()
        asyncio.run(run())

    def test_collect_maintainer_profile_aggregates_signals(self):
        """Profile family populates account-age, public-repo count,
        repo list (for reputation), sponsor state, orgs."""
        from ossuary.collectors.github import GitHubData
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                collector.get_user = AsyncMock(return_value={
                    "created_at": "2018-01-01T00:00:00Z",
                    "public_repos": 42,
                })
                collector.get_user_repos = AsyncMock(return_value=[
                    {"name": "a", "stargazers_count": 100},
                    {"name": "b", "stargazers_count": 250},
                ])
                collector.get_sponsors_status = AsyncMock(return_value=False)
                collector.get_user_orgs = AsyncMock(return_value=["acme"])

                data = GitHubData(owner="acme", repo="widget")
                await collector.collect_maintainer_profile("alice", data)

                assert data.maintainer_account_created == "2018-01-01T00:00:00Z"
                assert data.maintainer_public_repos == 42
                assert data.maintainer_total_stars == 350
                assert data.has_github_sponsors is False
                assert data.maintainer_orgs == ["acme"]
            finally:
                await collector.close()
        asyncio.run(run())

    def test_collect_org_admins_family_sets_org_fields(self):
        """Org-admins family populates is_org_owned + org_admin_count
        without touching maintainer fields (so per-family refresh of
        org status doesn't blow away maintainer signals)."""
        from ossuary.collectors.github import GitHubData
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                collector.get_org_admins = AsyncMock(return_value={
                    "is_org": True, "admin_count": 7,
                })
                data = GitHubData(
                    owner="acme", repo="widget",
                    maintainer_username="alice",  # would survive refresh
                    maintainer_total_stars=999,
                )
                await collector.collect_org_admins_family(
                    "acme", "widget", data,
                )
                assert data.is_org_owned is True
                assert data.org_admin_count == 7
                # Maintainer fields untouched.
                assert data.maintainer_username == "alice"
                assert data.maintainer_total_stars == 999
            finally:
                await collector.close()
        asyncio.run(run())

    def test_collect_issues_family_populates_issues(self):
        """Issues family populates data.issues independently — required
        so a future cache layer can refresh issues on a different
        cadence than the rest of the GitHub data."""
        from ossuary.collectors.github import GitHubData, IssueData
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                fake_issue = IssueData(
                    number=1, title="t", body="b", state="open",
                    is_pull_request=False, author_login="x",
                    created_at="2026-01-01", updated_at="2026-01-02",
                    closed_at=None, comments=[],
                )
                collector.get_issues = AsyncMock(return_value=[fake_issue])

                data = GitHubData(
                    owner="acme", repo="widget",
                    is_org_owned=True,  # would survive refresh
                )
                await collector.collect_issues_family(
                    "acme", "widget", data,
                )
                assert data.issues == [fake_issue]
                # Org field untouched.
                assert data.is_org_owned is True
            finally:
                await collector.close()
        asyncio.run(run())
