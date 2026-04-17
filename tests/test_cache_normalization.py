"""Name-normalisation contract for the cache layer.

Background: a DB duplicate was found on 2026-04-17 where the same PyPI
distribution lived under two different capitalisations (``PyYAML`` and
``pyyaml``) with separately cached scores. The cache-layer lookup was
case-sensitive so two code paths feeding different cases created two
rows. This test pins the fix: ``get_or_create_package`` normalises
PyPI names per PEP 503 so the same logical package resolves to the
same row regardless of how the caller wrote it.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ossuary.db.models import Base
from ossuary.services.cache import ScoreCache, normalize_package_name


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class TestNormalizePackageName:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("PyYAML", "pyyaml"),
            ("pyyaml", "pyyaml"),
            ("py_yaml", "py-yaml"),
            ("py.yaml", "py-yaml"),
            ("  PyYAML  ", "pyyaml"),
            # Runs of dividers collapse to a single hyphen (PEP 503).
            ("py__yaml", "py-yaml"),
            ("py.-yaml", "py-yaml"),
            # Already canonical.
            ("requests", "requests"),
            ("urllib3", "urllib3"),
        ],
    )
    def test_pypi_pep503(self, given, expected):
        assert normalize_package_name(given, "pypi") == expected

    @pytest.mark.parametrize(
        "ecosystem,given",
        [
            # Other ecosystems are pass-through. Lockdown against speculative
            # normalisation being added without evidence.
            ("npm", "@babel/core"),
            ("npm", "Socket.IO"),
            ("cargo", "Serde_json"),
            ("rubygems", "Rails"),
            ("go", "github.com/Spf13/cobra"),
            ("github", "anicka-net/ossuary-risk"),
            ("packagist", "symfony/Console"),
            ("nuget", "Newtonsoft.Json"),
        ],
    )
    def test_other_ecosystems_pass_through(self, ecosystem, given):
        assert normalize_package_name(given, ecosystem) == given


class TestGetOrCreatePackageDedup:
    def test_case_variants_resolve_to_same_row(self, session):
        # All four resolve to canonical "pyyaml" — no internal dividers.
        cache = ScoreCache(session)
        a = cache.get_or_create_package("PyYAML", "pypi")
        b = cache.get_or_create_package("pyyaml", "pypi")
        c = cache.get_or_create_package("PYYAML", "pypi")
        d = cache.get_or_create_package("  PyYAML  ", "pypi")
        session.commit()
        assert a.id == b.id == c.id == d.id
        assert a.name == "pyyaml"  # canonical form stored

    def test_divider_variants_resolve_to_same_row(self, session):
        # PEP 503: ``-``, ``_`` and ``.`` collapse to a single ``-``. So
        # ``foo-bar``, ``foo_bar``, ``foo.bar``, ``Foo--Bar`` all dedupe.
        cache = ScoreCache(session)
        a = cache.get_or_create_package("foo-bar", "pypi")
        b = cache.get_or_create_package("foo_bar", "pypi")
        c = cache.get_or_create_package("foo.bar", "pypi")
        d = cache.get_or_create_package("Foo--Bar", "pypi")
        session.commit()
        assert a.id == b.id == c.id == d.id
        assert a.name == "foo-bar"

    def test_packages_differing_only_by_dividers_distinct(self, session):
        # Lockdown: a name without dividers (``pyyaml``) and one with
        # (``py-yaml``) ARE different canonical packages on PyPI. Don't
        # over-dedupe.
        cache = ScoreCache(session)
        a = cache.get_or_create_package("pyyaml", "pypi")
        b = cache.get_or_create_package("py-yaml", "pypi")
        session.commit()
        assert a.id != b.id

    def test_same_ecosystem_different_name_creates_distinct_rows(self, session):
        cache = ScoreCache(session)
        a = cache.get_or_create_package("pyyaml", "pypi")
        b = cache.get_or_create_package("pydantic", "pypi")
        session.commit()
        assert a.id != b.id

    def test_same_name_different_ecosystem_creates_distinct_rows(self, session):
        # ``lodash`` exists on npm; if somebody also tried it on PyPI the rows
        # must remain distinct — normalisation is per-ecosystem, not global.
        cache = ScoreCache(session)
        a = cache.get_or_create_package("lodash", "npm")
        b = cache.get_or_create_package("lodash", "pypi")
        session.commit()
        assert a.id != b.id
        assert a.ecosystem == "npm"
        assert b.ecosystem == "pypi"

    def test_repo_url_filled_in_on_dedup_hit(self, session):
        """When a later call supplies a repo_url the row didn't have yet,
        it should be populated — not just silently discarded."""
        cache = ScoreCache(session)
        first = cache.get_or_create_package("PyYAML", "pypi", repo_url=None)
        assert first.repo_url is None
        cache.get_or_create_package("pyyaml", "pypi",
                                     repo_url="https://github.com/yaml/pyyaml")
        session.commit()
        refreshed = cache.get_or_create_package("PyYAML", "pypi")
        assert refreshed.repo_url == "https://github.com/yaml/pyyaml"

    def test_npm_case_sensitivity_preserved(self, session):
        """Lockdown: npm must still be case-sensitive until we have evidence
        it should not be. A silent npm normalisation would collide scoped
        packages or legitimately-distinct registrations."""
        cache = ScoreCache(session)
        a = cache.get_or_create_package("Socket.IO", "npm")
        b = cache.get_or_create_package("socket.io", "npm")
        session.commit()
        # Different rows; the bug we fixed is pypi-specific.
        assert a.id != b.id


class TestUserFacingLookupsNormalize:
    """The original normalisation fix lived only at the cache write side
    (``ScoreCache.get_or_create_package``). User-facing read sites that
    looked up ``Package.name == package`` directly would then miss the
    canonical row when the user typed the original capitalisation. These
    tests pin that the read sites are now also normalised.
    """

    def test_dashboard_get_score_history_finds_canonicalized_row(self, session, monkeypatch):
        """``dashboard.utils.get_score_history`` must canonicalise the
        ``package_name`` before the DB lookup so users can search the way
        they originally typed (``PyYAML``) and still hit the stored row."""
        from datetime import datetime
        from ossuary.db.models import Score
        from ossuary.dashboard import utils as dash_utils

        cache = ScoreCache(session)
        pkg = cache.get_or_create_package("PyYAML", "pypi")
        session.add(Score(
            package_id=pkg.id,
            calculated_at=datetime.utcnow(),
            cutoff_date=datetime(2026, 4, 1),
            final_score=42,
            risk_level="MODERATE",
            base_risk=40,
            activity_modifier=2,
            protective_factors_total=0,
            sentiment_modifier=0,
            breakdown={},
            maintainer_concentration=50.0,
            commits_last_year=10,
            unique_contributors=3,
            weekly_downloads=1000,
        ))
        session.commit()

        # Patch the dashboard's session helper to hand back our test session.
        class _FakeSessionScope:
            def __init__(self, s):
                self._s = s
            def __enter__(self):
                return self._s
            def __exit__(self, *_a):
                return False

        def _fake_get_session():
            yield _FakeSessionScope(session)

        monkeypatch.setattr(dash_utils, "get_session", _fake_get_session)

        # User typed ``PyYAML`` but the row is stored as ``pyyaml``.
        history = dash_utils.get_score_history("PyYAML", "pypi")
        assert len(history) == 1
        assert history[0]["score"] == 42

        # Other capitalisations also resolve.
        assert dash_utils.get_score_history("pyyaml", "pypi")
        assert dash_utils.get_score_history("PYYAML", "pypi")

    def test_dashboard_history_works_with_broken_cache_module(self, monkeypatch):
        """The dashboard's history lookup must not depend on the
        ``ossuary.services.cache`` module being importable in the
        currently-loaded interpreter. Streamlit reuses its worker
        process across reruns, so a long-lived dashboard can hold a
        stale cache module from before ``normalize_package_name`` was
        added — re-importing it inside the request would crash with
        ``ImportError`` and break the page.

        Simulate that by stubbing the cache module to one that has no
        ``normalize_package_name`` attribute, then verify the dashboard
        function still normalises correctly.
        """
        import sys
        import types
        from ossuary.dashboard import utils as dash_utils

        broken_cache = types.ModuleType("ossuary.services.cache")
        # Deliberately omit ``normalize_package_name``.
        monkeypatch.setitem(sys.modules, "ossuary.services.cache", broken_cache)

        # Empty session backdrop is fine — we only need to prove the
        # function reaches the DB query without ImportError.
        called_with = {}

        class _FakeQuery:
            def filter(self, *_args, **_kwargs):
                # Capture the canonical name from the filter expression.
                for arg in _args:
                    if hasattr(arg, "right") and hasattr(arg.right, "value"):
                        if isinstance(arg.right.value, str):
                            called_with.setdefault("name", arg.right.value)
                return self
            def first(self):
                return None

        class _FakeSession:
            def query(self, _model):
                return _FakeQuery()

        class _FakeScope:
            def __enter__(self):
                return _FakeSession()
            def __exit__(self, *_a):
                return False

        def _fake_get_session():
            yield _FakeScope()

        monkeypatch.setattr(dash_utils, "get_session", _fake_get_session)

        # User typed a non-canonical PyPI spelling.
        result = dash_utils.get_score_history("PyYAML", "pypi")
        assert result == []
        # And the canonical form was actually used in the DB filter.
        assert called_with["name"] == "pyyaml"

    def test_normalize_helper_idempotent(self):
        """Re-normalising an already-canonical name is a no-op. Lookup
        sites can call it without worrying about double-application."""
        canon = normalize_package_name("PyYAML", "pypi")
        assert canon == "pyyaml"
        assert normalize_package_name(canon, "pypi") == canon

    def test_cli_history_finds_canonicalized_row(self, tmp_path, monkeypatch):
        """``ossuary history PyYAML -e pypi`` must find the row even
        though the DB stores it under the canonical ``pyyaml``. Smoke
        tests the full CLI command against a real SQLite file."""
        from datetime import datetime
        from typer.testing import CliRunner

        # Point the DB module at a fresh SQLite file, then re-import the
        # session module so the engine binds to it.
        db_path = tmp_path / "ossuary.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

        # The session module captures DATABASE_URL at import time, so
        # rebuild the engine + sessionmaker against the new URL.
        import importlib
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from ossuary.db import session as db_session
        from ossuary.db.models import Base, Score
        new_engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )
        monkeypatch.setattr(db_session, "engine", new_engine)
        monkeypatch.setattr(
            db_session, "SessionLocal",
            sessionmaker(autocommit=False, autoflush=False, bind=new_engine),
        )
        Base.metadata.create_all(bind=new_engine)

        # Seed a row under the canonical name.
        with db_session.session_scope() as s:
            cache = ScoreCache(s)
            pkg = cache.get_or_create_package("pyyaml", "pypi")
            s.add(Score(
                package_id=pkg.id,
                calculated_at=datetime(2026, 4, 17, 10, 0, 0),
                cutoff_date=datetime(2026, 4, 17, 10, 0, 0),
                final_score=42,
                risk_level="MODERATE",
                base_risk=40,
                activity_modifier=2,
                protective_factors_total=0,
                sentiment_modifier=0,
                breakdown={},
                maintainer_concentration=50.0,
                commits_last_year=10,
                unique_contributors=3,
                weekly_downloads=1000,
            ))

        from ossuary.cli import app
        runner = CliRunner()

        # User types the original capitalisation.
        result = runner.invoke(app, ["history", "PyYAML", "-e", "pypi", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["package"] == "pyyaml"  # canonical form
        assert payload["records"][0]["score"] == 42

        # And without -e the same call still finds it (PyPI fallback).
        result2 = runner.invoke(app, ["history", "PyYAML", "--json"])
        assert result2.exit_code == 0, result2.output
        assert json.loads(result2.output)["package"] == "pyyaml"
