"""Tests for the diff command."""

import json

import pytest
from typer.testing import CliRunner

from ossuary.cli import app


class TestDiffCommand:
    """Tests for ossuary diff command."""

    def setup_method(self):
        self.runner = CliRunner()

    @staticmethod
    def _write_report(tmp_path, filename, results, file_label=None):
        data = {"results": results}
        if file_label:
            data["file"] = file_label
        path = tmp_path / filename
        path.write_text(json.dumps(data))
        return str(path)

    @staticmethod
    def _pkg(name, score, risk_level="LOW", concentration=50, commits=10):
        return {
            "package": name,
            "score": score,
            "risk_level": risk_level,
            "concentration": concentration,
            "commits_last_year": commits,
        }

    def test_added_packages(self, tmp_path):
        """Packages in after but not before are shown as added."""
        before = self._write_report(tmp_path, "before.json", [
            self._pkg("lodash", 30),
        ])
        after = self._write_report(tmp_path, "after.json", [
            self._pkg("lodash", 30),
            self._pkg("left-pad", 80, "CRITICAL", 95, 0),
        ])
        result = self.runner.invoke(app, ["diff", before, after])
        assert result.exit_code == 0
        assert "Added" in result.output
        assert "left-pad" in result.output

    def test_removed_packages(self, tmp_path):
        """Packages in before but not after are shown as removed."""
        before = self._write_report(tmp_path, "before.json", [
            self._pkg("lodash", 30),
            self._pkg("moment", 40, "MODERATE"),
        ])
        after = self._write_report(tmp_path, "after.json", [
            self._pkg("lodash", 30),
        ])
        result = self.runner.invoke(app, ["diff", before, after])
        assert result.exit_code == 0
        assert "Removed" in result.output
        assert "moment" in result.output

    def test_changed_scores(self, tmp_path):
        """Score changes between runs are shown with delta."""
        before = self._write_report(tmp_path, "before.json", [
            self._pkg("express", 40, "MODERATE"),
        ])
        after = self._write_report(tmp_path, "after.json", [
            self._pkg("express", 70, "HIGH"),
        ])
        result = self.runner.invoke(app, ["diff", before, after])
        assert result.exit_code == 0
        assert "Changed" in result.output
        assert "express" in result.output
        assert "+30" in result.output

    def test_identical_reports(self, tmp_path):
        """Identical reports show no differences."""
        pkgs = [self._pkg("lodash", 30), self._pkg("express", 10)]
        before = self._write_report(tmp_path, "before.json", pkgs)
        after = self._write_report(tmp_path, "after.json", pkgs)
        result = self.runner.invoke(app, ["diff", before, after])
        assert result.exit_code == 0
        assert "No differences" in result.output
        assert "2 unchanged" in result.output

    def test_empty_reports(self, tmp_path):
        """Empty results arrays handled gracefully."""
        before = self._write_report(tmp_path, "before.json", [])
        after = self._write_report(tmp_path, "after.json", [])
        result = self.runner.invoke(app, ["diff", before, after])
        assert result.exit_code == 0
        assert "No differences" in result.output

    def test_json_output(self, tmp_path):
        """--json flag produces valid JSON with correct structure."""
        before = self._write_report(tmp_path, "before.json", [
            self._pkg("lodash", 30),
        ])
        after = self._write_report(tmp_path, "after.json", [
            self._pkg("lodash", 30),
            self._pkg("left-pad", 80, "CRITICAL"),
        ])
        result = self.runner.invoke(app, ["diff", before, after, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["added"]) == 1
        assert data["added"][0]["package"] == "left-pad"
        assert data["unchanged_count"] == 1
        assert data["removed"] == []
        assert data["changed"] == []

    def test_missing_file(self, tmp_path):
        """Nonexistent file gives exit code 1."""
        after = self._write_report(tmp_path, "after.json", [])
        result = self.runner.invoke(app, ["diff", "/nonexistent/path.json", after])
        assert result.exit_code == 1
        assert "File not found" in result.output

    def test_invalid_json(self, tmp_path):
        """Malformed JSON gives exit code 1."""
        bad = tmp_path / "bad.json"
        bad.write_text("not valid json {{{")
        after = self._write_report(tmp_path, "after.json", [])
        result = self.runner.invoke(app, ["diff", str(bad), after])
        assert result.exit_code == 1
        assert "Invalid JSON" in result.output
