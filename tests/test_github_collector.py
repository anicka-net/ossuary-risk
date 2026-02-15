"""Tests for GitHub collector — URL parsing."""

import pytest

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
