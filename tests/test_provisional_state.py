"""Provisional-score data-completeness contract.

Background: a missing *visibility* signal (registry downloads) silently
*lowers* the final risk score and is therefore refused as
``INSUFFICIENT_DATA``. A missing *protective* signal (GitHub Sponsors,
maintainer profile, orgs, issues, CII badge) silently *raises* the
score because the corresponding factor defaults to 0. The latter is
conservative-not-dangerous, so the engine still computes a number but
flags it ``is_provisional=True`` so the user can rescore once the
upstream recovers.

This test module pins the contract:

1. npm and registry collectors propagate ``fetch_errors`` exactly like
   PyPI does. Their failure → INSUFFICIENT_DATA.
2. GitHub auxiliary failures land in
   ``CollectedData.provisional_reasons`` and ``RiskBreakdown.is_provisional``.
3. GitHub *repo_info* transient failure is ESSENTIAL — lands in
   ``fetch_errors`` → INSUFFICIENT_DATA.
4. ``RiskBreakdown.to_dict()`` round-trips ``is_provisional`` and
   ``provisional_reasons`` through the cache.
5. ``rescore-invalid`` finds both INSUFFICIENT_DATA and provisional rows.

Tests use ``respx`` to stub the HTTP surface; no live network calls.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import patch

import httpx
import pytest

respx = pytest.importorskip("respx")

from ossuary.collectors.github import GitHubCollector, GitHubData
from ossuary.collectors.npm import NpmCollector
from ossuary.collectors.registries import (
    CratesCollector,
    GoProxyCollector,
    NuGetCollector,
    PackagistCollector,
    RubyGemsCollector,
)
from ossuary.scoring.factors import RiskBreakdown, RiskLevel
from ossuary.services.scorer import CollectedData, calculate_score_for_date


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_collected_data(*, fetch_errors=None, provisional_reasons=None) -> CollectedData:
    return CollectedData(
        repo_url="https://github.com/example/pkg",
        all_commits=[],
        github_data=GitHubData(
            maintainer_username="",
            maintainer_account_created=None,
            maintainer_public_repos=0,
            maintainer_total_stars=0,
            maintainer_repos=[],
            maintainer_sponsor_count=0,
            maintainer_orgs=[],
            has_github_sponsors=False,
            is_org_owned=False,
            org_admin_count=0,
            issues=[],
        ),
        weekly_downloads=None,
        maintainer_account_created=None,
        repo_stargazers=0,
        fetch_errors=fetch_errors or [],
        provisional_reasons=provisional_reasons or [],
    )


@pytest.fixture
def fast_sleep(monkeypatch):
    """Make ``asyncio.sleep`` instantaneous so retry tests run in ms."""
    real_sleep = asyncio.sleep

    async def _no_sleep(*_args, **_kwargs):
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", _no_sleep)


# ---------------------------------------------------------------------------
# Engine: provisional plumbing
# ---------------------------------------------------------------------------

class TestEngineProvisionalState:
    def test_provisional_reasons_propagate_to_breakdown(self):
        """Engine attaches reasons to breakdown and flags is_provisional."""
        data = _empty_collected_data(
            provisional_reasons=["github.sponsors_status: HTTP 502 from api.github.com"]
        )
        breakdown = calculate_score_for_date("pkg", "pypi", data, datetime.now())
        assert breakdown.is_provisional is True
        assert breakdown.provisional_reasons == [
            "github.sponsors_status: HTTP 502 from api.github.com"
        ]
        # The score itself is computed (not None) — provisional doesn't
        # block scoring, only flags it.
        assert breakdown.final_score is not None
        # Risk level is the normal bucket, not INSUFFICIENT_DATA.
        assert breakdown.risk_level != RiskLevel.INSUFFICIENT_DATA
        # The recommendations list contains the rescore hint.
        assert any("PROVISIONAL" in r for r in breakdown.recommendations)

    def test_no_provisional_reasons_means_not_provisional(self):
        data = _empty_collected_data()
        breakdown = calculate_score_for_date("pkg", "pypi", data, datetime.now())
        assert breakdown.is_provisional is False
        assert breakdown.provisional_reasons == []

    def test_fetch_errors_take_precedence_over_provisional(self):
        """If both lists are populated, INSUFFICIENT_DATA wins.

        A missing visibility signal (fetch_errors) is the strict
        condition; a missing protective signal (provisional) only
        matters for scores that *could* be computed. When both are
        present the score isn't computable, so the strict rule wins.
        """
        data = _empty_collected_data(
            fetch_errors=["pypi.weekly_downloads: HTTP 429"],
            provisional_reasons=["github.sponsors: HTTP 502"],
        )
        breakdown = calculate_score_for_date("pkg", "pypi", data, datetime.now())
        assert breakdown.risk_level == RiskLevel.INSUFFICIENT_DATA
        assert breakdown.final_score is None

    def test_to_dict_round_trips_provisional_state(self):
        data = _empty_collected_data(provisional_reasons=["x: y"])
        breakdown = calculate_score_for_date("pkg", "pypi", data, datetime.now())
        d = breakdown.to_dict()
        assert d["provisional_reasons"] == ["x: y"]
        assert d["is_provisional"] is True


# ---------------------------------------------------------------------------
# npm collector contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestNpmCollectorFetchErrors:
    @respx.mock
    async def test_429_on_downloads_propagates_fetch_error(self, fast_sleep):
        respx.get("https://registry.npmjs.org/lodash").respond(
            200, json={"dist-tags": {"latest": "4.17.21"}, "description": "",
                       "homepage": "", "repository": {}, "maintainers": []}
        )
        respx.get("https://api.npmjs.org/downloads/point/last-week/lodash").mock(
            return_value=httpx.Response(429)
        )
        c = NpmCollector()
        try:
            data = await c.collect("lodash")
        finally:
            await c.close()
        assert data.weekly_downloads is None
        assert any("npm.weekly_downloads" in e and "429" in e
                   for e in data.fetch_errors)

    @respx.mock
    async def test_zero_downloads_is_real_zero(self):
        """A genuine 0-downloads response is a real measurement, not a failure."""
        respx.get("https://registry.npmjs.org/dead").respond(
            200, json={"dist-tags": {"latest": "0.0.1"}, "description": "",
                       "homepage": "", "repository": {}, "maintainers": []}
        )
        respx.get("https://api.npmjs.org/downloads/point/last-week/dead").respond(
            200, json={"downloads": 0, "package": "dead"}
        )
        c = NpmCollector()
        try:
            data = await c.collect("dead")
        finally:
            await c.close()
        assert data.weekly_downloads == 0
        assert data.fetch_errors == []

    @respx.mock
    async def test_clean_run_no_fetch_errors(self):
        respx.get("https://registry.npmjs.org/lodash").respond(
            200, json={"dist-tags": {"latest": "4.17.21"}, "description": "Util",
                       "homepage": "", "repository": {}, "maintainers": []}
        )
        respx.get("https://api.npmjs.org/downloads/point/last-week/lodash").respond(
            200, json={"downloads": 50_000_000, "package": "lodash"}
        )
        c = NpmCollector()
        try:
            data = await c.collect("lodash")
        finally:
            await c.close()
        assert data.weekly_downloads == 50_000_000
        assert data.fetch_errors == []

    @respx.mock
    async def test_malformed_json_is_failure(self):
        respx.get("https://registry.npmjs.org/x").respond(
            200, json={"dist-tags": {}, "repository": {}, "maintainers": []}
        )
        respx.get("https://api.npmjs.org/downloads/point/last-week/x").respond(
            200, content=b"not json {",
            headers={"content-type": "application/json"},
        )
        c = NpmCollector()
        try:
            data = await c.collect("x")
        finally:
            await c.close()
        assert data.weekly_downloads is None
        assert any("malformed JSON" in e for e in data.fetch_errors)


# ---------------------------------------------------------------------------
# Lightweight registry collectors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRegistryCollectorFetchErrors:
    @respx.mock
    async def test_crates_429_propagates(self, fast_sleep):
        respx.get("https://crates.io/api/v1/crates/serde").mock(
            return_value=httpx.Response(429)
        )
        c = CratesCollector()
        try:
            data = await c.collect("serde")
        finally:
            await c.close()
        assert data.weekly_downloads is None
        assert any("cargo.crate_info" in e for e in data.fetch_errors)

    @respx.mock
    async def test_rubygems_5xx_propagates(self, fast_sleep):
        respx.get("https://rubygems.org/api/v1/gems/sidekiq.json").mock(
            return_value=httpx.Response(503)
        )
        c = RubyGemsCollector()
        try:
            data = await c.collect("sidekiq")
        finally:
            await c.close()
        assert data.weekly_downloads is None
        assert any("503" in e for e in data.fetch_errors)

    @respx.mock
    async def test_packagist_404_propagates_no_retry(self, fast_sleep):
        respx.get("https://packagist.org/packages/foo/bar.json").mock(
            return_value=httpx.Response(404)
        )
        c = PackagistCollector()
        try:
            data = await c.collect("foo/bar")
        finally:
            await c.close()
        assert data.weekly_downloads is None
        assert any("404" in e for e in data.fetch_errors)

    @respx.mock
    async def test_nuget_search_returns_empty_propagates(self):
        respx.get(
            "https://azuresearch-usnc.nuget.org/query?q=packageid:Newtonsoft.Json&take=1"
        ).respond(200, json={"data": [], "totalHits": 0})
        c = NuGetCollector()
        try:
            data = await c.collect("Newtonsoft.Json")
        finally:
            await c.close()
        assert data.weekly_downloads is None
        assert any("not found" in e for e in data.fetch_errors)

    @respx.mock
    async def test_go_proxy_failure_is_NOT_fetch_error(self, fast_sleep):
        """Go has no download API — proxy.golang.org failure is non-essential.

        weekly_downloads stays at the real measurement of 0 (Go has no
        signal regardless), and fetch_errors stays empty so the score
        doesn't go INSUFFICIENT_DATA over a missing version string.
        """
        respx.get(
            "https://proxy.golang.org/github.com/gin-gonic/gin/@latest"
        ).mock(return_value=httpx.Response(503))
        c = GoProxyCollector()
        try:
            data = await c.collect("github.com/gin-gonic/gin")
        finally:
            await c.close()
        assert data.weekly_downloads == 0
        assert data.fetch_errors == []
        assert data.repository_url == "https://github.com/gin-gonic/gin"

    @respx.mock
    async def test_clean_crates_run(self):
        respx.get("https://crates.io/api/v1/crates/serde").respond(
            200, json={"crate": {
                "newest_version": "1.0.0", "description": "Serialization",
                "repository": "https://github.com/serde-rs/serde",
                "recent_downloads": 26_000_000,
            }}
        )
        c = CratesCollector()
        try:
            data = await c.collect("serde")
        finally:
            await c.close()
        assert data.fetch_errors == []
        assert data.weekly_downloads == 26_000_000 // 13


# ---------------------------------------------------------------------------
# GitHub collector: essential vs provisional classification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGitHubFailureClassification:
    @respx.mock
    async def test_repo_info_404_is_NOT_fetch_error(self, monkeypatch):
        """404 on repo_info means "doesn't exist", not "transient failure"."""
        monkeypatch.setenv("GITHUB_TOKEN", "test_token")
        respx.get("https://api.github.com/repos/missing/repo").respond(404)
        # repo_info 404 falls through to using "missing" as the maintainer
        # username; mock the user-profile/repos/etc. calls as 404 too so
        # the collector doesn't blow up trying to find what's not there.
        respx.get("https://api.github.com/users/missing").respond(404)
        respx.get("https://api.github.com/users/missing/repos").respond(200, json=[])
        respx.post("https://api.github.com/graphql").respond(200, json={"data": {}})
        respx.get("https://api.github.com/users/missing/orgs").respond(200, json=[])
        respx.get("https://api.github.com/repos/missing/repo/readme").respond(404)
        respx.get("https://api.github.com/repos/missing/repo/issues").respond(200, json=[])
        c = GitHubCollector(token="test_token")
        try:
            data = await c.collect("https://github.com/missing/repo")
        finally:
            await c.close()
        # 404 is permanent — no fetch_error for it
        assert not any("repo_info" in e for e in data.fetch_errors)

    @respx.mock
    async def test_sponsors_failure_lands_in_provisional(self, monkeypatch):
        """A 502 on the sponsors GraphQL call → provisional, not fatal."""
        monkeypatch.setenv("GITHUB_TOKEN", "test_token")
        # repo_info succeeds with user-owned repo
        respx.get("https://api.github.com/repos/octocat/hello").respond(
            200, json={
                "owner": {"type": "User", "login": "octocat"}, "name": "hello",
                "stargazers_count": 100,
            }
        )
        respx.get("https://api.github.com/users/octocat").respond(
            200, json={"login": "octocat", "created_at": "2010-01-01T00:00:00Z",
                       "public_repos": 5}
        )
        respx.get("https://api.github.com/users/octocat/repos").respond(200, json=[])
        # Sponsors GraphQL fails
        respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(502)
        )
        respx.get("https://api.github.com/users/octocat/orgs").respond(200, json=[])
        respx.get("https://api.github.com/repos/octocat/hello/readme").respond(404)
        respx.get("https://api.github.com/repos/octocat/hello/issues").respond(200, json=[])
        c = GitHubCollector(token="test_token")
        try:
            data = await c.collect(
                "https://github.com/octocat/hello",
                top_contributor_username="octocat",
            )
        finally:
            await c.close()
        assert data.fetch_errors == [], (
            "sponsor failures must NOT flip the score to INSUFFICIENT_DATA"
        )
        assert any("sponsors" in r for r in data.provisional_reasons)

    @respx.mock
    async def test_repo_info_5xx_lands_in_fetch_errors(self, monkeypatch):
        """A transient 5xx on repo_info IS essential → INSUFFICIENT_DATA."""
        monkeypatch.setenv("GITHUB_TOKEN", "test_token")
        respx.get("https://api.github.com/repos/octocat/hello").mock(
            return_value=httpx.Response(503)
        )
        # everything else returns 404 to short-circuit
        respx.get("https://api.github.com/users/octocat").respond(404)
        respx.get("https://api.github.com/users/octocat/repos").respond(200, json=[])
        respx.post("https://api.github.com/graphql").respond(200, json={"data": {}})
        respx.get("https://api.github.com/users/octocat/orgs").respond(200, json=[])
        respx.get("https://api.github.com/repos/octocat/hello/readme").respond(404)
        respx.get("https://api.github.com/repos/octocat/hello/issues").respond(200, json=[])
        c = GitHubCollector(token="test_token")
        try:
            data = await c.collect("https://github.com/octocat/hello")
        finally:
            await c.close()
        assert any("repo_info" in e and "503" in e for e in data.fetch_errors)


# ---------------------------------------------------------------------------
# Cache round-trip
# ---------------------------------------------------------------------------

class TestCacheRoundTrip:
    def test_is_provisional_persists_through_breakdown_dict(self):
        """to_dict / from-dict round-trip preserves the flag."""
        data = _empty_collected_data(provisional_reasons=["foo: bar"])
        breakdown = calculate_score_for_date("p", "pypi", data, datetime.now())
        d = breakdown.to_dict()
        assert d["is_provisional"] is True
        assert d["provisional_reasons"] == ["foo: bar"]


# ---------------------------------------------------------------------------
# rescore-invalid finds both states
# ---------------------------------------------------------------------------

class TestAutoMigration:
    """``init_db()`` must self-heal a DB that pre-dates ``is_provisional``.

    Without this, an upgrade-in-place crashes on the first write to the
    new column with ``OperationalError: no such column: is_provisional``.
    """

    def test_init_db_adds_missing_is_provisional_column(self, tmp_path, monkeypatch):
        import sqlite3

        from sqlalchemy import create_engine, inspect
        from sqlalchemy.orm import sessionmaker

        # Build a DB that matches the *pre*-provisional schema. Use raw
        # SQLite so we can omit the new column without SQLAlchemy
        # re-adding it from the model.
        db_path = tmp_path / "old.db"
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE packages (
                id INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                ecosystem VARCHAR(50) NOT NULL,
                repo_url VARCHAR(500),
                description TEXT,
                homepage VARCHAR(500),
                created_at DATETIME,
                last_analyzed DATETIME,
                UNIQUE(name, ecosystem)
            );
            CREATE TABLE scores (
                id INTEGER PRIMARY KEY,
                package_id INTEGER NOT NULL REFERENCES packages(id),
                calculated_at DATETIME NOT NULL,
                cutoff_date DATETIME NOT NULL,
                final_score INTEGER,
                risk_level VARCHAR(32) NOT NULL,
                base_risk INTEGER,
                activity_modifier INTEGER,
                protective_factors_total INTEGER,
                sentiment_modifier INTEGER NOT NULL DEFAULT 0,
                breakdown JSON NOT NULL,
                maintainer_concentration FLOAT,
                commits_last_year INTEGER,
                unique_contributors INTEGER,
                weekly_downloads INTEGER DEFAULT 0
                -- Note: NO is_provisional column
            );
        """)
        con.commit()
        con.close()

        # Confirm the starting state matches a pre-provisional install.
        con = sqlite3.connect(db_path)
        cols_before = {row[1] for row in con.execute("PRAGMA table_info(scores)")}
        con.close()
        assert "is_provisional" not in cols_before

        # Point the session module at the legacy DB and re-init.
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        # The session module reads DATABASE_URL at import time, so we
        # need a fresh engine here rather than trusting module-level state.
        engine = create_engine(f"sqlite:///{db_path}")
        from ossuary.db.session import _autoapply_simple_migrations
        with engine.begin() as conn:
            _autoapply_simple_migrations(conn)

        # Column should now exist.
        inspector = inspect(engine)
        cols_after = {col["name"] for col in inspector.get_columns("scores")}
        assert "is_provisional" in cols_after, (
            "init_db must auto-add is_provisional on legacy DBs"
        )

        # Re-running must be a no-op (idempotent).
        with engine.begin() as conn:
            _autoapply_simple_migrations(conn)


class TestRescoreInvalidCli:
    """CliRunner-level coverage for ``rescore-invalid --only``.

    Complements the SQL-filter test below: this verifies that the typer
    command actually wires up the flag, picks the right targets, and
    refuses unknown values.
    """

    def _seed_db(self, tmp_path, monkeypatch):
        """Build an isolated SQLite DB with three Score rows: one
        INSUFFICIENT_DATA, one provisional, one clean. Returns nothing —
        the test reads via the DB the CLI will see."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from ossuary.db.models import Base, Package, Score
        from ossuary.db import session as session_module

        db_path = tmp_path / "rescore.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)

        # Patch the module-level engine + SessionLocal that the CLI reads.
        monkeypatch.setattr(session_module, "engine", engine)
        monkeypatch.setattr(session_module, "SessionLocal", TestSession)

        with TestSession() as session:
            now = datetime.utcnow()
            for name, eco, risk, prov in [
                ("alpha", "pypi", "INSUFFICIENT_DATA", False),
                ("beta", "npm", "MODERATE", True),
                ("gamma", "npm", "LOW", False),
            ]:
                pkg = Package(name=name, ecosystem=eco)
                session.add(pkg)
                session.flush()
                session.add(Score(
                    package_id=pkg.id, calculated_at=now, cutoff_date=now,
                    final_score=None if risk == "INSUFFICIENT_DATA" else 50,
                    risk_level=risk, base_risk=None, activity_modifier=None,
                    protective_factors_total=None,
                    breakdown={}, maintainer_concentration=None,
                    commits_last_year=None, unique_contributors=None,
                    weekly_downloads=None, is_provisional=prov,
                ))
            session.commit()

    def test_dry_run_only_insufficient_lists_only_alpha(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from ossuary.cli import app

        self._seed_db(tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, [
            "rescore-invalid", "--only", "insufficient", "--dry-run"
        ])
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert "beta" not in result.output
        assert "gamma" not in result.output

    def test_dry_run_only_provisional_lists_only_beta(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from ossuary.cli import app

        self._seed_db(tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, [
            "rescore-invalid", "--only", "provisional", "--dry-run"
        ])
        assert result.exit_code == 0, result.output
        assert "alpha" not in result.output
        assert "beta" in result.output
        assert "gamma" not in result.output

    def test_dry_run_only_both_lists_alpha_and_beta(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from ossuary.cli import app

        self._seed_db(tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, [
            "rescore-invalid", "--only", "both", "--dry-run"
        ])
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "gamma" not in result.output

    def test_invalid_only_value_exits_nonzero(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from ossuary.cli import app

        self._seed_db(tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, [
            "rescore-invalid", "--only", "garbage", "--dry-run"
        ])
        assert result.exit_code != 0
        assert "Invalid --only" in result.output


class TestRebuildBreakdownRoundTrip:
    """A cached row's breakdown JSON must round-trip the provisional flag.

    The CLI/dashboard read scores from the cache via ``score_package``,
    which reconstructs the ``RiskBreakdown`` via ``_rebuild_breakdown``.
    If that helper drops ``provisional_reasons`` / ``is_provisional``,
    the user sees a clean score on a row that was actually computed
    from incomplete data — silently undoing the entire provisional
    contract for any cached read.
    """

    def test_cached_provisional_breakdown_rebuilds_with_flag(self):
        from ossuary.services.scorer import _rebuild_breakdown

        # Build a fake cached Score that mirrors what store_score would
        # have written for a provisional run.
        provisional_breakdown_dict = {
            "package": {"name": "lodash", "ecosystem": "npm",
                        "repo_url": "https://github.com/lodash/lodash"},
            "metrics": {"maintainer_concentration": 25.0,
                        "commits_last_year": 30, "unique_contributors": 5,
                        "weekly_downloads": 50_000_000},
            "chaoss_signals": {"bus_factor": 2, "elephant_factor": 1,
                               "inactive_contributor_ratio": 0.1},
            "score": {
                "final": 35, "risk_level": "LOW",
                "components": {
                    "base_risk": 20, "activity_modifier": -30,
                    "protective_factors": {
                        "reputation": {"score": -10, "evidence": "tier-1"},
                        "funding": {"score": 0, "evidence": None},
                        "organization": {"score": 0},
                        "visibility": {"score": -20},
                        "distributed_governance": {"score": -10},
                        "community": {"score": 0},
                        "cii_badge": {"score": 0},
                        "frustration": {"score": 0, "evidence": []},
                        "sentiment": {"score": 0, "evidence": []},
                        "maturity": {"score": 0, "evidence": None},
                        "takeover_risk": {"score": 0, "evidence": None},
                    },
                },
            },
            "explanation": "ok",
            "recommendations": [],
            "data_sources": {},
            "factor_availability": {},
            "warnings": [],
            "incomplete_reasons": [],
            "provisional_reasons": [
                "github.sponsors_status: HTTP 502 from api.github.com",
                "github.user_orgs: HTTP 502 from api.github.com",
            ],
            "is_provisional": True,
        }

        # The helper takes a Score row, not a dict — build a stub that
        # exposes the columns it actually reads.
        class FakeScoreRow:
            breakdown = provisional_breakdown_dict
            risk_level = "LOW"
            maintainer_concentration = 25.0
            commits_last_year = 30
            unique_contributors = 5
            weekly_downloads = 50_000_000
            base_risk = 20
            activity_modifier = -30
            final_score = 35

        rebuilt = _rebuild_breakdown(FakeScoreRow(), "lodash", "npm")
        assert rebuilt is not None
        assert rebuilt.is_provisional is True
        assert rebuilt.provisional_reasons == [
            "github.sponsors_status: HTTP 502 from api.github.com",
            "github.user_orgs: HTTP 502 from api.github.com",
        ]
        # Score is preserved (provisional doesn't blank it).
        assert rebuilt.final_score == 35
        assert rebuilt.risk_level == RiskLevel.LOW

    def test_cached_clean_breakdown_rebuilds_without_flag(self):
        """A row written before the provisional column existed (no
        ``provisional_reasons`` / ``is_provisional`` keys in the JSON)
        must come back with ``is_provisional=False``, not crash."""
        from ossuary.services.scorer import _rebuild_breakdown

        legacy_breakdown_dict = {
            "package": {"name": "x", "ecosystem": "npm", "repo_url": ""},
            "metrics": {"maintainer_concentration": 10.0,
                        "commits_last_year": 5, "unique_contributors": 1,
                        "weekly_downloads": 0},
            "chaoss_signals": {"bus_factor": 1, "elephant_factor": 1,
                               "inactive_contributor_ratio": 0.0},
            "score": {"final": 60, "risk_level": "HIGH",
                      "components": {"base_risk": 60, "activity_modifier": 0,
                                     "protective_factors": {}}},
            "explanation": "", "recommendations": [],
            # No provisional_reasons / is_provisional keys.
        }

        class FakeScoreRow:
            breakdown = legacy_breakdown_dict
            risk_level = "HIGH"
            maintainer_concentration = 10.0
            commits_last_year = 5
            unique_contributors = 1
            weekly_downloads = 0
            base_risk = 60
            activity_modifier = 0
            final_score = 60

        rebuilt = _rebuild_breakdown(FakeScoreRow(), "x", "npm")
        assert rebuilt is not None
        assert rebuilt.is_provisional is False
        assert rebuilt.provisional_reasons == []


class TestInitDbEndToEndOnLegacyDb:
    """Full ``init_db()`` (not just the helper) must self-heal a
    legacy on-disk database."""

    def test_init_db_with_legacy_url_adds_column(self, tmp_path, monkeypatch):
        import sqlite3

        from sqlalchemy import create_engine, inspect
        from sqlalchemy.orm import sessionmaker

        # Build the pre-provisional schema with a few rows of data.
        db_path = tmp_path / "legacy.db"
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE packages (
                id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL,
                ecosystem VARCHAR(50) NOT NULL, repo_url VARCHAR(500),
                description TEXT, homepage VARCHAR(500),
                created_at DATETIME, last_analyzed DATETIME,
                UNIQUE(name, ecosystem)
            );
            CREATE TABLE scores (
                id INTEGER PRIMARY KEY,
                package_id INTEGER NOT NULL REFERENCES packages(id),
                calculated_at DATETIME NOT NULL,
                cutoff_date DATETIME NOT NULL,
                final_score INTEGER, risk_level VARCHAR(32) NOT NULL,
                base_risk INTEGER, activity_modifier INTEGER,
                protective_factors_total INTEGER,
                sentiment_modifier INTEGER NOT NULL DEFAULT 0,
                breakdown JSON NOT NULL,
                maintainer_concentration FLOAT, commits_last_year INTEGER,
                unique_contributors INTEGER, weekly_downloads INTEGER DEFAULT 0
            );
            INSERT INTO packages (name, ecosystem) VALUES ('legacy_pkg', 'pypi');
            INSERT INTO scores (
                package_id, calculated_at, cutoff_date, risk_level, breakdown
            ) VALUES (1, '2026-01-01', '2026-01-01', 'LOW', '{}');
        """)
        con.commit()
        con.close()

        # Repoint the session module at this DB and run init_db end-to-end.
        # This exercises the full path including create_all() (which is a
        # no-op on existing tables) followed by the auto-migration helper.
        from ossuary.db import session as session_module

        engine = create_engine(f"sqlite:///{db_path}")
        TestSession = sessionmaker(bind=engine)
        monkeypatch.setattr(session_module, "engine", engine)
        monkeypatch.setattr(session_module, "SessionLocal", TestSession)

        # Sanity: column missing before init_db.
        cols_before = {c["name"] for c in inspect(engine).get_columns("scores")}
        assert "is_provisional" not in cols_before

        session_module.init_db()

        # Column present after, and the legacy row survived intact.
        cols_after = {c["name"] for c in inspect(engine).get_columns("scores")}
        assert "is_provisional" in cols_after
        with TestSession() as session:
            from ossuary.db.models import Score
            row = session.query(Score).first()
            assert row is not None
            # Existing rows backfilled to False (default 0).
            assert bool(row.is_provisional) is False


class TestDashboardProvisionalRendering:
    """Dashboard pages must render the ⚠ PROVISIONAL banner.

    Uses Streamlit's ``AppTest`` (no browser) to load the page, seed
    ``session_state`` with a provisional ``RiskBreakdown``, and assert
    the warning element appears. Mirrors the existing INSUFFICIENT_DATA
    coverage in ``tests/test_dashboard_api.py``.
    """

    def _provisional_result(self):
        """Build a ScoringResult with a non-trivial provisional breakdown."""
        from ossuary.services.scorer import ScoringResult
        breakdown = RiskBreakdown(
            package_name="lodash",
            ecosystem="npm",
            repo_url="https://github.com/lodash/lodash",
            maintainer_concentration=25.0,
            bus_factor=2,
            elephant_factor=1,
            inactive_contributor_ratio=0.1,
            commits_last_year=30,
            unique_contributors=5,
            weekly_downloads=50_000_000,
            base_risk=20,
            activity_modifier=-30,
            final_score=35,
            risk_level=RiskLevel.LOW,
            provisional_reasons=[
                "github.sponsors_status: HTTP 502 from api.github.com",
            ],
            explanation="Distributed commits.",
        )
        return ScoringResult(success=True, breakdown=breakdown)

    @staticmethod
    def _patch_page_link(monkeypatch):
        """``st.page_link`` requires a multi-page app context that
        ``AppTest.from_file`` doesn't provide; calls crash with
        ``KeyError('url_pathname')``. Stub it out — the navigation
        links aren't what we're testing here."""
        import streamlit as st
        monkeypatch.setattr(st, "page_link", lambda *a, **k: None)

    def test_score_page_shows_provisional_banner(self, monkeypatch):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            pytest.skip("streamlit testing harness unavailable")

        self._patch_page_link(monkeypatch)
        page_path = "src/ossuary/dashboard/pages/3_Score.py"
        app = AppTest.from_file(page_path)
        # Seed the rendered-results branch directly — the user-input
        # path requires running score_package against a real package,
        # which would hit the network.
        app.session_state["score_result"] = self._provisional_result()
        app.session_state["score_pkg"] = "lodash"
        app.session_state["score_eco"] = "npm"
        app.run(timeout=20)
        assert not app.exception, app.exception

        # The Streamlit ``warning`` element with the PROVISIONAL banner
        # must be present somewhere on the page.
        warning_bodies = [w.body for w in app.warning]
        assert any("PROVISIONAL" in body for body in warning_bodies), (
            f"expected PROVISIONAL warning on Score page, got warnings: "
            f"{warning_bodies}"
        )

    def test_package_page_shows_provisional_banner(self, monkeypatch):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            pytest.skip("streamlit testing harness unavailable")

        self._patch_page_link(monkeypatch)

        # The Package page reads the package via score_package using
        # query params. Patch score_package to return our provisional
        # result so we don't need a populated DB or live network.
        result = self._provisional_result()

        async def _fake_score_package(*_args, **_kwargs):
            return result

        # Patch in the ossuary.services.scorer namespace since the page
        # imports from there.
        monkeypatch.setattr(
            "ossuary.services.scorer.score_package",
            _fake_score_package,
        )

        page_path = "src/ossuary/dashboard/pages/2_Package.py"
        app = AppTest.from_file(page_path)
        app.query_params["name"] = "lodash"
        app.query_params["eco"] = "npm"
        app.run(timeout=20)
        assert not app.exception, app.exception

        warning_bodies = [w.body for w in app.warning]
        assert any("PROVISIONAL" in body for body in warning_bodies), (
            f"expected PROVISIONAL warning on Package page, got: "
            f"{warning_bodies}"
        )


class TestRescoreInvalidFindsBothStates:
    def test_query_filter_includes_provisional_rows(self, tmp_path, monkeypatch):
        """The CLI's SQL filter must catch is_provisional=True rows.

        We assemble a DB with three Score rows (one INSUFFICIENT_DATA,
        one is_provisional=True, one clean) and verify the query that
        powers ``rescore-invalid`` returns the first two but not the
        third.
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from ossuary.db.models import Base, Package, Score

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        with SessionLocal() as session:
            now = datetime.utcnow()
            for i, (eco, risk, prov) in enumerate([
                ("pypi", "INSUFFICIENT_DATA", False),
                ("npm", "MODERATE", True),
                ("npm", "LOW", False),
            ]):
                pkg = Package(name=f"pkg{i}", ecosystem=eco)
                session.add(pkg)
                session.flush()
                session.add(Score(
                    package_id=pkg.id, calculated_at=now, cutoff_date=now,
                    final_score=None if risk == "INSUFFICIENT_DATA" else 50,
                    risk_level=risk, base_risk=None, activity_modifier=None,
                    protective_factors_total=None,
                    breakdown={}, maintainer_concentration=None,
                    commits_last_year=None, unique_contributors=None,
                    weekly_downloads=None, is_provisional=prov,
                ))
            session.commit()

        # Replicate the SQL filter from the CLI command
        from sqlalchemy import or_
        with SessionLocal() as session:
            is_insufficient = Score.risk_level == "INSUFFICIENT_DATA"
            is_provisional = Score.is_provisional.is_(True)
            rows = (
                session.query(Package, Score)
                .join(Score, Score.package_id == Package.id)
                .filter(or_(is_insufficient, is_provisional))
                .all()
            )
            names = {pkg.name for pkg, _ in rows}
            assert names == {"pkg0", "pkg1"}, (
                f"rescore-invalid filter should find both INSUFFICIENT_DATA "
                f"and provisional rows; got {names}"
            )
