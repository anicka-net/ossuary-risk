"""Tests for PyPI collector — URL extraction and cleaning."""

import pytest

from ossuary.collectors.pypi import PyPICollector


class TestCleanRepoUrl:
    """Tests for _clean_repo_url."""

    def setup_method(self):
        self.collector = PyPICollector()

    def test_strips_issues_path(self):
        url = "https://github.com/pallets/flask/issues"
        assert self.collector._clean_repo_url(url) == "https://github.com/pallets/flask"

    def test_strips_tree_path(self):
        url = "https://github.com/psf/requests/tree/main/src"
        assert self.collector._clean_repo_url(url) == "https://github.com/psf/requests"

    def test_strips_blob_path(self):
        url = "https://github.com/owner/repo/blob/main/README.md"
        assert self.collector._clean_repo_url(url) == "https://github.com/owner/repo"

    def test_strips_wiki_path(self):
        url = "https://github.com/owner/repo/wiki"
        assert self.collector._clean_repo_url(url) == "https://github.com/owner/repo"

    def test_strips_pulls_path(self):
        url = "https://github.com/owner/repo/pulls"
        assert self.collector._clean_repo_url(url) == "https://github.com/owner/repo"

    def test_strips_query_string(self):
        url = "https://github.com/owner/repo?tab=readme"
        assert self.collector._clean_repo_url(url) == "https://github.com/owner/repo"

    def test_strips_fragment(self):
        url = "https://github.com/owner/repo#readme"
        assert self.collector._clean_repo_url(url) == "https://github.com/owner/repo"

    def test_strips_trailing_slash(self):
        url = "https://github.com/owner/repo/"
        assert self.collector._clean_repo_url(url) == "https://github.com/owner/repo"

    def test_preserves_clean_url(self):
        url = "https://github.com/owner/repo"
        assert self.collector._clean_repo_url(url) == "https://github.com/owner/repo"

    def test_strips_releases_path(self):
        url = "https://github.com/owner/repo/releases"
        assert self.collector._clean_repo_url(url) == "https://github.com/owner/repo"

    def test_strips_actions_path(self):
        url = "https://github.com/owner/repo/actions"
        assert self.collector._clean_repo_url(url) == "https://github.com/owner/repo"


class TestExtractRepoUrl:
    """Tests for _extract_repo_url."""

    def setup_method(self):
        self.collector = PyPICollector()

    def test_explicit_repository_key(self):
        info = {"project_urls": {"Repository": "https://github.com/owner/repo"}}
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    def test_lowercase_repository_key(self):
        info = {"project_urls": {"repository": "https://github.com/owner/repo"}}
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    def test_source_code_with_space(self):
        """gunicorn uses 'Source code' as key."""
        info = {"project_urls": {"Source code": "https://github.com/benoitc/gunicorn"}}
        assert self.collector._extract_repo_url(info) == "https://github.com/benoitc/gunicorn"

    def test_github_key(self):
        info = {"project_urls": {"GitHub": "https://github.com/owner/repo"}}
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    def test_code_key(self):
        info = {"project_urls": {"Code": "https://github.com/owner/repo"}}
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    def test_homepage_github_fallback(self):
        info = {"project_urls": {"Homepage": "https://github.com/owner/repo"}}
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    def test_homepage_non_github_ignored(self):
        """Non-GitHub homepage should not be treated as repo URL."""
        info = {"project_urls": {"Homepage": "https://docs.example.com"}}
        assert self.collector._extract_repo_url(info) == ""

    def test_scan_all_values_fallback(self):
        info = {"project_urls": {"Bug Tracker": "https://github.com/owner/repo/issues"}}
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    def test_legacy_home_page_field(self):
        info = {"home_page": "https://github.com/owner/repo", "project_urls": {}}
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    def test_empty_project_urls(self):
        info = {"project_urls": {}}
        assert self.collector._extract_repo_url(info) == ""

    def test_none_project_urls(self):
        info = {"project_urls": None}
        assert self.collector._extract_repo_url(info) == ""

    def test_missing_project_urls(self):
        info = {}
        assert self.collector._extract_repo_url(info) == ""

    def test_gitlab_url_supported(self):
        info = {"project_urls": {"Source": "https://gitlab.com/owner/repo"}}
        assert self.collector._extract_repo_url(info) == "https://gitlab.com/owner/repo"

    def test_priority_order_explicit_over_homepage(self):
        """Explicit repo key should win over homepage."""
        info = {
            "project_urls": {
                "Homepage": "https://docs.example.com",
                "Repository": "https://github.com/owner/repo",
            }
        }
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    def test_cleans_url_from_project_urls(self):
        """URLs extracted from project_urls should be cleaned."""
        info = {"project_urls": {"Source": "https://github.com/owner/repo/tree/main"}}
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    # Real-world package cases
    def test_pandas_style(self):
        """pandas uses lowercase 'repository'."""
        info = {"project_urls": {
            "Bug Tracker": "https://github.com/pandas-dev/pandas/issues",
            "Documentation": "https://pandas.pydata.org/pandas-docs/stable",
            "repository": "https://github.com/pandas-dev/pandas",
        }}
        assert self.collector._extract_repo_url(info) == "https://github.com/pandas-dev/pandas"

    def test_tqdm_style(self):
        """tqdm uses 'source' (lowercase)."""
        info = {"project_urls": {
            "Changelog": "https://tqdm.github.io/releases",
            "source": "https://github.com/tqdm/tqdm",
            "Wiki": "https://github.com/tqdm/tqdm/wiki",
        }}
        assert self.collector._extract_repo_url(info) == "https://github.com/tqdm/tqdm"

    def test_orjson_style(self):
        """orjson uses 'Repository' in mixed-case project_urls."""
        info = {"project_urls": {
            "Changelog": "https://github.com/ijl/orjson/blob/master/CHANGELOG.md",
            "Homepage": "https://github.com/ijl/orjson",
            "Repository": "https://github.com/ijl/orjson",
        }}
        assert self.collector._extract_repo_url(info) == "https://github.com/ijl/orjson"

    # Priority 5: long_description fallback
    def test_long_description_github_url(self):
        """Falls back to scanning description (long_description) for GitHub URL."""
        info = {
            "project_urls": {"Homepage": "https://example.com"},
            "description": "Some package.\n\nSource: https://github.com/owner/repo\n",
        }
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    def test_long_description_cleans_url(self):
        """URLs from description are cleaned (subpaths stripped)."""
        info = {
            "project_urls": {},
            "description": "See https://github.com/owner/repo/tree/main/docs for docs.",
        }
        assert self.collector._extract_repo_url(info) == "https://github.com/owner/repo"

    def test_long_description_not_used_when_project_urls_has_repo(self):
        """Priority 1 wins over description fallback."""
        info = {
            "project_urls": {"Repository": "https://github.com/correct/repo"},
            "description": "Old link: https://github.com/wrong/repo",
        }
        assert self.collector._extract_repo_url(info) == "https://github.com/correct/repo"

    def test_long_description_gitlab(self):
        """GitLab URLs in description also work."""
        info = {
            "project_urls": {},
            "description": "Hosted at https://gitlab.com/owner/repo.",
        }
        assert self.collector._extract_repo_url(info) == "https://gitlab.com/owner/repo"

    def test_long_description_no_url(self):
        """No GitHub URL in description still returns empty."""
        info = {
            "project_urls": {},
            "description": "A nice package with no repo link.",
        }
        assert self.collector._extract_repo_url(info) == ""

    def test_long_description_none(self):
        """None description doesn't crash."""
        info = {"project_urls": {}, "description": None}
        assert self.collector._extract_repo_url(info) == ""

    def test_long_description_markdown_link(self):
        """Extracts URL from markdown link syntax."""
        info = {
            "project_urls": {},
            "description": "See [the repo](https://github.com/owner/repo) for details.",
        }
        assert self.collector._extract_repo_url(info, "repo") == "https://github.com/owner/repo"

    def test_long_description_rejects_unrelated_url(self):
        """URLs in description that don't match the package name are skipped."""
        info = {
            "project_urls": {},
            "description": "Uses https://github.com/rtfd/recommonmark for parsing.",
        }
        assert self.collector._extract_repo_url(info, "docutils") == ""

    def test_long_description_name_match_normalized(self):
        """Name matching normalizes hyphens and underscores."""
        info = {
            "project_urls": {},
            "description": "Source: https://github.com/jupyter-widgets/ipywidgets",
        }
        assert self.collector._extract_repo_url(info, "ipywidgets") == "https://github.com/jupyter-widgets/ipywidgets"

    def test_long_description_picks_matching_url(self):
        """If description has multiple URLs, picks the one matching the package name."""
        info = {
            "project_urls": {},
            "description": (
                "Based on https://github.com/other/lib\n"
                "Source: https://github.com/owner/mypackage"
            ),
        }
        assert self.collector._extract_repo_url(info, "mypackage") == "https://github.com/owner/mypackage"

    def test_sponsors_link_not_treated_as_repo(self):
        """A Funding entry pointing at github.com/sponsors/<user> must not
        win priority 3 — it would fail to clone and get negative-cached."""
        info = {
            "project_urls": {"Funding": "https://github.com/sponsors/someone"},
            "description": "",
        }
        assert self.collector._extract_repo_url(info, "mypackage") == ""

    def test_sponsors_link_skipped_falls_through_to_description(self):
        info = {
            "project_urls": {"Funding": "https://github.com/sponsors/someone"},
            "description": "Source: https://github.com/owner/mypackage",
        }
        assert (
            self.collector._extract_repo_url(info, "mypackage")
            == "https://github.com/owner/mypackage"
        )

    def test_short_name_does_not_match_url_host(self):
        """Name guard must match against the owner/repo path only — a
        package named 'git' would otherwise match the host of any link."""
        info = {
            "project_urls": {},
            "description": "Badge: https://github.com/unrelated/project",
        }
        assert self.collector._extract_repo_url(info, "git") == ""

    def test_short_name_still_matches_in_path(self):
        info = {
            "project_urls": {},
            "description": "Source: https://github.com/owner/git",
        }
        assert self.collector._extract_repo_url(info, "git") == "https://github.com/owner/git"
