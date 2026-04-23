"""Regression tests for the ablation harness duplicate-name fix.

Bug history: the validation set deliberately contains duplicate (name,
ecosystem) pairs — ``chalk`` and ``axios`` each appear once as a control
(current state) and once as a 2026 T4 incident (pre-compromise cutoff).
The pre-fix ablation harness keyed its data cache on ``case.name`` alone,
so the second iteration overwrote the first's data and both rows then
scored against the wrong inputs. The thesis-cited per-case dump in
``thesis/ablation_results.json`` was contaminated. GPT review caught it.

These tests pin the fix: the cache must be keyed on the full
``case_key()`` tuple, and ``collect_all()`` and ``run_pass()`` must agree
on the key.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from validate import ValidationCase  # noqa: E402

from ablation import case_key, collect_all  # noqa: E402


class TestCaseKey:
    def test_duplicate_names_get_distinct_keys(self):
        control = ValidationCase(
            name="chalk", ecosystem="npm", expected_outcome="safe", cutoff_date=None,
        )
        incident = ValidationCase(
            name="chalk", ecosystem="npm", expected_outcome="incident",
            tier="T4", cutoff_date="2025-09-01",
        )
        assert case_key(control) != case_key(incident)

    def test_same_case_keys_identically(self):
        a = ValidationCase(
            name="lodash", ecosystem="npm", expected_outcome="safe", cutoff_date=None,
        )
        b = ValidationCase(
            name="lodash", ecosystem="npm", expected_outcome="safe", cutoff_date=None,
        )
        assert case_key(a) == case_key(b)


class TestCollectAllDeduplication:
    def test_duplicate_names_each_get_own_cache_slot(self):
        """``cache[case.name]`` (the bug) collapses two cases into one slot;
        ``cache[case_key(case)]`` (the fix) keeps them separate."""
        control = ValidationCase(
            name="chalk", ecosystem="npm", expected_outcome="safe", cutoff_date=None,
        )
        incident = ValidationCase(
            name="chalk", ecosystem="npm", expected_outcome="incident",
            tier="T4", cutoff_date="2025-09-01",
        )

        # Mock cached_collect to return a sentinel object per call so we
        # can verify each slot stored a *distinct* result.
        call_count = {"n": 0}

        async def fake_cached_collect(name, ecosystem, repo_url, cutoff_date=None):
            call_count["n"] += 1
            # Return a sentinel that records which call produced it.
            return f"data-call-{call_count['n']}", []

        with patch("ablation.cached_collect", side_effect=fake_cached_collect):
            cache = asyncio.run(collect_all([control, incident]))

        # Both cases must have their own cache slot — no overwrite.
        assert len(cache) == 2, (
            "Cache collapsed two distinct chalk cases into one slot. "
            "Bug 1 from GPT review has regressed."
        )

        # The slot keys must distinguish control from incident.
        assert case_key(control) in cache
        assert case_key(incident) in cache
        # Each slot holds the data fetched for that case (different sentinels).
        control_data = cache[case_key(control)][0]
        incident_data = cache[case_key(incident)][0]
        assert control_data != incident_data

    def test_duplicate_names_get_distinct_cutoffs(self):
        """The cutoff stored in each slot must match the case's own cutoff,
        not whichever case was iterated last."""
        from datetime import datetime

        control = ValidationCase(
            name="axios", ecosystem="npm", expected_outcome="safe", cutoff_date=None,
        )
        incident = ValidationCase(
            name="axios", ecosystem="npm", expected_outcome="incident",
            tier="T4", cutoff_date="2026-03-30",
        )

        async def fake_cached_collect(name, ecosystem, repo_url, cutoff_date=None):
            return "data", []

        with patch("ablation.cached_collect", side_effect=fake_cached_collect):
            cache = asyncio.run(collect_all([control, incident]))

        # Control: cutoff_date=None → cutoff_for_score = datetime.now() (a now-ish value)
        # Incident: cutoff_for_score = 2026-03-30 exactly.
        _, control_cutoff, _ = cache[case_key(control)]
        _, incident_cutoff, _ = cache[case_key(incident)]
        assert incident_cutoff == datetime(2026, 3, 30)
        # Control cutoff must NOT be the incident's cutoff (the bug would
        # have made them equal because cache[name] was last-write-wins).
        assert control_cutoff != incident_cutoff
