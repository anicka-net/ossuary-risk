"""Current-vs-historical Score row separation + cache rehydration fixes.

Background (June 2026 full-repo review): ``get_current_score`` filtered
only on ``cutoff_date >= now - freshness``, so a ``--cutoff`` run from
earlier in the freshness window was served as the package's *current*
score — despite being computed with sponsors zeroed and visibility
neutralized. Symmetrically, ``get_historical_scores`` returned any
recent Score rows, so a package scored repeatedly via batch/API
accumulated enough current rows to be served as a fake "24-month"
series. The ``Score.is_historical`` tag pins both directions.

Also pins: burnout escalation surviving ``_rebuild_breakdown`` (it was
the only protective factor not reconstructed from the breakdown JSON,
so every cache hit silently dropped 10 points of explanation), and the
naive-UTC normalization in ``repo_cache._parse_datetime``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ossuary._compat import utcnow_naive
from ossuary.db.models import Base
from ossuary.services.cache import ScoreCache


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _store(cache, package, *, cutoff, score=50, is_historical=False, risk_level="MEDIUM"):
    return cache.store_score(
        package=package,
        cutoff_date=cutoff,
        final_score=score,
        risk_level=risk_level,
        base_risk=40,
        activity_modifier=0,
        protective_factors_total=10,
        breakdown={},
        maintainer_concentration=80.0,
        commits_last_year=10,
        unique_contributors=3,
        is_historical=is_historical,
    )


class TestCurrentVsHistoricalRows:
    def test_recent_cutoff_run_not_served_as_current(self, session):
        cache = ScoreCache(session)
        pkg = cache.get_or_create_package("leftpad", "npm")
        # A --cutoff run from two days ago, inside the 7-day window.
        _store(
            cache, pkg,
            cutoff=utcnow_naive() - timedelta(days=2),
            score=20, is_historical=True,
        )
        session.flush()
        assert cache.get_current_score(pkg) is None

    def test_current_row_still_served(self, session):
        cache = ScoreCache(session)
        pkg = cache.get_or_create_package("leftpad", "npm")
        _store(cache, pkg, cutoff=utcnow_naive(), score=65)
        session.flush()
        current = cache.get_current_score(pkg)
        assert current is not None and current.final_score == 65

    def test_history_series_excludes_current_rows(self, session):
        cache = ScoreCache(session)
        pkg = cache.get_or_create_package("leftpad", "npm")
        # 30 current rows scored on consecutive days must NOT satisfy a
        # months=24 history lookup.
        for d in range(30):
            _store(cache, pkg, cutoff=utcnow_naive() - timedelta(days=d))
        session.flush()
        assert cache.get_historical_scores(pkg, months=24) == []

    def test_history_series_excludes_insufficient_data_rows(self, session):
        cache = ScoreCache(session)
        pkg = cache.get_or_create_package("leftpad", "npm")
        month = datetime(2025, 3, 1)
        _store(cache, pkg, cutoff=month, is_historical=True)
        bad = cache.store_score(
            package=pkg,
            cutoff_date=datetime(2025, 4, 1),
            final_score=None,
            risk_level="INSUFFICIENT_DATA",
            base_risk=None,
            activity_modifier=None,
            protective_factors_total=None,
            breakdown={},
            maintainer_concentration=None,
            commits_last_year=None,
            unique_contributors=None,
            is_historical=True,
        )
        assert bad is not None
        session.flush()
        rows = cache.get_historical_scores(pkg, months=24)
        assert [r.cutoff_date for r in rows] == [month]


class TestRebuildBreakdownBurnout:
    def test_burnout_escalation_survives_cache_round_trip(self):
        from ossuary.scoring.factors import ProtectiveFactors
        from ossuary.services.scorer import _rebuild_breakdown

        pf = ProtectiveFactors(
            frustration_score=15,
            burnout_escalation_score=10,
            burnout_escalation_evidence="Frustration detected with bus_factor=1",
        )

        class FakeScore:
            breakdown = {
                "package": {"repo_url": "https://github.com/o/r"},
                "metrics": {},
                "chaoss_signals": {},
                "score": {"components": {"protective_factors": pf.to_dict()}},
            }
            risk_level = "HIGH"
            final_score = 65
            base_risk = 40
            activity_modifier = 0
            maintainer_concentration = 90.0
            commits_last_year = 100
            unique_contributors = 2
            weekly_downloads = 1000

        rebuilt = _rebuild_breakdown(FakeScore(), "rayon", "cargo")
        assert rebuilt is not None
        assert rebuilt.protective_factors.burnout_escalation_score == 10
        assert rebuilt.protective_factors.burnout_escalation_evidence
        # The factor total must reproduce what was scored, not run 10 low.
        assert rebuilt.protective_factors.total == pf.total


class TestParseDatetimeUtcNormalization:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("2024-01-01T12:00:00Z", datetime(2024, 1, 1, 12, 0)),
            # Non-UTC offsets must convert, not just drop the tz.
            ("2024-01-01T12:00:00+02:00", datetime(2024, 1, 1, 10, 0)),
            ("2024-01-01T12:00:00", datetime(2024, 1, 1, 12, 0)),
        ],
    )
    def test_normalises_to_naive_utc(self, given, expected):
        from ossuary.services.repo_cache import _parse_datetime
        assert _parse_datetime(given) == expected
