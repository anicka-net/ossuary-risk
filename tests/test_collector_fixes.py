"""Regression tests for the June 2026 collector-bug sweep.

Covers the score-corrupting collector bugs found in the full-repo
review: stale clones (fetch without fast-forward), the merged-PR
GraphQL window fetching the *oldest* PRs, deleted-account issue
authors, and the substring ``.git`` strip mangling repo names.
"""

import asyncio
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from ossuary.collectors.git import GitCollector
from ossuary.collectors.github import GitHubCollector


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "Tester",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Tester",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "HOME": str(repo.parent),
            "PATH": "/usr/bin:/bin",
        },
    )


class TestCloneOrUpdateFreshness:
    """fetch() alone only updates refs/remotes/*; `git log` reads HEAD.

    Without the fast-forward after fetch, every previously cloned repo
    serves history frozen at first-clone time — commits_last_year decays
    toward zero and abandonment signals fire spuriously.
    """

    def test_update_picks_up_new_upstream_commits(self, tmp_path):
        origin = tmp_path / "origin"
        origin.mkdir()
        _git(origin, "init", "-b", "main")
        (origin / "a.txt").write_text("one\n")
        _git(origin, "add", "a.txt")
        _git(origin, "commit", "-m", "first")

        collector = GitCollector(repos_path=str(tmp_path / "repos"))
        repo_path = collector.clone_or_update(str(origin))
        assert len(collector.extract_commits(repo_path)) == 1

        (origin / "b.txt").write_text("two\n")
        _git(origin, "add", "b.txt")
        _git(origin, "commit", "-m", "second")

        repo_path = collector.clone_or_update(str(origin))
        commits = collector.extract_commits(repo_path)
        assert len(commits) == 2, (
            "clone_or_update must fast-forward the local branch; "
            "fetch alone leaves `git log` reading stale history"
        )


class TestRepoPathNaming:
    def test_github_pages_repo_name_not_mangled(self, tmp_path):
        collector = GitCollector(repos_path=str(tmp_path))
        path = collector._get_repo_path("https://github.com/foo/foo.github.io")
        assert path.name.startswith("foo.github.io_")

    def test_trailing_git_suffix_stripped(self, tmp_path):
        collector = GitCollector(repos_path=str(tmp_path))
        path = collector._get_repo_path("https://github.com/foo/bar.git")
        assert path.name.startswith("bar_")


class TestWeightedConcentration:
    def test_uses_largest_weighted_identity_not_unweighted_top(self):
        from ossuary.collectors.git import CommitData

        cutoff = datetime(2026, 1, 1)

        def commit(sha, name, email, date):
            return CommitData(
                sha=sha,
                author_name=name,
                author_email=email,
                authored_date=date,
                committer_name=name,
                committer_email=email,
                committed_date=date,
                message="test",
            )

        commits = [
            commit(str(i), "Alice", "alice@example.com",
                   cutoff - timedelta(days=340))
            for i in range(10)
        ] + [
            commit(str(i + 10), "Bob", "bob@example.com",
                   cutoff - timedelta(days=30))
            for i in range(8)
        ]

        metrics = GitCollector().calculate_metrics(commits, cutoff_date=cutoff)

        assert metrics.top_contributor_email == "alice@example.com"
        assert metrics.maintainer_concentration > 50


class TestParseRepoUrlEdgeCases:
    def test_github_pages_repo_preserved(self):
        owner, repo = GitHubCollector.parse_repo_url(
            "https://github.com/foo/foo.github.io"
        )
        assert (owner, repo) == ("foo", "foo.github.io")

    def test_github_pages_repo_with_git_suffix(self):
        owner, repo = GitHubCollector.parse_repo_url(
            "https://github.com/foo/foo.github.io.git"
        )
        assert (owner, repo) == ("foo", "foo.github.io")

    def test_query_string_stripped_from_repo_name(self):
        owner, repo = GitHubCollector.parse_repo_url(
            "https://github.com/owner/repo?tab=readme"
        )
        assert (owner, repo) == ("owner", "repo")


class TestMergeConcentrationWindow:
    """`last: 100` with DESC ordering takes the connection tail — the
    *least* recently updated merged PRs. The query must use `first:` so
    the v6.4 merge-author signals describe current governance."""

    def test_graphql_query_uses_first_not_last(self):
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                captured = {}

                async def fake_graphql(query, variables=None, _rotated=False):
                    captured["query"] = query
                    return None

                collector._graphql = fake_graphql
                await collector.get_merge_concentration("owner", "repo")
            finally:
                await collector.close()

            assert "first: 100" in captured["query"]
            assert "last: 100" not in captured["query"]

        asyncio.run(run())


class TestComputeMergeStats:
    """Merge aggregates can be inspected on raw or diagnostic subsets.

    Historical scoring deliberately neutralizes the bounded current sample;
    the cutoff option remains useful for demonstrating why a filtered slice is
    not an adequate reconstruction.
    """

    def _sample(self):
        # 30 merges across 3 humans, Jan..Sep 2024; bot noise mixed in.
        prs = [
            {"login": f"user{i % 3}", "merged_at": f"2024-0{(i % 9) + 1}-01T00:00:00Z"}
            for i in range(30)
        ]
        prs.append({"login": "dependabot[bot]", "merged_at": "2024-05-01T00:00:00Z"})
        return prs

    def test_no_cutoff_uses_full_sample(self):
        from ossuary.collectors.github import compute_merge_stats

        stats = compute_merge_stats(self._sample())
        assert stats["merges_analyzed"] == 30  # bot excluded
        assert stats["merge_bus_factor"] == 2

    def test_cutoff_excludes_later_merges(self):
        from datetime import datetime

        from ossuary.collectors.github import compute_merge_stats

        stats = compute_merge_stats(self._sample(), cutoff=datetime(2024, 3, 15))
        assert stats["merges_analyzed"] == 12  # Jan-Mar only

    def test_below_min_sample_returns_unavailable(self):
        from datetime import datetime

        from ossuary.collectors.github import compute_merge_stats

        stats = compute_merge_stats(self._sample(), cutoff=datetime(2024, 1, 15))
        assert stats["merge_bus_factor"] == 0
        assert stats["merges_analyzed"] == 0

    def test_missing_timestamp_excluded_when_cutoff_given(self):
        from datetime import datetime

        from ossuary.collectors.github import compute_merge_stats

        prs = [{"login": "u1", "merged_at": ""}] * 20
        stats = compute_merge_stats(prs, cutoff=datetime(2024, 6, 1))
        assert stats["merge_bus_factor"] == 0


class TestIssuesDeletedAuthor:
    """GitHub serves `"user": null` for deleted accounts; that must not
    crash get_issues (a crash dumps the whole GitHub family into the
    degraded fallback path for the package)."""

    def test_null_user_does_not_crash(self):
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                collector._get = AsyncMock(
                    return_value=[
                        {
                            "number": 1,
                            "title": "ghost issue",
                            "body": "",
                            "state": "open",
                            "user": None,
                            "created_at": "2024-01-01T00:00:00Z",
                            "updated_at": "2024-01-02T00:00:00Z",
                            "closed_at": None,
                            "comments": 0,
                        }
                    ]
                )
                issues = await collector.get_issues("owner", "repo")
            finally:
                await collector.close()

            assert len(issues) == 1
            assert issues[0].author_login == ""

        asyncio.run(run())

    def test_partial_comment_failure_survives_later_success(self):
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                issues = [
                    {
                        "number": 1, "title": "one", "body": "",
                        "state": "open", "user": {"login": "u"},
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-02T00:00:00Z",
                        "closed_at": None, "comments": 1,
                    },
                    {
                        "number": 2, "title": "two", "body": "",
                        "state": "open", "user": {"login": "u"},
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-02T00:00:00Z",
                        "closed_at": None, "comments": 1,
                    },
                ]

                async def fake_get(endpoint, params=None):
                    if endpoint.endswith("/issues"):
                        collector.last_error = None
                        return issues
                    if endpoint.endswith("/1/comments"):
                        collector.last_error = "HTTP 502 from api.github.com"
                        return None
                    collector.last_error = None
                    return [{"id": 2, "user": {"login": "u"}, "body": "ok"}]

                collector._get = fake_get
                result = await collector.get_issues("owner", "repo")
                assert len(result) == 2
                assert "HTTP 502" in (collector.last_error or "")
            finally:
                await collector.close()

        asyncio.run(run())


class TestOrgMembersDeterministic:
    """role=admin is only honoured for org-member tokens; the collector
    must query public members (per_page=100, no role param) so every
    token sees the same count."""

    def test_queries_public_members_not_role_admin(self):
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                calls = []

                async def fake_get(endpoint, params=None):
                    calls.append((endpoint, params or {}))
                    if endpoint.startswith("/repos/"):
                        return {"owner": {"type": "Organization"}}
                    return [{"login": f"u{i}"} for i in range(5)]

                collector._get = fake_get
                collector.get_repo_info = AsyncMock(
                    return_value={"owner": {"type": "Organization"}}
                )
                result = await collector.get_org_admins("org", "repo")
            finally:
                await collector.close()

            member_calls = [c for c in calls if c[0] == "/orgs/org/members"]
            assert member_calls, "must hit the members endpoint"
            assert member_calls[0][1].get("role") is None
            assert member_calls[0][1].get("per_page") == 100
            assert result == {"is_org": True, "admin_count": 5}

        asyncio.run(run())
