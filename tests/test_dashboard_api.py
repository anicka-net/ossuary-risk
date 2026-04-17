"""Tests for ossuary dashboard and api CLI commands."""

import asyncio
import sys
from unittest.mock import patch

from typer.testing import CliRunner

from ossuary.api.main import CheckResponse, ScoreResponse, _get_score, check_package, get_score
from ossuary.cli import app
from ossuary.scoring.factors import RiskBreakdown, RiskLevel
from ossuary.services.scorer import ScoringResult


class TestDashboardCommand:
    """Tests for ossuary dashboard command."""

    def setup_method(self):
        self.runner = CliRunner()

    @patch("subprocess.run")
    def test_dashboard_default_port(self, mock_run):
        """Dashboard launches streamlit on default port 8501."""
        mock_run.return_value = None
        result = self.runner.invoke(app, ["dashboard"])
        assert result.exit_code == 0
        args = mock_run.call_args[0][0]
        assert args[0] == sys.executable
        assert args[1:3] == ["-m", "streamlit"]
        assert args[3] == "run"
        assert "app.py" in args[4]
        assert "--server.port" in args
        assert "8501" in args

    @patch("subprocess.run")
    def test_dashboard_custom_port(self, mock_run):
        """Dashboard respects --port option."""
        mock_run.return_value = None
        result = self.runner.invoke(app, ["dashboard", "--port", "9000"])
        assert result.exit_code == 0
        args = mock_run.call_args[0][0]
        assert "9000" in args

    @patch("subprocess.run")
    def test_dashboard_headless(self, mock_run):
        """Dashboard runs in headless mode."""
        mock_run.return_value = None
        result = self.runner.invoke(app, ["dashboard"])
        args = mock_run.call_args[0][0]
        assert "--server.headless" in args
        assert "true" in args

    @patch("subprocess.run")
    def test_dashboard_points_to_packaged_app(self, mock_run):
        """Dashboard runs the packaged app.py, not the repo root one."""
        mock_run.return_value = None
        result = self.runner.invoke(app, ["dashboard"])
        args = mock_run.call_args[0][0]
        app_path = args[4]
        assert "ossuary/dashboard/app.py" in app_path


class TestApiCommand:
    """Tests for ossuary api command."""

    def setup_method(self):
        self.runner = CliRunner()

    @patch("subprocess.run")
    def test_api_default_port(self, mock_run):
        """API launches uvicorn on default port 8100."""
        mock_run.return_value = None
        result = self.runner.invoke(app, ["api"])
        assert result.exit_code == 0
        args = mock_run.call_args[0][0]
        assert args[0] == sys.executable
        assert args[1:3] == ["-m", "uvicorn"]
        assert "ossuary.api.main:app" in args
        assert "--port" in args
        assert "8100" in args

    @patch("subprocess.run")
    def test_api_custom_port(self, mock_run):
        """API respects --port option."""
        mock_run.return_value = None
        result = self.runner.invoke(app, ["api", "--port", "9100"])
        assert result.exit_code == 0
        args = mock_run.call_args[0][0]
        assert "9100" in args

    @patch("subprocess.run")
    def test_api_default_host(self, mock_run):
        """API binds to 0.0.0.0 by default."""
        mock_run.return_value = None
        result = self.runner.invoke(app, ["api"])
        args = mock_run.call_args[0][0]
        assert "--host" in args
        assert "0.0.0.0" in args

    @patch("subprocess.run")
    def test_api_custom_host(self, mock_run):
        """API respects --host option."""
        mock_run.return_value = None
        result = self.runner.invoke(app, ["api", "--host", "127.0.0.1"])
        assert result.exit_code == 0
        args = mock_run.call_args[0][0]
        assert "127.0.0.1" in args


class TestDashboardImports:
    """Tests that dashboard package imports work correctly."""

    def test_utils_import(self):
        """Dashboard utils module imports cleanly."""
        from ossuary.dashboard.utils import VERSION, COLORS, apply_style, risk_color
        assert VERSION
        assert "critical" in COLORS
        assert callable(apply_style)
        assert callable(risk_color)

    def test_risk_color_levels(self):
        """risk_color returns correct colors for all levels."""
        from ossuary.dashboard.utils import risk_color, COLORS
        assert risk_color("critical") == COLORS["critical"]
        assert risk_color("HIGH") == COLORS["high"]
        assert risk_color("VERY_LOW") == COLORS["very_low"]
        assert risk_color("unknown") == COLORS["text_muted"]

    def test_risk_badge(self):
        """risk_badge produces HTML with score and level."""
        from ossuary.dashboard.utils import risk_badge
        html = risk_badge("critical", 85)
        assert "85" in html
        assert "critical" in html
        assert "<span" in html


class TestRiskLevelStrHelper:
    """Defensive helper: dashboard pages must not crash when a stale
    ``RiskLevel`` import (no INSUFFICIENT_DATA member) is reused across
    Streamlit reruns, or when the breakdown's ``risk_level`` arrives as
    a plain string from the cache JSON.
    """

    def test_returns_value_for_current_enum(self):
        from ossuary.dashboard.utils import risk_level_str
        from ossuary.scoring.factors import RiskLevel
        assert risk_level_str(RiskLevel.INSUFFICIENT_DATA) == "INSUFFICIENT_DATA"
        assert risk_level_str(RiskLevel.HIGH) == "HIGH"

    def test_returns_string_unchanged(self):
        """Cache round-trips can deliver risk_level as a plain string."""
        from ossuary.dashboard.utils import risk_level_str
        assert risk_level_str("INSUFFICIENT_DATA") == "INSUFFICIENT_DATA"
        assert risk_level_str("HIGH") == "HIGH"

    def test_does_not_touch_enum_members(self):
        """A stale ``RiskLevel`` class without INSUFFICIENT_DATA must not
        crash the comparison. We simulate the stale enum and verify the
        helper only relies on the per-instance ``.value`` attribute."""
        from enum import Enum
        from ossuary.dashboard.utils import risk_level_str

        class StaleRiskLevel(str, Enum):
            CRITICAL = "CRITICAL"
            HIGH = "HIGH"
            MODERATE = "MODERATE"
            LOW = "LOW"
            VERY_LOW = "VERY_LOW"

        # The class lacks INSUFFICIENT_DATA, but the helper never looks
        # the member up by name — it asks the *instance* for its value.
        assert risk_level_str(StaleRiskLevel.HIGH) == "HIGH"
        assert not hasattr(StaleRiskLevel, "INSUFFICIENT_DATA")

    def test_short_circuit_pattern_works_with_stale_enum(self):
        """End-to-end: the page-level pattern
        `risk_level_str(b.risk_level) == "INSUFFICIENT_DATA"` must work
        whether ``b.risk_level`` is the current enum, the stale enum,
        or a plain string."""
        from enum import Enum
        from ossuary.dashboard.utils import risk_level_str
        from ossuary.scoring.factors import RiskLevel

        class StaleRiskLevel(str, Enum):
            HIGH = "HIGH"

        # Case 1: current enum, value is INSUFFICIENT_DATA → match
        assert risk_level_str(RiskLevel.INSUFFICIENT_DATA) == "INSUFFICIENT_DATA"
        # Case 2: stale enum, value is HIGH → no match (no crash either)
        assert risk_level_str(StaleRiskLevel.HIGH) != "INSUFFICIENT_DATA"
        # Case 3: cache JSON delivers a plain string
        assert risk_level_str("INSUFFICIENT_DATA") == "INSUFFICIENT_DATA"
        # Case 4: cache JSON delivers a non-INSUFFICIENT_DATA string
        assert risk_level_str("LOW") != "INSUFFICIENT_DATA"


class TestApiCacheAge:
    """Tests for API cache-age behavior."""

    @patch("ossuary.api.main.score_package")
    def test_get_score_passes_max_age_as_freshness(self, mock_score_package):
        """API max_age should be forwarded as an actual freshness window."""
        mock_score_package.return_value = ScoringResult(
            success=True,
            breakdown=RiskBreakdown(
                package_name="flask",
                ecosystem="pypi",
                final_score=10,
                risk_level=RiskLevel.VERY_LOW,
            ),
        )

        asyncio.run(_get_score("flask", "pypi", None, 3))

        kwargs = mock_score_package.await_args.kwargs
        assert kwargs["use_cache"] is True
        assert kwargs["freshness_days"] == 3

    @patch("ossuary.api.main.score_package")
    def test_get_score_zero_max_age_forces_rescore(self, mock_score_package):
        """max_age=0 should disable cache reuse."""
        mock_score_package.return_value = ScoringResult(
            success=True,
            breakdown=RiskBreakdown(
                package_name="flask",
                ecosystem="pypi",
                final_score=10,
                risk_level=RiskLevel.VERY_LOW,
            ),
        )

        asyncio.run(_get_score("flask", "pypi", None, 0))

        kwargs = mock_score_package.await_args.kwargs
        assert kwargs["use_cache"] is False
        assert kwargs["freshness_days"] is None


class TestApiResponses:
    """Tests for external API response shapes."""

    @patch("ossuary.api.main.score_package")
    def test_score_endpoint_returns_full_breakdown(self, mock_score_package):
        """The score endpoint should expose the complete serialized breakdown."""
        mock_score_package.return_value = ScoringResult(
            success=True,
            breakdown=RiskBreakdown(
                package_name="flask",
                ecosystem="pypi",
                repo_url="https://github.com/pallets/flask",
                maintainer_concentration=41,
                bus_factor=2,
                elephant_factor=1,
                inactive_contributor_ratio=0.25,
                commits_last_year=12,
                unique_contributors=3,
                weekly_downloads=1234,
                final_score=10,
                risk_level=RiskLevel.VERY_LOW,
                explanation="ok",
            ),
        )

        response = asyncio.run(get_score("pypi", "flask", None, 7))
        body = response.model_dump()
        assert body["breakdown"]["package"]["name"] == "flask"
        assert body["breakdown"]["metrics"]["weekly_downloads"] == 1234
        assert body["breakdown"]["chaoss_signals"]["bus_factor"] == 2
        assert body["breakdown"]["score"]["final"] == 10
        assert body["incomplete_reasons"] == []


class TestApiInsufficientData:
    """The API contract must accept INSUFFICIENT_DATA cleanly.

    Background: the data-completeness contract introduced on 2026-04-17
    can return ``final_score=None``. Both endpoints declared ``score: int``
    so the response model rejected the value with a Pydantic error,
    turning a documented scoring state into a 500-style failure.
    """

    @patch("ossuary.api.main.score_package")
    def test_score_endpoint_returns_null_score_for_insufficient_data(self, mock_score_package):
        mock_score_package.return_value = ScoringResult(
            success=True,
            breakdown=RiskBreakdown(
                package_name="pyyaml",
                ecosystem="pypi",
                final_score=None,
                risk_level=RiskLevel.INSUFFICIENT_DATA,
                incomplete_reasons=["pypi.weekly_downloads: HTTP 429 from pypistats.org"],
                recommendations=["Run 'ossuary rescore-invalid' to retry."],
            ),
        )

        response = asyncio.run(get_score("pypi", "pyyaml", None, 7))
        body = response.model_dump()
        assert body["score"] is None
        assert body["risk_level"] == "INSUFFICIENT_DATA"
        assert body["semaphore"] == "⚪"
        assert body["incomplete_reasons"] == [
            "pypi.weekly_downloads: HTTP 429 from pypistats.org"
        ]
        assert body["breakdown"]["score"]["final"] is None

    @patch("ossuary.api.main.score_package")
    def test_check_endpoint_returns_null_score_for_insufficient_data(self, mock_score_package):
        mock_score_package.return_value = ScoringResult(
            success=True,
            breakdown=RiskBreakdown(
                package_name="pyyaml",
                ecosystem="pypi",
                final_score=None,
                risk_level=RiskLevel.INSUFFICIENT_DATA,
                incomplete_reasons=["pypi.weekly_downloads: HTTP 429 from pypistats.org"],
            ),
        )

        response = asyncio.run(check_package("pypi", "pyyaml", None, 7))
        body = response.model_dump()
        assert body["score"] is None
        assert body["risk_level"] == "INSUFFICIENT_DATA"
        assert body["semaphore"] == "⚪"
        assert body["incomplete_reasons"] == [
            "pypi.weekly_downloads: HTTP 429 from pypistats.org"
        ]

    def test_response_models_accept_null_score(self):
        """Direct schema check — guards against a future regression that
        re-tightens ``score`` to a non-Optional int."""
        assert (
            CheckResponse(
                package="pyyaml",
                ecosystem="pypi",
                score=None,
                risk_level="INSUFFICIENT_DATA",
                semaphore="⚪",
            ).score
            is None
        )
        assert (
            ScoreResponse(
                package="pyyaml",
                ecosystem="pypi",
                score=None,
                risk_level="INSUFFICIENT_DATA",
                semaphore="⚪",
                explanation="",
                breakdown={},
                recommendations=[],
            ).score
            is None
        )
