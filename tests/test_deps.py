"""Tests for the deps command."""

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ossuary.cli import app


class TestDepsCommand:
    """Tests for ossuary deps command."""

    def setup_method(self):
        self.runner = CliRunner()

    @patch("ossuary.db.session.session_scope")
    @patch("ossuary.cli._fetch_dep_tree")
    def test_tree_output_contains_packages(self, mock_fetch, mock_scope):
        """Tree output displays package names from dependency graph."""
        mock_fetch.return_value = {
            "express": ["debug", "cookie"],
            "debug": ["ms"],
            "cookie": [],
            "ms": [],
        }
        # Mock DB: return no scores (all unscored)
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)

        result = self.runner.invoke(app, ["deps", "express"])
        assert result.exit_code == 0
        assert "express" in result.output
        assert "debug" in result.output
        assert "cookie" in result.output
        assert "ms" in result.output
        assert "4 packages" in result.output

    @patch("ossuary.db.session.session_scope")
    @patch("ossuary.cli._fetch_dep_tree")
    def test_json_output_structure(self, mock_fetch, mock_scope):
        """--json produces valid JSON with correct nested structure."""
        mock_fetch.return_value = {
            "root": ["child-a", "child-b"],
            "child-a": ["shared"],
            "child-b": ["shared"],
            "shared": [],
        }
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)

        result = self.runner.invoke(app, ["deps", "root", "--json"])
        assert result.exit_code == 0

        # Find the JSON part (skip the "Fetching..." line)
        lines = result.output.strip().split("\n")
        json_start = next(i for i, l in enumerate(lines) if l.strip().startswith("{"))
        data = json.loads("\n".join(lines[json_start:]))

        assert data["root"] == "root"
        assert data["ecosystem"] == "npm"
        assert data["packages"] == 4
        assert data["tree"]["name"] == "root"
        assert len(data["tree"]["dependencies"]) == 2
        child_names = {c["name"] for c in data["tree"]["dependencies"]}
        assert child_names == {"child-a", "child-b"}

    @patch("ossuary.cli._fetch_dep_tree")
    def test_unsupported_ecosystem(self, mock_fetch):
        """Unsupported ecosystem gives exit code 1."""
        result = self.runner.invoke(app, ["deps", "foo", "-e", "maven"])
        assert result.exit_code == 1
        assert "Supported ecosystems" in result.output
        mock_fetch.assert_not_called()

    @patch("ossuary.db.session.session_scope")
    @patch("ossuary.cli._fetch_dep_tree")
    def test_empty_tree(self, mock_fetch, mock_scope):
        """Package with no deps shows just root."""
        mock_fetch.return_value = {"solo": []}
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)

        result = self.runner.invoke(app, ["deps", "solo"])
        assert result.exit_code == 0
        assert "solo" in result.output
        assert "1 packages" in result.output

    @patch("ossuary.cli._fetch_dep_tree")
    def test_fetch_failure(self, mock_fetch):
        """Empty result from _fetch_dep_tree gives exit code 1."""
        mock_fetch.return_value = {}
        result = self.runner.invoke(app, ["deps", "nonexistent"])
        assert result.exit_code == 1
        assert "Could not fetch" in result.output
