"""Repo-aware batch scoring (v0.10.1 — phase 3 step 5).

The cache layers (1a + 1b) deduplicate work across packages mapping to
the same canonical repo URL — but only after one of them has written
the snapshot. With ``batch_score`` running N package scores in
parallel, multiple concurrent calls for the same repo can race and
each do their own GitHub fetch before any snapshot lands. ``repo_aware``
mode pre-groups by canonical URL and serialises scoring within each
group so the first entry warms the cache before the rest hit it.

These tests pin:
- the planning groups entries by canonical URL (different spellings
  collapse to the same group)
- entries lacking a derivable URL fall into ``unplanable`` and don't
  block the rest
- the BatchResult telemetry (unique_repos / shared_repo_packages /
  unplanable) reflects the planning result
- in repo-aware mode, multi-package groups process sequentially so a
  shared repo is only fetched once
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ossuary.services.batch import (
    BatchResult,
    PackageEntry,
    _build_repo_plan,
    batch_score,
)


def _entry(name: str, eco: str, url: str = "", owner: str = "", repo: str = "") -> PackageEntry:
    return PackageEntry(
        obs_package=name,
        github_owner=owner,
        github_repo=repo,
        repo_url=url,
        source="custom",
        ecosystem=eco,
    )


class TestBuildRepoPlan:
    def test_groups_entries_by_canonical_url(self):
        e1 = _entry("axios-a", "npm", url="https://github.com/axios/axios")
        e2 = _entry("axios-b", "npm", url="https://github.com/Axios/Axios.git/")
        e3 = _entry("requests", "pypi", url="https://github.com/psf/requests")

        plan = _build_repo_plan([e1, e2, e3])
        assert len(plan.groups) == 2
        # axios-a and axios-b should land together despite differing spellings.
        ax_group = plan.groups.get("https://github.com/axios/axios")
        assert ax_group is not None
        assert {e.obs_package for e in ax_group} == {"axios-a", "axios-b"}
        assert plan.unplanable == []

    def test_github_eco_url_derivable_from_owner_repo(self):
        """Entries with no explicit ``repo_url`` but populated
        ``github_owner`` / ``github_repo`` (the discovery JSON path)
        should still be planable."""
        e = _entry("acme/widget", "github", owner="acme", repo="widget")
        plan = _build_repo_plan([e])
        assert "https://github.com/acme/widget" in plan.groups
        assert plan.unplanable == []

    def test_non_github_without_url_is_unplanable(self):
        """A pypi entry with no explicit URL flows through the standard
        path (planning would require a registry probe upfront)."""
        e = _entry("requests", "pypi")
        plan = _build_repo_plan([e])
        assert plan.groups == {}
        assert len(plan.unplanable) == 1
        assert plan.unplanable[0].obs_package == "requests"

    def test_mixed_planable_and_unplanable(self):
        e_planable = _entry("axios", "npm", url="https://github.com/axios/axios")
        e_unplanable = _entry("requests", "pypi")
        plan = _build_repo_plan([e_planable, e_unplanable])
        assert len(plan.groups) == 1
        assert len(plan.unplanable) == 1


class TestRepoAwareBatchScore:
    """End-to-end test of the repo-aware mode: mock score_package so we
    can observe the call ordering. The contract: within a group, calls
    are sequential (first finishes before second starts); across
    groups, calls run in parallel up to the semaphore."""

    def test_within_group_calls_are_sequential(self):
        """Three packages mapping to the same repo should be scored
        sequentially in repo-aware mode, not all three at once."""
        from ossuary.services.scorer import ScoringResult
        from ossuary.scoring.factors import RiskBreakdown

        entries = [
            _entry(f"pkg-{i}", "npm", url="https://github.com/shared/repo")
            for i in range(3)
        ]

        in_flight = 0
        max_in_flight = 0
        call_order = []

        async def fake_score_package(name, eco, force=False, **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            call_order.append(name)
            await asyncio.sleep(0.01)  # let other tasks attempt to interleave
            in_flight -= 1
            # Return a successful ScoringResult shape.
            return ScoringResult(success=True, breakdown=None)

        with patch(
            "ossuary.services.batch.score_package",
            side_effect=fake_score_package,
        ), patch(
            "ossuary.services.batch.is_fresh", return_value=False,
        ):
            result = asyncio.run(batch_score(
                entries, max_concurrent=10, skip_fresh=False,
                repo_aware=True,
            ))

        # All three landed under one group; max-in-flight must be 1.
        assert max_in_flight == 1, (
            f"expected sequential within-group scoring, got "
            f"max_in_flight={max_in_flight}"
        )
        assert call_order == ["pkg-0", "pkg-1", "pkg-2"]
        assert result.scored == 3
        assert result.unique_repos == 1
        assert result.shared_repo_packages == 3
        assert result.unplanable == 0

    def test_groups_run_in_parallel(self):
        """Two groups (different repos) with one package each should
        run in parallel up to the semaphore."""
        from ossuary.services.scorer import ScoringResult

        entries = [
            _entry("a-pkg", "npm", url="https://github.com/a/repo"),
            _entry("b-pkg", "npm", url="https://github.com/b/repo"),
        ]

        in_flight = 0
        max_in_flight = 0

        async def fake_score_package(name, eco, force=False, **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return ScoringResult(success=True, breakdown=None)

        with patch(
            "ossuary.services.batch.score_package",
            side_effect=fake_score_package,
        ), patch(
            "ossuary.services.batch.is_fresh", return_value=False,
        ):
            asyncio.run(batch_score(
                entries, max_concurrent=2, skip_fresh=False,
                repo_aware=True,
            ))

        # Two singleton groups should be able to run concurrently.
        assert max_in_flight == 2, (
            f"expected parallel across groups, got max_in_flight={max_in_flight}"
        )

    def test_default_mode_still_runs_in_parallel(self):
        """``repo_aware=False`` (the default) preserves the prior
        flat-parallel behaviour — even for entries that would have
        been pre-grouped, they all run in parallel under the
        semaphore."""
        from ossuary.services.scorer import ScoringResult

        entries = [
            _entry(f"pkg-{i}", "npm", url="https://github.com/shared/repo")
            for i in range(3)
        ]

        in_flight = 0
        max_in_flight = 0

        async def fake_score_package(name, eco, force=False, **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return ScoringResult(success=True, breakdown=None)

        with patch(
            "ossuary.services.batch.score_package",
            side_effect=fake_score_package,
        ), patch(
            "ossuary.services.batch.is_fresh", return_value=False,
        ):
            result = asyncio.run(batch_score(
                entries, max_concurrent=10, skip_fresh=False,
                repo_aware=False,
            ))

        # Without repo-aware mode, all three race in parallel.
        assert max_in_flight == 3
        # Telemetry fields stay at default (0) when not in repo-aware mode.
        assert result.unique_repos == 0
        assert result.shared_repo_packages == 0

    def test_unplanable_entries_still_get_scored(self):
        """Mixed batch: planable + unplanable. Both are scored in
        repo-aware mode; planable go through groups, unplanable
        through singleton tasks."""
        from ossuary.services.scorer import ScoringResult

        planable_a = _entry("a", "npm", url="https://github.com/x/y")
        planable_b = _entry("b", "npm", url="https://github.com/x/y")
        unplanable = _entry("c", "pypi")  # no explicit repo URL
        entries = [planable_a, planable_b, unplanable]

        scored_names = []

        async def fake_score_package(name, eco, force=False, **kwargs):
            scored_names.append(name)
            return ScoringResult(success=True, breakdown=None)

        with patch(
            "ossuary.services.batch.score_package",
            side_effect=fake_score_package,
        ), patch(
            "ossuary.services.batch.is_fresh", return_value=False,
        ):
            result = asyncio.run(batch_score(
                entries, max_concurrent=10, skip_fresh=False,
                repo_aware=True,
            ))

        assert sorted(scored_names) == ["a", "b", "c"]
        assert result.scored == 3
        assert result.unique_repos == 1  # planable_a + planable_b
        assert result.shared_repo_packages == 2
        assert result.unplanable == 1
