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
