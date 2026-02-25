"""Tests for dependency tree fetching."""

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from ossuary.cli import _fetch_dep_tree


def _mock_response(data):
    """Create a mock HTTP response returning JSON data."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _mock_text_response(text):
    """Create a mock HTTP response returning plain text."""
    resp = MagicMock()
    resp.read.return_value = text.encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestFetchDepTree:
    """Tests for _fetch_dep_tree function."""

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_npm_parses_dependencies(self, mock_urlopen, mock_console):
        """npm registry JSON dependencies field is parsed into adj dict."""
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/express/" in url:
                return _mock_response({"dependencies": {"debug": "^4.3", "ms": "^2.1"}})
            return _mock_response({"dependencies": {}})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("express", "npm", max_depth=2, max_packages=50)

        assert "express" in adj
        assert set(adj["express"]) == {"debug", "ms"}
        assert adj["debug"] == []
        assert adj["ms"] == []

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_pypi_parses_requires_dist(self, mock_urlopen, mock_console):
        """PyPI requires_dist strings are parsed into dependency names."""
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/requests/" in url:
                return _mock_response({
                    "info": {"requires_dist": [
                        "urllib3 (>=1.25)",
                        "certifi (>=2017.4.17)",
                    ]}
                })
            return _mock_response({"info": {"requires_dist": None}})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("requests", "pypi", max_depth=2, max_packages=50)

        assert "requests" in adj
        assert set(adj["requests"]) == {"urllib3", "certifi"}

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_pypi_skips_extras(self, mock_urlopen, mock_console):
        """Dependencies with 'extra ==' condition are filtered out."""
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/requests/" in url:
                return _mock_response({
                    "info": {"requires_dist": [
                        "chardet (>=3.0.2)",
                        'PySocks (>=1.5.6) ; extra == "socks"',
                        'win-inet-pton ; (sys_platform == "win32") and extra == "security"',
                    ]}
                })
            return _mock_response({"info": {"requires_dist": None}})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("requests", "pypi", max_depth=2, max_packages=50)

        assert adj["requests"] == ["chardet"]

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_respects_max_depth(self, mock_urlopen, mock_console):
        """Packages beyond max_depth are not fetched."""
        # Chain: A → B → C → D
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/A/" in url:
                return _mock_response({"dependencies": {"B": "1.0"}})
            if "/B/" in url:
                return _mock_response({"dependencies": {"C": "1.0"}})
            if "/C/" in url:
                return _mock_response({"dependencies": {"D": "1.0"}})
            if "/D/" in url:
                return _mock_response({"dependencies": {}})
            return _mock_response({"dependencies": {}})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("A", "npm", max_depth=2, max_packages=50)

        assert "A" in adj
        assert "B" in adj
        assert "C" in adj
        assert "D" not in adj  # depth 3, beyond limit

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_respects_max_packages(self, mock_urlopen, mock_console):
        """Stops fetching when max_packages is reached."""
        # Linear chain: A→B→C→D→E→F→G→H, limit to 4
        # Each BFS round fetches 1 package, so limit kicks in at round boundary
        chain = list("ABCDEFGH")

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            for i, name in enumerate(chain[:-1]):
                if f"/{name}/" in url:
                    return _mock_response({"dependencies": {chain[i + 1]: "1.0"}})
            return _mock_response({"dependencies": {}})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("A", "npm", max_depth=20, max_packages=4)

        assert len(adj) == 4
        assert "A" in adj
        assert "D" in adj
        assert "E" not in adj  # 5th package, beyond limit

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_handles_network_error(self, mock_urlopen, mock_console):
        """Network errors for one package don't break the rest."""
        call_count = {"n": 0}

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/root/" in url:
                return _mock_response({"dependencies": {"good": "1.0", "bad": "1.0"}})
            if "/bad/" in url:
                raise urllib.error.URLError("connection refused")
            return _mock_response({"dependencies": {}})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("root", "npm", max_depth=2, max_packages=50)

        assert "root" in adj
        assert "good" in adj
        assert "bad" in adj
        assert adj["bad"] == []  # failed fetch returns empty deps

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_cargo_parses_dependencies(self, mock_urlopen, mock_console):
        """Cargo fetcher gets latest version then parses dependency list."""
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/crates/serde" in url and "/dependencies" not in url:
                return _mock_response({"crate": {"newest_version": "1.0.200"}})
            if "/crates/serde/1.0.200/dependencies" in url:
                return _mock_response({"dependencies": [
                    {"crate_id": "serde_derive", "kind": "normal", "optional": False},
                    {"crate_id": "serde_json", "kind": "dev", "optional": False},
                ]})
            return _mock_response({"crate": {"newest_version": "0.1.0"},
                                   "dependencies": []})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("serde", "cargo", max_depth=1, max_packages=50)

        assert "serde" in adj
        assert "serde_derive" in adj["serde"]
        assert "serde_json" not in adj["serde"]  # dev dep filtered out

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_cargo_filters_optional(self, mock_urlopen, mock_console):
        """Cargo fetcher skips optional dependencies."""
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/dependencies" not in url:
                return _mock_response({"crate": {"newest_version": "1.0.0"}})
            return _mock_response({"dependencies": [
                {"crate_id": "core", "kind": "normal", "optional": False},
                {"crate_id": "feature-dep", "kind": "normal", "optional": True},
            ]})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("mycrate", "cargo", max_depth=1, max_packages=50)

        assert adj["mycrate"] == ["core"]

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_rubygems_parses_runtime_deps(self, mock_urlopen, mock_console):
        """RubyGems fetcher returns runtime deps, not development deps."""
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/api/v1/gems/" in url:
                return _mock_response({"version": "7.1.0"})
            if "/api/v2/rubygems/" in url:
                return _mock_response({"dependencies": {
                    "runtime": [{"name": "activesupport"}, {"name": "actionpack"}],
                    "development": [{"name": "minitest"}],
                }})
            return _mock_response({"version": "0.1", "dependencies": {"runtime": [], "development": []}})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("rails", "rubygems", max_depth=1, max_packages=50)

        assert "rails" in adj
        assert set(adj["rails"]) == {"activesupport", "actionpack"}

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_go_parses_gomod(self, mock_urlopen, mock_console):
        """Go fetcher parses require block from go.mod text."""
        gomod = """module golang.org/x/text

go 1.21

require (
\tgolang.org/x/tools v0.16.0
\tgolang.org/x/net v0.19.0
)
"""
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/@latest" in url:
                return _mock_response({"Version": "v0.14.0"})
            if ".mod" in url:
                return _mock_text_response(gomod)
            return _mock_response({"Version": "v0.1.0"})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("golang.org/x/text", "go", max_depth=1, max_packages=50)

        assert "golang.org/x/text" in adj
        assert set(adj["golang.org/x/text"]) == {"golang.org/x/tools", "golang.org/x/net"}

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_go_parses_single_require(self, mock_urlopen, mock_console):
        """Go fetcher handles single-line require statement."""
        gomod = """module example.com/foo

go 1.21

require golang.org/x/sys v0.15.0
"""
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/@latest" in url:
                return _mock_response({"Version": "v1.0.0"})
            if ".mod" in url:
                return _mock_text_response(gomod)
            return _mock_response({"Version": "v0.1.0"})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("example.com/foo", "go", max_depth=1, max_packages=50)

        assert adj["example.com/foo"] == ["golang.org/x/sys"]

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_packagist_parses_require(self, mock_urlopen, mock_console):
        """Packagist fetcher extracts require keys with '/' (real packages only)."""
        def fake_urlopen(req, timeout=None):
            return _mock_response({"packages": {"vendor/pkg": [
                {"require": {
                    "php": ">=8.1",
                    "ext-mbstring": "*",
                    "vendor/dep-a": "^1.0",
                    "vendor/dep-b": "^2.0",
                }},
            ]}})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("vendor/pkg", "packagist", max_depth=1, max_packages=50)

        assert "vendor/pkg" in adj
        assert set(adj["vendor/pkg"]) == {"vendor/dep-a", "vendor/dep-b"}

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_nuget_parses_dependency_groups(self, mock_urlopen, mock_console):
        """NuGet fetcher extracts deps from registration index."""
        def fake_urlopen(req, timeout=None):
            return _mock_response({"items": [{"items": [
                {"catalogEntry": {
                    "version": "13.0.3",
                    "dependencyGroups": [
                        {"targetFramework": "net6.0", "dependencies": [
                            {"id": "Newtonsoft.Json.Bson"},
                        ]},
                        {"targetFramework": "netstandard2.0", "dependencies": [
                            {"id": "Newtonsoft.Json.Bson"},
                            {"id": "System.ComponentModel.Annotations"},
                        ]},
                    ],
                }},
            ]}]})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("Newtonsoft.Json", "nuget", max_depth=1, max_packages=50)

        assert "Newtonsoft.Json" in adj
        # Deduplicated across target frameworks
        assert "Newtonsoft.Json.Bson" in adj["Newtonsoft.Json"]
        assert "System.ComponentModel.Annotations" in adj["Newtonsoft.Json"]

    @patch("ossuary.cli.console")
    @patch("urllib.request.urlopen")
    def test_github_sbom_parses_packages(self, mock_urlopen, mock_console):
        """GitHub SBOM fetcher strips ecosystem prefixes and skips root."""
        def fake_urlopen(req, timeout=None):
            return _mock_response({"sbom": {"packages": [
                {"SPDXID": "SPDXRef-DOCUMENT", "name": "owner/repo"},
                {"SPDXID": "SPDXRef-1", "name": "pip:flask"},
                {"SPDXID": "SPDXRef-2", "name": "pip:click"},
                {"SPDXID": "SPDXRef-3", "name": "npm:lodash"},
            ]}})

        mock_urlopen.side_effect = fake_urlopen
        adj = _fetch_dep_tree("owner/repo", "github", max_depth=1, max_packages=50)

        assert "owner/repo" in adj
        deps = adj["owner/repo"]
        assert "flask" in deps
        assert "click" in deps
        assert "lodash" in deps
