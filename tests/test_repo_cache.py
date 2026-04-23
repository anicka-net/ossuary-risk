"""Tests for the snapshot cache layer (services/repo_cache.py).

Covers serde round-trip on real CollectedData shapes and the get/store
contract on the SQLAlchemy model.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ossuary.collectors.git import CommitData
from ossuary.collectors.github import GitHubData, IssueData
from ossuary.db.models import Base, RepoSnapshot
from ossuary.services.repo_cache import (
    COLLECTOR_VERSION,
    RepoSnapshotCache,
    canonicalize_repo_url,
    deserialise_collected_data,
    serialise_collected_data,
)
from ossuary.services.scorer import CollectedData


@pytest.fixture
def session():
    """In-memory SQLite session per test."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()


def _make_collected_data(commit_count: int = 3) -> CollectedData:
    """Build a representative CollectedData for serde tests."""
    base_time = datetime(2026, 1, 1, 12, 0, 0)
    commits = [
        CommitData(
            sha=f"sha{i:040d}",
            author_name=f"Author {i}",
            author_email=f"author{i}@example.com",
            authored_date=base_time + timedelta(days=i),
            committer_name=f"Committer {i}",
            committer_email=f"committer{i}@example.com",
            committed_date=base_time + timedelta(days=i, hours=1),
            message=f"Commit {i}",
        )
        for i in range(commit_count)
    ]
    issue = IssueData(
        number=42,
        title="An issue",
        body="With some body text",
        state="open",
        is_pull_request=False,
        author_login="someuser",
        created_at="2026-02-01T10:00:00Z",
        updated_at="2026-02-02T11:00:00Z",
        closed_at=None,
        comments=[
            {"author_login": "responder", "body": "thanks", "created_at": "2026-02-01T11:00:00Z"},
        ],
    )
    github_data = GitHubData(
        owner="acme",
        repo="widget",
        owner_type="Organization",
        maintainer_username="alice",
        maintainer_account_created="2018-04-01T00:00:00Z",
        maintainer_repos=[{"name": "widget", "stars": 100}],
        is_org_owned=True,
        issues=[issue],
    )
    return CollectedData(
        repo_url="https://github.com/acme/widget",
        all_commits=commits,
        github_data=github_data,
        weekly_downloads=12345,
        maintainer_account_created=datetime(2018, 4, 1),
        repo_stargazers=100,
        fetch_errors=[],
        provisional_reasons=[],
    )


# ---------------------------------------------------------------------------
# Serde
# ---------------------------------------------------------------------------

class TestSerde:
    def test_serialise_returns_jsonable_dict(self):
        data = _make_collected_data()
        blob = serialise_collected_data(data)

        assert isinstance(blob, dict)
        # Datetime fields must have been converted to ISO strings.
        assert isinstance(blob["all_commits"][0]["authored_date"], str)
        assert isinstance(blob["maintainer_account_created"], str)

    def test_round_trip_preserves_shape(self):
        original = _make_collected_data(commit_count=5)
        blob = serialise_collected_data(original)
        restored = deserialise_collected_data(blob, CollectedData)

        assert restored.repo_url == original.repo_url
        assert restored.weekly_downloads == original.weekly_downloads
        assert restored.repo_stargazers == original.repo_stargazers
        assert len(restored.all_commits) == len(original.all_commits)
        assert restored.fetch_errors == original.fetch_errors
        assert restored.provisional_reasons == original.provisional_reasons

    def test_round_trip_preserves_commit_datetimes(self):
        original = _make_collected_data(commit_count=2)
        blob = serialise_collected_data(original)
        restored = deserialise_collected_data(blob, CollectedData)

        # Datetimes must come back as datetime instances, not strings.
        for orig_commit, restored_commit in zip(original.all_commits, restored.all_commits):
            assert isinstance(restored_commit.authored_date, datetime)
            assert restored_commit.authored_date == orig_commit.authored_date
            assert isinstance(restored_commit.committed_date, datetime)

    def test_round_trip_preserves_github_issues(self):
        original = _make_collected_data()
        blob = serialise_collected_data(original)
        restored = deserialise_collected_data(blob, CollectedData)

        assert len(restored.github_data.issues) == 1
        issue = restored.github_data.issues[0]
        assert issue.number == 42
        assert issue.title == "An issue"
        assert issue.author_login == "someuser"
        assert issue.comments[0]["author_login"] == "responder"

    def test_round_trip_preserves_top_level_datetime(self):
        original = _make_collected_data()
        blob = serialise_collected_data(original)
        restored = deserialise_collected_data(blob, CollectedData)

        assert isinstance(restored.maintainer_account_created, datetime)
        assert restored.maintainer_account_created == original.maintainer_account_created

    def test_serialise_rejects_non_dataclass(self):
        with pytest.raises(TypeError):
            serialise_collected_data({"not": "a dataclass"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cache contract
# ---------------------------------------------------------------------------

class TestRepoSnapshotCache:
    def test_miss_when_no_snapshot_exists(self, session):
        cache = RepoSnapshotCache(session)
        assert cache.get_snapshot_for_cutoff("anything", "npm") is None

    def test_store_and_retrieve_round_trip(self, session):
        cache = RepoSnapshotCache(session)
        data = _make_collected_data()
        blob = serialise_collected_data(data)

        cache.store_snapshot(
            name="widget",
            ecosystem="npm",
            repo_url="https://github.com/acme/widget",
            blob=blob,
        )
        session.commit()

        snapshot = cache.get_snapshot_for_cutoff("widget", "npm")
        assert snapshot is not None
        assert snapshot.repo_url == "https://github.com/acme/widget"
        assert snapshot.fetcher_version == COLLECTOR_VERSION

    def test_coverage_until_derived_from_latest_commit(self, session):
        cache = RepoSnapshotCache(session)
        data = _make_collected_data(commit_count=3)
        blob = serialise_collected_data(data)
        latest_commit_date = max(c.authored_date for c in data.all_commits)

        cache.store_snapshot("widget", "npm", "https://example", blob)
        session.commit()

        snapshot = cache.get_snapshot_for_cutoff("widget", "npm")
        assert snapshot.coverage_until == latest_commit_date

    def test_historical_cutoff_after_collected_at_returns_none(self, session):
        """A snapshot taken before the cutoff cannot satisfy that cutoff —
        it doesn't yet contain the data that existed at the cutoff date."""
        cache = RepoSnapshotCache(session)
        data = _make_collected_data()
        blob = serialise_collected_data(data)

        cache.store_snapshot(
            "widget", "npm", "https://example", blob,
            collected_at=datetime(2026, 1, 1),
        )
        session.commit()

        # Cutoff is after the snapshot was collected — snapshot is too old.
        future_cutoff = datetime(2026, 6, 1)
        assert cache.get_snapshot_for_cutoff("widget", "npm", future_cutoff) is None

    def test_historical_cutoff_before_collected_at_returns_snapshot(self, session):
        """A snapshot taken after the cutoff contains everything up to the
        cutoff (later activity is filtered at scoring time)."""
        cache = RepoSnapshotCache(session)
        data = _make_collected_data(commit_count=5)
        blob = serialise_collected_data(data)

        cache.store_snapshot(
            "widget", "npm", "https://example", blob,
            collected_at=datetime(2026, 4, 1),
        )
        session.commit()

        past_cutoff = datetime(2025, 6, 1)  # collected_at >= cutoff
        snapshot = cache.get_snapshot_for_cutoff("widget", "npm", past_cutoff)
        assert snapshot is not None

    def test_current_scoring_serves_recent_snapshot(self, session):
        """When cutoff_date is None, a snapshot fresher than the SLA is served
        even if its commits are years old (governance signals are structural,
        not commit-frequency)."""
        cache = RepoSnapshotCache(session)
        # The blob's commits are all in early 2026; ``coverage_until`` will
        # be that. The snapshot itself was collected today.
        data = _make_collected_data()
        blob = serialise_collected_data(data)
        cache.store_snapshot(
            "widget", "npm", "https://example", blob,
            collected_at=datetime.utcnow(),
        )
        session.commit()

        snapshot = cache.get_snapshot_for_cutoff("widget", "npm", cutoff_date=None)
        assert snapshot is not None, (
            "Current-scoring path must serve a freshly-collected snapshot "
            "regardless of how stale the commit history is. Regression of "
            "the GPT-flagged Bug 2."
        )

    def test_current_scoring_rejects_expired_snapshot(self, session):
        """A snapshot older than the SLA (90 days) is not served on the
        current-scoring path — caller must refetch."""
        from datetime import timedelta
        cache = RepoSnapshotCache(session)
        data = _make_collected_data()
        blob = serialise_collected_data(data)
        cache.store_snapshot(
            "widget", "npm", "https://example", blob,
            collected_at=datetime.utcnow() - timedelta(days=120),
        )
        session.commit()

        assert cache.get_snapshot_for_cutoff(
            "widget", "npm", cutoff_date=None
        ) is None

    def test_current_scoring_serves_within_custom_sla(self, session):
        """The SLA is parameterisable — used by tests and by per-deployment
        tightening if needed."""
        from datetime import timedelta
        cache = RepoSnapshotCache(session)
        data = _make_collected_data()
        blob = serialise_collected_data(data)
        cache.store_snapshot(
            "widget", "npm", "https://example", blob,
            collected_at=datetime.utcnow() - timedelta(days=10),
        )
        session.commit()

        # Default 90-day SLA: served.
        assert cache.get_snapshot_for_cutoff("widget", "npm", None) is not None
        # Tighter 5-day SLA: rejected.
        assert cache.get_snapshot_for_cutoff(
            "widget", "npm", None, sla_expired_days=5,
        ) is None

    def test_get_returns_most_recent_snapshot(self, session):
        cache = RepoSnapshotCache(session)
        blob = serialise_collected_data(_make_collected_data())

        cache.store_snapshot("widget", "npm", "https://example", blob, collected_at=datetime(2026, 1, 1))
        cache.store_snapshot("widget", "npm", "https://example", blob, collected_at=datetime(2026, 4, 1))
        cache.store_snapshot("widget", "npm", "https://example", blob, collected_at=datetime(2026, 2, 1))
        session.commit()

        snapshot = cache.get_snapshot_for_cutoff("widget", "npm")
        assert snapshot.collected_at == datetime(2026, 4, 1)

    def test_fetcher_version_mismatch_invalidates(self, session):
        """An older fetcher_version row must not be served."""
        cache = RepoSnapshotCache(session)
        package = cache._get_or_create_package("widget", "npm", "https://example")
        session.add(
            RepoSnapshot(
                package_id=package.id,
                collected_at=datetime.utcnow(),
                coverage_until=datetime.utcnow(),
                repo_url="https://example",
                blob={},
                fetcher_version=COLLECTOR_VERSION + 99,  # future / mismatched
            )
        )
        session.commit()

        assert cache.get_snapshot_for_cutoff("widget", "npm") is None

    def test_pypi_name_normalisation(self, session):
        """``Foo_Bar`` and ``foo-bar`` resolve to the same PyPI snapshot."""
        cache = RepoSnapshotCache(session)
        blob = serialise_collected_data(_make_collected_data())

        cache.store_snapshot("Foo_Bar", "pypi", "https://example", blob)
        session.commit()

        # Same canonical lookup
        assert cache.get_snapshot_for_cutoff("foo-bar", "pypi") is not None
        assert cache.get_snapshot_for_cutoff("FOO.BAR", "pypi") is not None

    def test_npm_case_sensitive_separate_packages(self, session):
        """npm scoped packages keep case — must not collide on lookup."""
        cache = RepoSnapshotCache(session)
        blob = serialise_collected_data(_make_collected_data())

        cache.store_snapshot("react", "npm", "https://example", blob)
        session.commit()

        assert cache.get_snapshot_for_cutoff("React", "npm") is None


# ---------------------------------------------------------------------------
# Negative cache
# ---------------------------------------------------------------------------

class TestNegativeCache:
    def test_no_cache_returns_none(self, session):
        """A package never seen has no negative cache."""
        cache = RepoSnapshotCache(session)
        assert cache.get_negative_cache("never-seen", "npm") is None

    def test_store_and_retrieve_within_ttl(self, session):
        cache = RepoSnapshotCache(session)
        cache.store_negative("dead-pkg", "npm", "Repository not found: https://example")
        session.commit()

        cached = cache.get_negative_cache("dead-pkg", "npm")
        assert cached == "Repository not found: https://example"

    def test_ttl_expires(self, session):
        """A negative-cache entry older than its TTL must not be returned."""
        from datetime import timedelta

        cache = RepoSnapshotCache(session)
        package = cache._get_or_create_package("expired-pkg", "npm")
        package.last_failed_at = datetime.utcnow() - timedelta(days=120)
        package.failure_reason = "Repository not found"
        session.commit()

        # 90-day TTL for "not found" — 120 days is past expiry.
        assert cache.get_negative_cache("expired-pkg", "npm") is None

    def test_no_repo_url_uses_shorter_ttl(self, session):
        """Registry-has-no-repo failures use the 30-day TTL, not 90."""
        from datetime import timedelta

        cache = RepoSnapshotCache(session)
        package = cache._get_or_create_package("no-repo", "npm")
        package.last_failed_at = datetime.utcnow() - timedelta(days=45)
        package.failure_reason = "Package 'no-repo' not found on npm (no repository URL)"
        session.commit()

        # 45 days > 30-day no-repo-field TTL → expired.
        assert cache.get_negative_cache("no-repo", "npm") is None

    def test_clear_negative_removes_cache(self, session):
        cache = RepoSnapshotCache(session)
        cache.store_negative("recovered", "npm", "Repository not found")
        session.commit()
        assert cache.get_negative_cache("recovered", "npm") is not None

        cache.clear_negative("recovered", "npm")
        session.commit()
        assert cache.get_negative_cache("recovered", "npm") is None


# ---------------------------------------------------------------------------
# is_permanent_failure
# ---------------------------------------------------------------------------

class TestIsPermanentFailure:
    """Classification of collection failures into 'cache forever-ish' vs
    'retry next time' is the hinge of the negative cache. Get this wrong
    and we either wastefully re-probe permanent 404s every run, or
    permanently cache transient rate-limit failures and never see the
    package again."""

    def _check(self, text):
        from ossuary.services.repo_cache import is_permanent_failure
        return is_permanent_failure(text)

    def test_repository_not_found_is_permanent(self):
        assert self._check("Repository not found: https://github.com/foo/bar")

    def test_no_repository_url_is_permanent(self):
        assert self._check("Package 'foo' not found on npm (no repository URL)")

    def test_unsupported_ecosystem_is_permanent(self):
        assert self._check("Unsupported ecosystem: cool-new-thing")

    def test_rate_limit_is_transient(self):
        assert not self._check("pypi.weekly_downloads: HTTP 429 (rate limited)")

    def test_5xx_is_transient(self):
        assert not self._check("github.com returned 503 service unavailable")

    def test_insufficient_data_is_transient(self):
        assert not self._check("INSUFFICIENT_DATA: pypi.weekly_downloads: HTTP 429")

    def test_empty_text_is_not_permanent(self):
        assert not self._check("")
        assert not self._check(None)

    def test_unknown_text_defaults_to_transient(self):
        """Default to NOT caching on unknown failure shapes — better to
        re-probe than to silently lose a package forever."""
        assert not self._check("Some weird new error nobody planned for")


# ---------------------------------------------------------------------------
# Stats / introspection
# ---------------------------------------------------------------------------

class TestStats:
    def test_stats_on_empty_cache(self, session):
        cache = RepoSnapshotCache(session)
        stats = cache.stats()

        assert stats["snapshots"]["total"] == 0
        assert stats["snapshots"]["unique_packages"] == 0
        assert stats["negative_cache"]["active"] == 0
        assert stats["negative_cache"]["total_recorded"] == 0
        assert stats["sla"]["fresh_days"] == 30
        assert stats["sla"]["expired_days"] == 90

    def test_stats_classifies_snapshots_by_sla_band(self, session):
        from datetime import timedelta

        cache = RepoSnapshotCache(session)
        blob = serialise_collected_data(_make_collected_data())
        now = datetime.utcnow()

        cache.store_snapshot("fresh-pkg", "npm", "https://x", blob,
                             collected_at=now - timedelta(days=10))
        cache.store_snapshot("stale-pkg", "npm", "https://x", blob,
                             collected_at=now - timedelta(days=60))
        cache.store_snapshot("expired-pkg", "npm", "https://x", blob,
                             collected_at=now - timedelta(days=120))
        session.commit()

        stats = cache.stats()
        assert stats["snapshots"]["fresh"] == 1
        assert stats["snapshots"]["stale"] == 1
        assert stats["snapshots"]["expired"] == 1
        assert stats["snapshots"]["unique_packages"] == 3

    def test_stats_counts_negative_cache(self, session):
        cache = RepoSnapshotCache(session)
        cache.store_negative("dead-1", "npm", "Repository not found")
        cache.store_negative("dead-2", "pypi", "Package not found (no repository URL)")
        session.commit()

        stats = cache.stats()
        # Both within their respective TTLs — both active.
        assert stats["negative_cache"]["active"] == 2
        assert stats["negative_cache"]["total_recorded"] == 2

    def test_stats_excludes_expired_negative_from_active(self, session):
        """Regression for the GPT-flagged stats overcount: an expired
        negative-cache row should NOT appear in the 'active' count, only
        in 'total_recorded'.

        The earlier impl counted every non-null failure row as active, so
        operators would see ``cache-stats`` overstate how many packages
        the cache was actually skipping."""
        from datetime import timedelta

        cache = RepoSnapshotCache(session)
        # Within TTL
        cache.store_negative("dead-fresh", "npm", "Repository not found")
        # Past 90-day TTL for "not found" — should be inactive.
        package = cache._get_or_create_package("dead-stale", "npm")
        package.last_failed_at = datetime.utcnow() - timedelta(days=120)
        package.failure_reason = "Repository not found"
        session.commit()

        stats = cache.stats()
        assert stats["negative_cache"]["active"] == 1, (
            "Expired negative-cache rows must not be counted as active. "
            "Regression of the GPT-flagged overcount."
        )
        assert stats["negative_cache"]["total_recorded"] == 2

        # And the inactive row indeed doesn't get served.
        assert cache.get_negative_cache("dead-stale", "npm") is None
        assert cache.get_negative_cache("dead-fresh", "npm") is not None

    def test_stats_per_class_ttls_apply_correctly(self, session):
        """The 'no repo URL' class uses 30-day TTL; the 'not found' class
        uses 90-day. Stats must split-apply these to active counts."""
        from datetime import timedelta

        cache = RepoSnapshotCache(session)
        # 'no repo URL' at 45 days — past its 30-day TTL → inactive.
        cache.store_negative(
            "no-repo-stale", "npm",
            "Package 'no-repo-stale' not found on npm (no repository URL)",
        )
        # 'not found' (repo) at 45 days — within its 90-day TTL → active.
        cache.store_negative(
            "dead-mid", "npm",
            "Repository not found: https://example",
        )
        # Backdate both to 45 days ago so the active/inactive split fires.
        for pkg_name in ("no-repo-stale", "dead-mid"):
            from ossuary.db.models import Package
            p = session.query(Package).filter(Package.name == pkg_name).first()
            p.last_failed_at = datetime.utcnow() - timedelta(days=45)
        session.commit()

        stats = cache.stats()
        assert stats["negative_cache"]["active"] == 1
        assert stats["negative_cache"]["total_recorded"] == 2

    def test_stats_classifies_uppercase_no_repo_url_correctly(self, session):
        """The collector's actual emitted text is mixed-case
        ("no repository URL" with uppercase URL). With the typed
        failure_kind column the case-folding happens once at write time
        in ``classify_failure``, so SQL stays a clean equality filter
        instead of needing func.lower() / LIKE for portability — the
        v0.10 GPT-review regression class can no longer recur."""
        from datetime import timedelta

        cache = RepoSnapshotCache(session)
        cache.store_negative(
            "uppercase-url", "npm",
            "Package 'uppercase-url' not found on npm (no repository URL)",
        )
        from ossuary.db.models import Package
        p = session.query(Package).filter(Package.name == "uppercase-url").first()
        p.last_failed_at = datetime.utcnow() - timedelta(days=45)
        session.commit()

        # Verify the classifier wrote the typed kind (not just the text).
        from ossuary.services.repo_cache import FailureKind
        assert p.failure_kind == FailureKind.NO_REPO_URL

        stats = cache.stats()
        # 45 days > 30-day TTL for NO_REPO_URL → inactive.
        assert stats["negative_cache"]["active"] == 0
        assert stats["negative_cache"]["total_recorded"] == 1

    def test_stats_separates_wrong_collector_version(self, session):
        """Snapshots from an old collector schema show up under their own
        bucket so operators know to re-collect after a collector bump."""
        cache = RepoSnapshotCache(session)
        package = cache._get_or_create_package("old-snap", "npm")
        session.add(
            RepoSnapshot(
                package_id=package.id,
                collected_at=datetime.utcnow(),
                coverage_until=datetime.utcnow(),
                repo_url="https://x",
                blob={},
                fetcher_version=COLLECTOR_VERSION + 99,
            )
        )
        session.commit()

        stats = cache.stats()
        assert stats["snapshots"]["wrong_collector_version"] == 1
        assert stats["snapshots"]["fresh"] == 0
        assert stats["snapshots"]["stale"] == 0
        assert stats["snapshots"]["expired"] == 0


# ---------------------------------------------------------------------------
# Canonical URL + repo-keyed lookup (v0.10.1 — phase 3 step 1 narrow slice)
# ---------------------------------------------------------------------------

class TestCanonicalizeRepoUrl:
    def test_returns_none_for_empty_input(self):
        assert canonicalize_repo_url(None) is None
        assert canonicalize_repo_url("") is None
        assert canonicalize_repo_url("   ") is None

    def test_strips_trailing_dot_git(self):
        assert (
            canonicalize_repo_url("https://github.com/Acme/Widget.git")
            == "https://github.com/acme/widget"
        )

    def test_strips_trailing_slash(self):
        assert (
            canonicalize_repo_url("https://github.com/acme/widget/")
            == "https://github.com/acme/widget"
        )

    def test_lowercases_owner_and_repo(self):
        # GitHub treats owner/repo as case-insensitive — the cache should
        # too, otherwise ``Axios-Http/Axios`` and ``axios-http/axios``
        # would write two separate snapshots for the same repo.
        assert (
            canonicalize_repo_url("https://github.com/Axios-Http/Axios")
            == "https://github.com/axios-http/axios"
        )

    def test_promotes_http_to_https(self):
        assert (
            canonicalize_repo_url("http://github.com/acme/widget")
            == "https://github.com/acme/widget"
        )

    def test_normalises_ssh_form(self):
        assert (
            canonicalize_repo_url("git@github.com:Acme/Widget.git")
            == "https://github.com/acme/widget"
        )

    def test_idempotent(self):
        once = canonicalize_repo_url("https://github.com/Acme/Widget.git/")
        twice = canonicalize_repo_url(once)
        assert once == twice == "https://github.com/acme/widget"


class TestSnapshotByRepoUrl:
    def test_finds_snapshot_written_by_different_package(self, session):
        """The whole point of repo-keying: a snapshot written by package A
        on npm is reachable by a github-direct lookup for the same repo."""
        cache = RepoSnapshotCache(session)
        blob = serialise_collected_data(_make_collected_data())
        cache.store_snapshot(
            name="widget",
            ecosystem="npm",
            repo_url="https://github.com/Acme/Widget",
            blob=blob,
        )
        session.commit()

        snap = cache.get_snapshot_by_repo_url("https://github.com/acme/widget")
        assert snap is not None
        assert canonicalize_repo_url(snap.repo_url) == "https://github.com/acme/widget"

    def test_canonicalises_caller_url(self, session):
        """Differently-spelled equivalent URLs all hit the same snapshot."""
        cache = RepoSnapshotCache(session)
        blob = serialise_collected_data(_make_collected_data())
        cache.store_snapshot(
            name="widget", ecosystem="npm",
            repo_url="https://github.com/acme/widget", blob=blob,
        )
        session.commit()

        for query in (
            "https://github.com/acme/widget",
            "https://github.com/acme/widget/",
            "https://github.com/acme/widget.git",
            "https://github.com/Acme/Widget",
            "git@github.com:acme/widget.git",
            "http://github.com/acme/widget",
        ):
            assert cache.get_snapshot_by_repo_url(query) is not None, query

    def test_miss_when_no_match(self, session):
        cache = RepoSnapshotCache(session)
        blob = serialise_collected_data(_make_collected_data())
        cache.store_snapshot(
            name="widget", ecosystem="npm",
            repo_url="https://github.com/acme/widget", blob=blob,
        )
        session.commit()
        assert cache.get_snapshot_by_repo_url(
            "https://github.com/different/repo"
        ) is None

    def test_returns_none_for_unparseable_url(self, session):
        cache = RepoSnapshotCache(session)
        # Empty / None input is rejected without touching the DB.
        assert cache.get_snapshot_by_repo_url("") is None
        assert cache.get_snapshot_by_repo_url(None) is None  # type: ignore[arg-type]

    def test_picks_most_recent_when_multiple_packages_share_repo(self, session):
        """Two packages on different ecosystems both wrote snapshots for
        the same repo. The lookup returns the most recent one."""
        cache = RepoSnapshotCache(session)
        old_blob = serialise_collected_data(_make_collected_data(commit_count=1))
        new_blob = serialise_collected_data(_make_collected_data(commit_count=5))

        old_snap = cache.store_snapshot(
            name="widget-npm", ecosystem="npm",
            repo_url="https://github.com/acme/widget", blob=old_blob,
            collected_at=datetime.utcnow() - timedelta(days=10),
        )
        new_snap = cache.store_snapshot(
            name="widget-pypi", ecosystem="pypi",
            repo_url="https://github.com/acme/widget", blob=new_blob,
        )
        session.commit()

        result = cache.get_snapshot_by_repo_url("https://github.com/acme/widget")
        assert result is not None
        assert result.id == new_snap.id
        assert result.id != old_snap.id

    def test_respects_freshness_sla_in_current_mode(self, session):
        """A snapshot older than SLA_EXPIRED_DAYS isn't served when
        cutoff_date is None."""
        cache = RepoSnapshotCache(session)
        blob = serialise_collected_data(_make_collected_data())
        cache.store_snapshot(
            name="widget", ecosystem="npm",
            repo_url="https://github.com/acme/widget", blob=blob,
            collected_at=datetime.utcnow() - timedelta(days=200),
        )
        session.commit()

        # Default sla_expired_days=90, snapshot is 200 days old → miss.
        assert cache.get_snapshot_by_repo_url(
            "https://github.com/acme/widget"
        ) is None

    def test_respects_collector_version(self, session):
        """Snapshots written by an older collector schema are filtered out
        — same invalidation contract as the package-keyed lookup."""
        from ossuary.db.models import RepoSnapshot
        cache = RepoSnapshotCache(session)
        package = cache._get_or_create_package("widget", "npm")
        session.add(
            RepoSnapshot(
                package_id=package.id,
                collected_at=datetime.utcnow(),
                coverage_until=datetime.utcnow(),
                repo_url="https://github.com/acme/widget",
                blob={},
                fetcher_version=COLLECTOR_VERSION + 99,
            )
        )
        session.commit()

        assert cache.get_snapshot_by_repo_url(
            "https://github.com/acme/widget"
        ) is None


# ---------------------------------------------------------------------------
# Typed failure classifier (v0.10.1 — phase 3 step 4)
# ---------------------------------------------------------------------------

class TestClassifyFailure:
    """The typed classifier is the contract that lets the SQL filters in
    stats() use exact equality instead of LIKE/lower. Each warning shape
    must map to the right ``FailureKind`` (or to ``None`` for transient)."""

    def test_no_repository_url_classifies_as_no_repo_url(self):
        from ossuary.services.repo_cache import FailureKind, classify_failure
        # Mixed case "URL" — the v0.10 regression — must still hit.
        assert classify_failure(
            "Package 'x' not found on npm (no repository URL)"
        ) == FailureKind.NO_REPO_URL
        assert classify_failure(
            "Package 'x' not found on npm (no repository url)"
        ) == FailureKind.NO_REPO_URL

    def test_repository_not_found_classifies_as_repo_not_found(self):
        from ossuary.services.repo_cache import FailureKind, classify_failure
        assert classify_failure(
            "Repository not found: https://github.com/foo/bar"
        ) == FailureKind.REPO_NOT_FOUND

    def test_unsupported_ecosystem_classifies_correctly(self):
        from ossuary.services.repo_cache import FailureKind, classify_failure
        assert classify_failure(
            "Unsupported ecosystem: cool-new-thing"
        ) == FailureKind.UNSUPPORTED_ECOSYSTEM

    def test_no_repo_url_wins_over_not_found(self):
        """The ``not found`` substring is also present in the no-repo-URL
        message; classifier must check the more specific phrase first
        otherwise no-repo-URL would land in the repo-not-found bucket
        and use the wrong (90-day) TTL."""
        from ossuary.services.repo_cache import FailureKind, classify_failure
        # The actual collector message contains both "not found" and
        # "no repository URL".
        assert classify_failure(
            "Package 'x' not found on npm (no repository URL)"
        ) == FailureKind.NO_REPO_URL

    def test_transient_failures_return_none(self):
        from ossuary.services.repo_cache import classify_failure
        for warning in (
            "pypi.weekly_downloads: HTTP 429 (rate limited)",
            "github.com returned 503 service unavailable",
            "INSUFFICIENT_DATA: pypi.weekly_downloads: HTTP 429",
            "transport error",
            "request timeout",
        ):
            assert classify_failure(warning) is None, warning

    def test_unknown_warning_returns_none(self):
        """Default to NOT cacheing — a misclassification that caches a
        transient is much worse than re-probing once on the next run."""
        from ossuary.services.repo_cache import classify_failure
        assert classify_failure("something weird happened") is None
        assert classify_failure("") is None
        assert classify_failure(None) is None  # type: ignore[arg-type]


class TestStoreNegativeWritesTypedKind:
    """``store_negative`` must populate both ``failure_reason`` (for
    operators) and ``failure_kind`` (for SQL). Without this the typed
    column would stay NULL and stats() would mis-bucket new rows."""

    def test_store_writes_typed_kind(self, session):
        from ossuary.db.models import Package
        from ossuary.services.repo_cache import FailureKind
        cache = RepoSnapshotCache(session)
        cache.store_negative(
            "x", "npm",
            "Package 'x' not found on npm (no repository URL)",
        )
        session.commit()
        package = session.query(Package).filter(Package.name == "x").first()
        assert package.failure_kind == FailureKind.NO_REPO_URL
        assert "no repository URL" in package.failure_reason

    def test_clear_negative_clears_typed_kind(self, session):
        from ossuary.db.models import Package
        cache = RepoSnapshotCache(session)
        cache.store_negative("x", "npm", "Repository not found: https://x")
        session.commit()
        cache.clear_negative("x", "npm")
        session.commit()
        package = session.query(Package).filter(Package.name == "x").first()
        assert package.failure_kind is None
        assert package.failure_reason is None
        assert package.last_failed_at is None

    def test_legacy_row_falls_back_to_text_classifier(self, session):
        """A pre-v0.10.1 row has failure_reason set but failure_kind NULL.
        ``get_negative_cache`` must still look up the right TTL by
        re-classifying the text — defensive belt-and-braces during the
        migration window."""
        from ossuary.db.models import Package
        from datetime import timedelta
        cache = RepoSnapshotCache(session)
        package = cache._get_or_create_package("legacy", "npm")
        # Simulate a row written before failure_kind existed.
        package.last_failed_at = datetime.utcnow() - timedelta(days=10)
        package.failure_reason = "Package 'legacy' not found on npm (no repository URL)"
        package.failure_kind = None
        session.commit()

        # 10 days < 30-day TTL (no-repo-url class) → still active despite
        # NULL failure_kind, because the text classifier kicks in.
        assert cache.get_negative_cache("legacy", "npm") is not None
