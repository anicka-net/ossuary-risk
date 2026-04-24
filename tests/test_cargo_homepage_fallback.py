"""Cargo homepage-URL fallback when ``repository`` 404s.

Background: crates.io's ``repository`` field is mutable free text and a
small number of crates ship with a typo (the canonical case is ``agg``:
``https://github.com/savge13/agg`` — missing 'a' — vs the correct
``savage13/agg`` shown in the ``homepage`` field). With no fallback,
the GitHub fetch 404s, the negative cache stores
``failure_kind=repo_not_found`` with a long TTL, and the package is
permanently unscoreable until the upstream fixes the typo.

This module pins the fallback contract:

1. ``CratesCollector`` carries a separate ``homepage_url`` whenever
   ``homepage`` looks like a code-host URL and differs from
   ``repository``.

2. ``services.scorer._collect_registry_data`` propagates that into
   ``RegistryData.repo_url_fallback``.

3. ``services.scorer.collect_package_data`` retries with the fallback
   when the primary URL 404s, and the warning trail records the swap.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

try:
    import respx
except ImportError:  # pragma: no cover
    # Per-test skip — keeps the layer-3 fallback tests (which use
    # unittest.mock.patch and don't touch the registry HTTP surface)
    # runnable in lean environments. Module-level skip would have
    # hidden them along with the respx-dependent layer-1/2 cases.
    class _RespxStub:
        @staticmethod
        def mock(fn):
            return pytest.mark.skip(reason="respx unavailable")(fn)

    respx = _RespxStub()

from ossuary.collectors.registries import CratesCollector


def _crate_payload(*, repository: str = "", homepage: str = "") -> dict:
    return {
        "crate": {
            "name": "agg",
            "newest_version": "0.1.0",
            "description": "asciinema gif generator",
            "repository": repository,
            "homepage": homepage,
            "recent_downloads": 10000,
        }
    }


@pytest.mark.asyncio
class TestCratesHomepageCarriedAsFallback:
    """Layer 1 — ``CratesCollector`` populates ``homepage_url`` only
    when it's distinct from ``repository`` and looks like a code host."""

    @respx.mock
    async def test_distinct_github_homepage_is_carried(self):
        respx.get("https://crates.io/api/v1/crates/agg").respond(
            200, json=_crate_payload(
                repository="https://github.com/savge13/agg",
                homepage="https://github.com/savage13/agg",
            ),
        )
        c = CratesCollector()
        try:
            data = await c.collect("agg")
        finally:
            await c.close()
        assert data.repository_url == "https://github.com/savge13/agg"
        assert data.homepage_url == "https://github.com/savage13/agg"

    @respx.mock
    async def test_homepage_equal_to_repository_is_skipped(self):
        same = "https://github.com/serde-rs/serde"
        respx.get("https://crates.io/api/v1/crates/serde").respond(
            200, json=_crate_payload(repository=same, homepage=same),
        )
        c = CratesCollector()
        try:
            data = await c.collect("serde")
        finally:
            await c.close()
        assert data.repository_url == same
        # No benefit to retrying the same URL.
        assert data.homepage_url == ""

    @respx.mock
    async def test_non_code_host_homepage_is_skipped(self):
        # The classic case: homepage is a docs page or company site,
        # not a repo. Carrying it as a "fallback" would make the
        # downstream retry attempt clone something nonsensical.
        respx.get("https://crates.io/api/v1/crates/foo").respond(
            200, json=_crate_payload(
                repository="https://github.com/foo/foo",
                homepage="https://docs.rs/foo",
            ),
        )
        c = CratesCollector()
        try:
            data = await c.collect("foo")
        finally:
            await c.close()
        assert data.homepage_url == ""

    @respx.mock
    async def test_gitlab_homepage_also_carried(self):
        respx.get("https://crates.io/api/v1/crates/bar").respond(
            200, json=_crate_payload(
                repository="https://github.com/bar/bar",
                homepage="https://gitlab.com/bar/bar",
            ),
        )
        c = CratesCollector()
        try:
            data = await c.collect("bar")
        finally:
            await c.close()
        # GitLab is also a recognised code host — fallback should carry.
        assert data.homepage_url == "https://gitlab.com/bar/bar"


@pytest.mark.asyncio
class TestRegistryDataPropagatesFallback:
    """Layer 2 — ``_collect_registry_data`` exposes the fallback as
    ``RegistryData.repo_url_fallback`` so downstream consumers (the
    git-clone retry path) can see it."""

    @respx.mock
    async def test_fallback_propagates_through_scorer_registry_data(self):
        respx.get("https://crates.io/api/v1/crates/agg").respond(
            200, json=_crate_payload(
                repository="https://github.com/savge13/agg",
                homepage="https://github.com/savage13/agg",
            ),
        )
        from ossuary.services.scorer import _collect_registry_data

        registry = await _collect_registry_data("agg", "cargo")
        assert registry.repo_url == "https://github.com/savge13/agg"
        assert registry.repo_url_fallback == "https://github.com/savage13/agg"

    @respx.mock
    async def test_caller_supplied_repo_url_still_gets_fallback(self):
        """Even when the caller passes an explicit ``repo_url``
        override, we still fetch the registry record and surface its
        fallback — the operator's explicit URL doesn't suppress the
        secondary candidate, so a typo in their override can still be
        recovered."""
        respx.get("https://crates.io/api/v1/crates/agg").respond(
            200, json=_crate_payload(
                repository="https://github.com/savge13/agg",
                homepage="https://github.com/savage13/agg",
            ),
        )
        from ossuary.services.scorer import _collect_registry_data

        registry = await _collect_registry_data(
            "agg", "cargo",
            repo_url="https://github.com/savge13/agg",
        )
        assert registry.repo_url == "https://github.com/savge13/agg"
        assert registry.repo_url_fallback == "https://github.com/savage13/agg"


class TestCollectPackageDataRetriesOnFallback:
    """Layer 3 — when the primary URL 404s, ``collect_package_data``
    retries with the fallback before giving up. Pins the regression on
    the agg-style typo case."""

    def test_404_on_primary_then_success_on_fallback(self):
        from ossuary.services.scorer import collect_package_data, RegistryData

        primary = "https://github.com/savge13/agg"
        fallback = "https://github.com/savage13/agg"

        # Build a registry result so we don't need to mock crates.io.
        prefetched = RegistryData(
            repo_url=primary,
            weekly_downloads=10000,
            fetch_errors=[],
            warnings=[],
            repo_url_fallback=fallback,
        )

        clone_attempts: list[str] = []

        class _FakeCommit:
            def __init__(self):
                self.author_email = "dev@example.com"
                self.author_name = "Dev"
                self.authored_date = __import__("datetime").datetime(2026, 1, 1)
                self.committed_date = self.authored_date
                self.message = "init"
                self.sha = "deadbeef"

        class _FakeMetrics:
            def __init__(self):
                self.top_contributor_email = None
                self.top_contributor_name = None
                self.maintainer_concentration = 0.0
                self.commits_last_year = 1
                self.unique_contributors = 1

        class _FakeGitCollector:
            def clone_or_update(self, url):
                clone_attempts.append(url)
                if url == primary:
                    raise RuntimeError("Cmd('git') failed: exit code(128)")
                return "/tmp/fake-repo"
            def extract_commits(self, _path):
                return [_FakeCommit()]
            def calculate_metrics(self, _commits, _date):
                return _FakeMetrics()

        async def _run():
            with patch(
                "ossuary.services.scorer.GitCollector", _FakeGitCollector,
            ), patch(
                # Skip the GitHub side — it's not what this test pins.
                "ossuary.services.scorer.GitHubCollector",
            ) as _gh_cls:
                _gh_cls.parse_repo_url = lambda url: ("savage13", "agg")

                async def _gh_collect(*_a, **_k):
                    from ossuary.collectors.github import GitHubData
                    return GitHubData(owner="savage13", repo="agg")
                _gh_cls.return_value.collect = _gh_collect

                async def _gh_get_repo_info(*_a, **_k):
                    return {"stargazers_count": 0}
                _gh_cls.return_value.get_repo_info = _gh_get_repo_info

                async def _gh_close():
                    return None
                _gh_cls.return_value.close = _gh_close

                return await collect_package_data(
                    "agg", "cargo", prefetched_registry=prefetched,
                )

        data, warnings = asyncio.run(_run())

        assert clone_attempts == [primary, fallback], (
            f"expected primary then fallback, got: {clone_attempts}"
        )
        assert data is not None, (
            f"fallback retry should produce CollectedData; warnings={warnings}"
        )
        # Warning trail records the swap so an operator can see it
        # happened (and so the dashboard can surface it).
        assert any("fallback" in w.lower() for w in warnings), warnings

    def test_404_on_primary_AND_fallback_returns_combined_error(self):
        from ossuary.services.scorer import collect_package_data, RegistryData

        primary = "https://github.com/x/missing"
        fallback = "https://github.com/y/also-missing"
        prefetched = RegistryData(
            repo_url=primary, weekly_downloads=100,
            fetch_errors=[], warnings=[],
            repo_url_fallback=fallback,
        )

        class _AlwaysMissing:
            def clone_or_update(self, _url):
                raise RuntimeError("Cmd('git') failed: exit code(128)")
            def extract_commits(self, _path):
                return []
            def calculate_metrics(self, *_a, **_k):
                return None

        async def _run():
            with patch(
                "ossuary.services.scorer.GitCollector", _AlwaysMissing,
            ):
                return await collect_package_data(
                    "missing", "cargo", prefetched_registry=prefetched,
                )

        data, warnings = asyncio.run(_run())
        assert data is None
        assert any(primary in w for w in warnings), warnings
        assert any(fallback in w for w in warnings), warnings
