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


class TestRegistryProbePrePass:
    """Optional probe pre-pass for ``--repo-aware``: pip-list / gem-list
    style seeds carry no explicit ``repo_url`` and would otherwise all
    fall into ``unplanable`` (no grouping benefit). The probe pre-pass
    runs one cheap registry call per such entry to learn its canonical
    URL upfront, so sibling packages from the same monorepo
    (nvidia-cuda-*, jupyter-*) get grouped instead of racing in
    parallel.
    """

    def test_probe_resolves_unplanable_into_groups(self):
        """Three sibling entries with no explicit URL — probe returns
        the same canonical URL for all three — they collapse into one
        group of three (sequential within), not three unplanable
        singletons."""
        from ossuary.services.scorer import RegistryData, ScoringResult

        entries = [
            _entry("nvidia-cuda-cublas", "pypi"),
            _entry("nvidia-cuda-cudart", "pypi"),
            _entry("nvidia-cuda-cufft", "pypi"),
        ]
        shared_url = "https://github.com/nvidia/cuda-python"

        async def fake_probe(name, ecosystem, repo_url=None):
            return RegistryData(
                repo_url=shared_url,
                weekly_downloads=100,
                fetch_errors=[],
                warnings=[],
            )

        async def fake_score_package(name, eco, force=False, **kwargs):
            return ScoringResult(success=True, breakdown=None)

        with patch(
            "ossuary.services.scorer._collect_registry_data",
            side_effect=fake_probe,
        ), patch(
            "ossuary.services.batch.score_package",
            side_effect=fake_score_package,
        ), patch(
            "ossuary.services.batch.is_fresh", return_value=False,
        ):
            result = asyncio.run(batch_score(
                entries, max_concurrent=5, skip_fresh=False,
                repo_aware=True, probe_registries=True,
            ))

        # All three landed in a single group via the probe.
        assert result.scored == 3
        assert result.unique_repos == 1
        assert result.shared_repo_packages == 3
        assert result.unplanable == 0
        assert result.probed == 3
        assert result.probe_resolved == 3

    def test_probe_failure_leaves_entries_unplanable(self):
        """When the registry probe returns no URL, the entry stays in
        ``unplanable`` and falls through to the standard parallel path
        — no error, no crash."""
        from ossuary.services.scorer import RegistryData, ScoringResult

        entries = [
            _entry("orphan-pkg-1", "pypi"),
            _entry("orphan-pkg-2", "pypi"),
        ]

        async def fake_probe(name, ecosystem, repo_url=None):
            return RegistryData(
                repo_url=None,  # no URL discoverable
                weekly_downloads=10,
                fetch_errors=[],
                warnings=[],
            )

        async def fake_score_package(name, eco, force=False, **kwargs):
            return ScoringResult(success=True, breakdown=None)

        with patch(
            "ossuary.services.scorer._collect_registry_data",
            side_effect=fake_probe,
        ), patch(
            "ossuary.services.batch.score_package",
            side_effect=fake_score_package,
        ), patch(
            "ossuary.services.batch.is_fresh", return_value=False,
        ):
            result = asyncio.run(batch_score(
                entries, max_concurrent=5, skip_fresh=False,
                repo_aware=True, probe_registries=True,
            ))

        assert result.scored == 2
        assert result.unique_repos == 0
        assert result.unplanable == 2
        assert result.probed == 2
        assert result.probe_resolved == 0

    def test_probe_only_runs_for_entries_lacking_url(self):
        """Entries that already have a URL skip the probe; only the
        URL-less ones get probed. Cuts unnecessary HTTP for mixed
        batches."""
        from ossuary.services.scorer import RegistryData, ScoringResult

        entries = [
            _entry("known", "npm", url="https://github.com/known/known"),
            _entry("unknown", "pypi"),  # needs probe
        ]

        probed_names: list[str] = []

        async def fake_probe(name, ecosystem, repo_url=None):
            probed_names.append(name)
            return RegistryData(
                repo_url="https://github.com/found/found",
                weekly_downloads=100,
                fetch_errors=[], warnings=[],
            )

        async def fake_score_package(name, eco, force=False, **kwargs):
            return ScoringResult(success=True, breakdown=None)

        with patch(
            "ossuary.services.scorer._collect_registry_data",
            side_effect=fake_probe,
        ), patch(
            "ossuary.services.batch.score_package",
            side_effect=fake_score_package,
        ), patch(
            "ossuary.services.batch.is_fresh", return_value=False,
        ):
            result = asyncio.run(batch_score(
                entries, max_concurrent=5, skip_fresh=False,
                repo_aware=True, probe_registries=True,
            ))

        # Only the URL-less entry got probed.
        assert probed_names == ["unknown"]
        assert result.probed == 1
        assert result.probe_resolved == 1

    def test_probe_disabled_preserves_old_unplanable_behavior(self):
        """``probe_registries=False`` (the default) — repo-aware mode
        without the pre-pass leaves URL-less entries in unplanable,
        same as before. Pin so the new flag is opt-in only."""
        from ossuary.services.scorer import ScoringResult

        entries = [
            _entry("urlless-1", "pypi"),
            _entry("urlless-2", "pypi"),
        ]

        async def fake_probe(name, ecosystem, repo_url=None):
            raise AssertionError("probe must not be called when flag is off")

        async def fake_score_package(name, eco, force=False, **kwargs):
            return ScoringResult(success=True, breakdown=None)

        with patch(
            "ossuary.services.scorer._collect_registry_data",
            side_effect=fake_probe,
        ), patch(
            "ossuary.services.batch.score_package",
            side_effect=fake_score_package,
        ), patch(
            "ossuary.services.batch.is_fresh", return_value=False,
        ):
            result = asyncio.run(batch_score(
                entries, max_concurrent=5, skip_fresh=False,
                repo_aware=True, probe_registries=False,
            ))

        assert result.scored == 2
        assert result.unplanable == 2
        assert result.probed == 0
        assert result.probe_resolved == 0

    def test_probe_exception_does_not_crash_run(self):
        """Defensive: a probe that raises shouldn't take down the
        whole batch. The entry just stays unplanable."""
        from ossuary.services.scorer import ScoringResult

        entries = [_entry("flaky", "pypi")]

        async def fake_probe(name, ecosystem, repo_url=None):
            raise RuntimeError("simulated registry meltdown")

        async def fake_score_package(name, eco, force=False, **kwargs):
            return ScoringResult(success=True, breakdown=None)

        with patch(
            "ossuary.services.scorer._collect_registry_data",
            side_effect=fake_probe,
        ), patch(
            "ossuary.services.batch.score_package",
            side_effect=fake_score_package,
        ), patch(
            "ossuary.services.batch.is_fresh", return_value=False,
        ):
            result = asyncio.run(batch_score(
                entries, max_concurrent=5, skip_fresh=False,
                repo_aware=True, probe_registries=True,
            ))

        assert result.scored == 1
        assert result.unplanable == 1
        assert result.probed == 1
        assert result.probe_resolved == 0

    def test_probe_result_is_threaded_through_to_score_package(self):
        """Pin the no-double-call contract that justifies the
        'no net new HTTP per probed entry' wording in --probe-registries
        help. The pre-pass calls _collect_registry_data once per
        URL-less entry, then the result is plumbed via
        score_package(prefetched_registry=...) so cached_collect
        reuses it instead of refetching. Without plumbing, we'd see
        2 registry calls per entry (probe + cached_collect's own
        internal probe). Counts the side-effect to assert exactly 1."""
        from ossuary.services.scorer import (
            RegistryData, ScoringResult, score_package,
        )

        entries = [
            _entry("pkg-a", "pypi"),
            _entry("pkg-b", "pypi"),
        ]
        registry_call_log: list[str] = []

        async def fake_collect_registry_data(name, ecosystem, repo_url=None):
            registry_call_log.append(name)
            return RegistryData(
                repo_url=f"https://github.com/shared/{name}",
                weekly_downloads=100,
                fetch_errors=[], warnings=[],
            )

        prefetched_seen: list[bool] = []

        async def fake_score_package(name, eco, force=False, **kwargs):
            # Capture whether the prefetched_registry was threaded
            # through — that's the actual contract.
            prefetched_seen.append("prefetched_registry" in kwargs)
            return ScoringResult(success=True, breakdown=None)

        with patch(
            "ossuary.services.scorer._collect_registry_data",
            side_effect=fake_collect_registry_data,
        ), patch(
            "ossuary.services.batch.score_package",
            side_effect=fake_score_package,
        ), patch(
            "ossuary.services.batch.is_fresh", return_value=False,
        ):
            asyncio.run(batch_score(
                entries, max_concurrent=5, skip_fresh=False,
                repo_aware=True, probe_registries=True,
            ))

        # Pre-pass runs the probe exactly once per URL-less entry.
        # The fake_score_package above intercepts before reaching
        # cached_collect, so we can't observe the avoided second call
        # directly here — but we CAN observe that the prefetched
        # RegistryData was threaded through, which is the wiring that
        # makes cached_collect skip its internal probe.
        assert sorted(registry_call_log) == ["pkg-a", "pkg-b"], (
            f"probe should run exactly once per entry, got: "
            f"{registry_call_log}"
        )
        assert prefetched_seen == [True, True], (
            f"score_package must receive prefetched_registry kwarg "
            f"so cached_collect can skip its internal probe — "
            f"otherwise we silently double-fetch. Got: {prefetched_seen}"
        )

    def test_cached_collect_skips_internal_probe_when_prefetched_given(self):
        """Companion to the above test, at the cached_collect layer.
        Pins the receiving end of the contract: when a caller passes
        prefetched_registry, cached_collect must NOT call
        _collect_registry_data internally. Without this, the plumbing
        on the batch side would still result in 2 calls."""
        import asyncio
        from ossuary.services.scorer import (
            RegistryData, cached_collect, _collect_registry_data,
        )

        prefetched = RegistryData(
            repo_url="https://github.com/found/it",
            weekly_downloads=999,
            fetch_errors=[], warnings=[],
        )

        internal_call_log: list[str] = []

        async def spy(name, ecosystem, repo_url=None):
            internal_call_log.append(name)
            return RegistryData(
                repo_url=None, weekly_downloads=None,
                fetch_errors=["should not be called"], warnings=[],
            )

        # cached_collect needs use_cache=False to bypass DB session
        # paths it has no DB for in this test, but the prefetched-vs-
        # internal-probe gate is also on the use_cache=True path —
        # both branches must respect prefetched.
        async def go():
            with patch(
                "ossuary.services.scorer._collect_registry_data",
                side_effect=spy,
            ):
                # use_cache=False → snapshot block skipped; goes straight
                # to the prefetched-or-fetch decision (which we want to
                # pin: prefetched provided → no internal call).
                await cached_collect(
                    "x", "pypi",
                    prefetched_registry=prefetched,
                    use_cache=False,
                )
        # Suppress the downstream collect_package_data call which
        # would otherwise try real network — make it a no-op coroutine.
        async def _noop_collect(*_a, **_kw):
            return None, []

        with patch(
            "ossuary.services.scorer.collect_package_data",
            side_effect=_noop_collect,
        ):
            asyncio.run(go())

        assert internal_call_log == [], (
            f"cached_collect must not call _collect_registry_data "
            f"when prefetched_registry is provided. Got: "
            f"{internal_call_log}"
        )
