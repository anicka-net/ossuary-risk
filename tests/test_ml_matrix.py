"""Regression tests for diagnostic-ML matrix construction."""

import inspect
from datetime import datetime

from ossuary.collectors.git import CommitData, GitCollector
from scripts import ml_matrix
from scripts.ml_matrix import calculate_metrics_at_cutoff, h_exclusion


def _commit(name: str, authored: str, committed: str) -> CommitData:
    return CommitData(
        sha=name,
        author_name=name,
        author_email=f"{name}@example.com",
        authored_date=datetime.fromisoformat(authored),
        committer_name=name,
        committer_email=f"{name}@example.com",
        committed_date=datetime.fromisoformat(committed),
        message=name,
    )


def test_historical_metrics_exclude_commits_after_cutoff_by_either_timestamp():
    cutoff = datetime(2024, 1, 31, 23, 59, 59)
    before = _commit("before", "2024-01-01", "2024-01-02")
    committed_after = _commit("committed-after", "2024-01-03", "2024-02-01")
    authored_after = _commit("authored-after", "2024-02-01", "2024-01-04")

    metrics = calculate_metrics_at_cutoff(
        GitCollector(), [before, committed_after, authored_after], cutoff
    )

    assert metrics.total_commits == 1
    assert metrics.lifetime_contributors == 1
    assert metrics.first_commit_date == before.authored_date
    assert metrics.last_commit_date == before.authored_date


def test_matrix_builder_has_one_cutoff_safe_metrics_entrypoint():
    source = inspect.getsource(ml_matrix)
    assert source.count(".calculate_metrics(") == 1
    assert "git.calculate_metrics(commits_observable_at(commits, cutoff), cutoff)" in source


def test_polyfill_exclusion_is_keyed_to_exact_case_and_cutoff():
    class Case:
        ecosystem = "github"
        name = "polyfillpolyfill/polyfill-library"
        cutoff_date = "2024-02-01"

    assert "no commits observable" in h_exclusion(Case())

    Case.cutoff_date = "2024-02-02"
    assert h_exclusion(Case()) is None
