"""Tests for ossuary dashboard and api CLI commands."""

import sys
from unittest.mock import patch, call

from typer.testing import CliRunner

from ossuary.cli import app


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
