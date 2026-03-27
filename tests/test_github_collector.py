"""Tests for GitHub collector."""

import asyncio
import base64
from unittest.mock import AsyncMock

from ossuary.collectors.github import GitHubCollector


class TestParseRepoUrl:
    """Tests for parse_repo_url static method."""

    def test_standard_https_url(self):
        owner, repo = GitHubCollector.parse_repo_url("https://github.com/pallets/flask")
        assert owner == "pallets"
        assert repo == "flask"

    def test_https_with_trailing_slash(self):
        owner, repo = GitHubCollector.parse_repo_url("https://github.com/pallets/flask/")
        assert owner == "pallets"
        assert repo == "flask"

    def test_https_with_git_suffix(self):
        owner, repo = GitHubCollector.parse_repo_url("https://github.com/pallets/flask.git")
        assert owner == "pallets"
        assert repo == "flask"

    def test_ssh_url(self):
        owner, repo = GitHubCollector.parse_repo_url("git@github.com:pallets/flask.git")
        assert owner == "pallets"
        assert repo == "flask"

    def test_ssh_url_without_git_suffix(self):
        owner, repo = GitHubCollector.parse_repo_url("git@github.com:pallets/flask")
        assert owner == "pallets"
        assert repo == "flask"

    def test_invalid_url_returns_none(self):
        owner, repo = GitHubCollector.parse_repo_url("https://example.com/foo")
        assert owner is None
        assert repo is None

    def test_empty_string(self):
        owner, repo = GitHubCollector.parse_repo_url("")
        assert owner is None
        assert repo is None

    def test_url_with_extra_path(self):
        owner, repo = GitHubCollector.parse_repo_url("https://github.com/pallets/flask/tree/main")
        assert owner == "pallets"
        assert repo == "flask"

    def test_org_repo_style(self):
        owner, repo = GitHubCollector.parse_repo_url("https://github.com/kubernetes/kubernetes")
        assert owner == "kubernetes"
        assert repo == "kubernetes"


class TestCiiBadgeDetection:
    """Tests for README-based CII badge detection."""

    def test_detects_cii_badge_from_readme(self):
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                readme = (
                    "[![CII Best Practices]"
                    "(https://bestpractices.coreinfrastructure.org/projects/1234/badge)]"
                    "(https://bestpractices.coreinfrastructure.org/projects/1234)"
                )
                collector._get = AsyncMock(
                    return_value={
                        "encoding": "base64",
                        "content": base64.b64encode(readme.encode()).decode(),
                    }
                )

                level = await collector.get_cii_badge_level("owner", "repo")

                assert level == "passing"
            finally:
                await collector.close()

        asyncio.run(run())

    def test_returns_none_when_badge_missing(self):
        async def run():
            collector = GitHubCollector(token="test-token")
            try:
                collector._get = AsyncMock(
                    return_value={
                        "encoding": "base64",
                        "content": base64.b64encode(b"# Example project").decode(),
                    }
                )

                level = await collector.get_cii_badge_level("owner", "repo")

                assert level == "none"
            finally:
                await collector.close()

        asyncio.run(run())
