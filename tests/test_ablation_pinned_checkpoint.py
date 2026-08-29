"""Guards for the ablation harness current-state checkpoint defect.

Bug history (2026-08-29): ``scripts/ablation.py`` scored current-state
cases (controls and T_risk, ``cutoff_date=None``) at ``datetime.now()``
against SLA-served snapshots, so a final ablation run on 28 August
silently evaluated current-state evidence at a 28-August cutoff while
the canonical validation checkpoint was 15 August 2026. The frozen
snapshot blobs were identical; the scoring cutoff was not. One control
(isarray) moved 40 (canonical, TN) -> 60 (harness, FP) purely from the
13-day window shift on a near-zero-activity package, and the harness
baseline then differed from the canonical matrix by more than the
disclosed pyphetools-fixture difference.

The fix: final runs must pin the canonical current-state checkpoint
(``--replay-instant`` + ``--snapshot-collected-before``, cache-only
replay, exactly like ``scripts/validate.py``). Run-time scoring requires
an explicit ``--allow-run-time-cutoff`` opt-in. These tests pin that
contract so the defect cannot recur silently.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from validate import ValidationCase  # noqa: E402

from ablation import (  # noqa: E402
    case_key,
    check_arg_compatibility,
    collect_all,
)

REPLAY = datetime(2026, 8, 15, 15, 49, 38, 364446)
SNAPSHOT_BEFORE = datetime(2026, 8, 15, 18, 48, 0)


class TestArgGuard:
    def test_no_checkpoint_without_opt_in_is_rejected(self):
        """The core regression guard: a bare invocation must be refused
        instead of silently scoring current-state cases at run time."""
        msg = check_arg_compatibility(None, None, False)
        assert msg is not None
        assert "run time" in msg

    def test_replay_without_collected_before_is_rejected(self):
        assert check_arg_compatibility(REPLAY, None, False) is not None

    def test_collected_before_without_replay_is_rejected(self):
        assert check_arg_compatibility(None, SNAPSHOT_BEFORE, False) is not None

    def test_pinned_pair_is_accepted(self):
        assert check_arg_compatibility(REPLAY, SNAPSHOT_BEFORE, False) is None

    def test_run_time_opt_in_is_accepted(self):
        assert check_arg_compatibility(None, None, True) is None

    def test_opt_in_is_rejected_with_replay(self):
        # Ambiguous combination: pinned instant wins, so refuse rather
        # than guess which mode the caller meant.
        assert check_arg_compatibility(REPLAY, SNAPSHOT_BEFORE, True) is not None


class TestPinnedCollectionSemantics:
    def test_current_state_case_uses_pinned_instant_for_collect_and_score(self):
        control = ValidationCase(
            name="isarray", ecosystem="npm",
            expected_outcome="safe", cutoff_date=None,
        )
        calls = {}

        async def fake_cached_collect(name, ecosystem, repo_url, cutoff_date=None,
                                      cache_only=False,
                                      snapshot_collected_before=None):
            calls["args"] = {
                "cutoff_date": cutoff_date,
                "cache_only": cache_only,
                "snapshot_collected_before": snapshot_collected_before,
            }
            return "data", []

        with patch("ablation.cached_collect", side_effect=fake_cached_collect):
            cache = asyncio.run(collect_all(
                [control],
                current_state_cutoff=REPLAY,
                snapshot_collected_before=SNAPSHOT_BEFORE,
                cache_only=True,
            ))

        # Collection must use the pinned replay instant (frozen-snapshot
        # replay lookup), never None/SLA mode…
        assert calls["args"]["cutoff_date"] == REPLAY
        # …never contact upstreams…
        assert calls["args"]["cache_only"] is True
        # …and never accept snapshots collected after the freeze bound.
        assert calls["args"]["snapshot_collected_before"] == SNAPSHOT_BEFORE
        # The scoring cutoff must be the pinned instant, not datetime.now().
        _, score_cutoff, _ = cache[case_key(control)]
        assert score_cutoff == REPLAY

    def test_historical_case_keeps_declared_cutoff_in_pinned_mode(self):
        incident = ValidationCase(
            name="tukaani-project/xz", ecosystem="github",
            expected_outcome="incident", tier="T1",
            cutoff_date="2024-03-01",
        )
        calls = {}

        async def fake_cached_collect(name, ecosystem, repo_url, cutoff_date=None,
                                      cache_only=False,
                                      snapshot_collected_before=None):
            calls["args"] = {"cutoff_date": cutoff_date}
            return "data", []

        with patch("ablation.cached_collect", side_effect=fake_cached_collect):
            cache = asyncio.run(collect_all(
                [incident],
                current_state_cutoff=REPLAY,
                snapshot_collected_before=SNAPSHOT_BEFORE,
                cache_only=True,
            ))

        # Historical incidents keep their declared T-1 end-of-day cutoff…
        assert calls["args"]["cutoff_date"] == datetime(
            2024, 3, 1, 23, 59, 59, 999999
        )
        # …and are scored at that cutoff.
        _, score_cutoff, _ = cache[case_key(incident)]
        assert score_cutoff == datetime(2024, 3, 1, 23, 59, 59, 999999)

    def test_run_time_mode_matches_legacy_behaviour_for_current_state(self):
        """Without a pinned checkpoint, collection stays SLA-mode
        (cutoff_date=None) — the behaviour the dedup tests were written
        against. The guard against this mode lives in the CLI, not here."""
        control = ValidationCase(
            name="isarray", ecosystem="npm",
            expected_outcome="safe", cutoff_date=None,
        )
        calls = {}

        async def fake_cached_collect(name, ecosystem, repo_url, cutoff_date=None,
                                      cache_only=False,
                                      snapshot_collected_before=None):
            calls["args"] = {"cutoff_date": cutoff_date}
            return "data", []

        with patch("ablation.cached_collect", side_effect=fake_cached_collect):
            cache = asyncio.run(collect_all([control]))

        assert calls["args"]["cutoff_date"] is None
        _, score_cutoff, _ = cache[case_key(control)]
        assert score_cutoff is not None
