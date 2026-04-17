"""Data-completeness contract for the scoring pipeline.

Background: a stability incident on 2026-04-17 surfaced that
``pypistats.org`` rate-limits (HTTP 429) intermittently and the PyPI
collector silently swallowed the failure as ``weekly_downloads = 0``.
The score was then computed from partial data and produced a different
number than the previous run, with no signal to the user that anything
had gone wrong. This test module pins down the contract that replaced
that behaviour:

1. Failed fetches (any non-2xx, transport exception, malformed payload)
   become *known* failures recorded in
   ``CollectedData.fetch_errors``.
2. The scoring engine refuses to produce a number when
   ``fetch_errors`` is non-empty — it returns
   ``RiskLevel.INSUFFICIENT_DATA`` with the failure list copied to
   ``incomplete_reasons``.
3. The PyPI collector retries 429 with the ``Retry-After`` header
   honoured (capped) and 5xx with a short backoff before giving up.
4. ``RiskBreakdown.to_dict()`` exposes the new state so it round-trips
   through the cache.

Tests use ``respx`` to stub the HTTP surface; no live network calls.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import pytest

respx = pytest.importorskip("respx")

from ossuary.collectors.pypi import PyPICollector
from ossuary.scoring.factors import RiskBreakdown, RiskLevel
from ossuary.services.scorer import CollectedData, calculate_score_for_date


# --- helpers --------------------------------------------------------------

def _empty_collected_data(*, fetch_errors=None) -> CollectedData:
    """Minimal CollectedData stub for engine short-circuit tests."""
    from ossuary.collectors.github import GitHubData
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
    )


# --- engine short-circuit -------------------------------------------------

class TestEngineShortCircuit:
    def test_fetch_errors_present_yields_insufficient_data(self):
        data = _empty_collected_data(
            fetch_errors=["pypi.weekly_downloads: HTTP 429 from pypistats.org"]
        )
        breakdown = calculate_score_for_date("pkg", "pypi", data, datetime.now())
        assert breakdown.risk_level == RiskLevel.INSUFFICIENT_DATA
        assert breakdown.final_score is None
        assert breakdown.incomplete_reasons == [
            "pypi.weekly_downloads: HTTP 429 from pypistats.org"
        ]
        # Recommendations point at the recovery path.
        assert any("rescore-invalid" in r for r in breakdown.recommendations)

    def test_no_fetch_errors_proceeds_with_scoring(self):
        # Empty fetch_errors → engine attempts to score. With no commits this
        # particular call won't produce an interesting number, but it should
        # NOT short-circuit to INSUFFICIENT_DATA.
        data = _empty_collected_data(fetch_errors=[])
        breakdown = calculate_score_for_date("pkg", "pypi", data, datetime.now())
        assert breakdown.risk_level != RiskLevel.INSUFFICIENT_DATA

    def test_to_dict_round_trips_incomplete_reasons(self):
        data = _empty_collected_data(fetch_errors=["x: y"])
        breakdown = calculate_score_for_date("pkg", "pypi", data, datetime.now())
        d = breakdown.to_dict()
        assert d["score"]["final"] is None
        assert d["score"]["risk_level"] == "INSUFFICIENT_DATA"
        assert d["incomplete_reasons"] == ["x: y"]


# --- pypi collector contract ---------------------------------------------

@pytest.mark.asyncio
class TestPyPIWeeklyDownloads:
    @respx.mock
    async def test_success_returns_count_no_error(self):
        respx.get("https://pypistats.org/api/packages/lodash/recent").respond(
            200, json={"data": {"last_month": 4_000_000}, "package": "lodash"}
        )
        c = PyPICollector()
        try:
            count, err = await c.get_weekly_downloads("lodash")
        finally:
            await c.close()
        assert err is None
        assert count == 1_000_000  # monthly // 4

    @respx.mock
    async def test_429_retries_then_gives_up_with_error(self, monkeypatch):
        # Make the backoff a no-op so the test runs in milliseconds.
        # Capture the real asyncio.sleep, then replace asyncio.sleep with a
        # zero-delay shim. Avoids infinite recursion.
        _real_sleep = asyncio.sleep
        async def _no_sleep(*_args, **_kwargs):
            await _real_sleep(0)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        respx.get("https://pypistats.org/api/packages/foo/recent").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "1"})
        )
        c = PyPICollector()
        try:
            count, err = await c.get_weekly_downloads("foo")
        finally:
            await c.close()
        assert count is None
        assert err is not None
        assert "429" in err
        assert "rate limited" in err.lower()

    @respx.mock
    async def test_429_then_200_succeeds(self, monkeypatch):
        # Capture the real asyncio.sleep, then replace asyncio.sleep with a
        # zero-delay shim. Avoids infinite recursion.
        _real_sleep = asyncio.sleep
        async def _no_sleep(*_args, **_kwargs):
            await _real_sleep(0)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        route = respx.get("https://pypistats.org/api/packages/bar/recent")
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(200, json={"data": {"last_month": 800}}),
        ]
        c = PyPICollector()
        try:
            count, err = await c.get_weekly_downloads("bar")
        finally:
            await c.close()
        assert err is None
        assert count == 200  # 800 // 4

    @respx.mock
    async def test_500_retries_with_short_backoff(self, monkeypatch):
        # Capture the real asyncio.sleep, then replace asyncio.sleep with a
        # zero-delay shim. Avoids infinite recursion.
        _real_sleep = asyncio.sleep
        async def _no_sleep(*_args, **_kwargs):
            await _real_sleep(0)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        respx.get("https://pypistats.org/api/packages/baz/recent").mock(
            return_value=httpx.Response(503)
        )
        c = PyPICollector()
        try:
            count, err = await c.get_weekly_downloads("baz")
        finally:
            await c.close()
        assert count is None
        assert "503" in err

    @respx.mock
    async def test_404_no_retry(self, monkeypatch):
        # 4xx other than 429 = permanent error, no retry.
        sleep_calls = []
        original_sleep = asyncio.sleep
        async def fake_sleep(s):
            sleep_calls.append(s)
            await original_sleep(0)
        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        respx.get("https://pypistats.org/api/packages/nonexistent/recent").mock(
            return_value=httpx.Response(404)
        )
        c = PyPICollector()
        try:
            count, err = await c.get_weekly_downloads("nonexistent")
        finally:
            await c.close()
        assert count is None
        assert "404" in err
        assert sleep_calls == []  # no backoff was attempted

    @respx.mock
    async def test_malformed_json_treated_as_failure(self):
        respx.get("https://pypistats.org/api/packages/q/recent").respond(
            200, content=b"not json {",
            headers={"content-type": "application/json"},
        )
        c = PyPICollector()
        try:
            count, err = await c.get_weekly_downloads("q")
        finally:
            await c.close()
        assert count is None
        assert "malformed JSON" in err

    @respx.mock
    async def test_unexpected_schema_treated_as_failure(self):
        respx.get("https://pypistats.org/api/packages/r/recent").respond(
            200, json={"unexpected": "shape"}
        )
        c = PyPICollector()
        try:
            count, err = await c.get_weekly_downloads("r")
        finally:
            await c.close()
        assert count is None
        assert "schema" in err

    @respx.mock
    async def test_zero_downloads_is_real_zero_not_error(self):
        # A package with genuinely zero recent downloads is a valid signal,
        # not a fetch failure.
        respx.get("https://pypistats.org/api/packages/dead/recent").respond(
            200, json={"data": {"last_month": 0}}
        )
        c = PyPICollector()
        try:
            count, err = await c.get_weekly_downloads("dead")
        finally:
            await c.close()
        assert err is None
        assert count == 0


# --- collector.collect propagation ---------------------------------------

@pytest.mark.asyncio
class TestPyPICollectFetchErrors:
    @respx.mock
    async def test_collect_propagates_downloads_error(self, monkeypatch):
        # Capture the real asyncio.sleep, then replace asyncio.sleep with a
        # zero-delay shim. Avoids infinite recursion.
        _real_sleep = asyncio.sleep
        async def _no_sleep(*_args, **_kwargs):
            await _real_sleep(0)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        respx.get("https://pypi.org/pypi/foo/json").respond(
            200, json={"info": {"name": "foo", "version": "1.0",
                                  "summary": "", "home_page": "",
                                  "project_urls": {}}}
        )
        respx.get("https://pypistats.org/api/packages/foo/recent").mock(
            return_value=httpx.Response(429)
        )
        c = PyPICollector()
        try:
            data = await c.collect("foo")
        finally:
            await c.close()
        assert data.weekly_downloads is None
        assert any("weekly_downloads" in e for e in data.fetch_errors)

    @respx.mock
    async def test_collect_clean_run_has_no_fetch_errors(self):
        respx.get("https://pypi.org/pypi/foo/json").respond(
            200, json={"info": {"name": "foo", "version": "1.0",
                                  "summary": "", "home_page": "",
                                  "project_urls": {}}}
        )
        respx.get("https://pypistats.org/api/packages/foo/recent").respond(
            200, json={"data": {"last_month": 4_000_000}}
        )
        c = PyPICollector()
        try:
            data = await c.collect("foo")
        finally:
            await c.close()
        assert data.fetch_errors == []
        assert data.weekly_downloads == 1_000_000
