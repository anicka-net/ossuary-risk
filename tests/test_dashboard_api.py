"""Tests for ossuary dashboard and api CLI commands."""

import asyncio
import sys
from unittest.mock import patch

from typer.testing import CliRunner

from ossuary.api.main import _get_score, get_score
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
