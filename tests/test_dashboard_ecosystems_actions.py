"""Dashboard regression: per-ecosystem Retry / Re-score buttons.

Pins two contracts on ``dashboard/pages/1_Ecosystems.py``:

1. The page exposes a ``Retry N unscored`` button when there are
   packages with ``last_analyzed=None`` in the selected ecosystem, and
   that button calls ``score_package`` with ``use_cache=False`` (true
   bypass — needed to escape negative-cache entries like the cargo
   ``agg`` typo case).

2. The page exposes a ``Re-score all N`` button whenever the ecosystem
   has any tracked packages, and that button calls ``score_package``
   with ``force=True, use_cache=True`` (bypass score cache only — keeps
   snapshot reuse for cheap repeats).

Background: a previous version of the helper passed ``force=False``
silently, so "Re-score all" returned cached scores without recollecting
— the GPT review caught the overclaim. These tests pin the corrected
semantics so the regression cannot reappear.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest

from ossuary._compat import utcnow_naive
from ossuary.db.models import Base, Package, Score


PAGE_PATH = "src/ossuary/dashboard/pages/1_Ecosystems.py"


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Seed a temp SQLite DB with one scored + one orphan packagist row."""
    db_path = tmp_path / "ossuary.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    from ossuary.db import session as db_session

    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(
        db_session, "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as s:
        scored = Package(
            name="laravel/framework", ecosystem="packagist",
            repo_url="https://github.com/laravel/framework",
            last_analyzed=utcnow_naive(),
        )
        orphan = Package(
            name="phpunit/phpunit", ecosystem="packagist",
            repo_url="https://github.com/sebastianbergmann/phpunit",
            last_analyzed=None,
        )
        s.add_all([scored, orphan])
        s.flush()
        s.add(Score(
            package_id=scored.id,
            calculated_at=utcnow_naive(),
            cutoff_date=datetime(2026, 4, 24),
            final_score=10, risk_level="VERY_LOW",
            base_risk=10, activity_modifier=0,
            protective_factors_total=0, sentiment_modifier=0,
            breakdown={}, maintainer_concentration=20.0,
            commits_last_year=200, unique_contributors=50,
            weekly_downloads=1_000_000,
        ))
        s.commit()

    return db_path


def _captured_score(call_log: list[dict]):
    """Return an AsyncMock that records each invocation into ``call_log``
    and returns a stub success result."""
    from ossuary.services.scorer import ScoringResult

    async def _fake(name, ecosystem, **kwargs):
        call_log.append({"name": name, "ecosystem": ecosystem, **kwargs})

        class _BD:
            final_score = 10
            risk_level = type("RL", (), {"value": "VERY_LOW"})()
            is_provisional = False
            def to_dict(self):
                return {}

        return ScoringResult(success=True, breakdown=_BD())

    return AsyncMock(side_effect=_fake)


class TestEcosystemsActions:
    def test_orphan_present_renders_both_buttons(self, seeded_db):
        at = AppTest.from_file(PAGE_PATH, default_timeout=20)
        at.run()
        keys = {b.key for b in at.button}
        assert "retry_unscored_packagist" in keys, (
            "Retry button must appear when an orphan package exists"
        )
        assert "rescore_all_packagist" in keys, (
            "Re-score-all button must always appear when ecosystem has packages"
        )

    def test_retry_button_bypasses_all_caches(self, seeded_db, monkeypatch):
        """Clicking Retry must call score_package with use_cache=False so
        negative-cached failures (failure_kind=repo_not_found etc.) get a
        real re-attempt instead of being served from cache."""
        from ossuary.services import scorer as scorer_mod
        call_log: list[dict] = []
        monkeypatch.setattr(
            scorer_mod, "score_package", _captured_score(call_log),
        )

        at = AppTest.from_file(PAGE_PATH, default_timeout=20)
        at.run()
        retry_btn = next(
            b for b in at.button if b.key == "retry_unscored_packagist"
        )
        retry_btn.click()
        at.run()

        assert len(call_log) == 1, (
            f"expected 1 score_package call (1 orphan), got {len(call_log)}: "
            f"{call_log}"
        )
        invocation = call_log[0]
        assert invocation["name"] == "phpunit/phpunit"
        assert invocation["ecosystem"] == "packagist"
        assert invocation["use_cache"] is False, (
            "Retry must pass use_cache=False to bypass the negative cache. "
            f"Got: {invocation}"
        )
        assert invocation["force"] is True, (
            "Retry should also pass force=True for symmetry. "
            f"Got: {invocation}"
        )

    def test_rescore_all_bypasses_score_cache_only(
        self, seeded_db, monkeypatch
    ):
        """Clicking Re-score all must call score_package with force=True
        (so cached scores are bypassed) but use_cache=True (so the
        snapshot cache still serves cheap repeats). The previous version
        passed force=False and silently returned cached scores —
        regression pin."""
        from ossuary.services import scorer as scorer_mod
        call_log: list[dict] = []
        monkeypatch.setattr(
            scorer_mod, "score_package", _captured_score(call_log),
        )

        at = AppTest.from_file(PAGE_PATH, default_timeout=20)
        at.run()
        rescore_btn = next(
            b for b in at.button if b.key == "rescore_all_packagist"
        )
        rescore_btn.click()
        at.run()

        # Both packages in the eco get re-scored.
        assert len(call_log) == 2, (
            f"expected 2 score_package calls (2 packages), got {len(call_log)}"
        )
        for invocation in call_log:
            assert invocation["force"] is True, (
                "Re-score all must pass force=True so cached scores are "
                f"bypassed. Got: {invocation}"
            )
            assert invocation["use_cache"] is True, (
                "Re-score all should keep use_cache=True so the snapshot "
                f"cache still serves cheap repeats. Got: {invocation}"
            )
