"""Tests for the scoring engine."""

from datetime import datetime, timedelta

import pytest

from ossuary._compat import parse_utc_date_end
from ossuary.collectors.github import GitHubData, IssueData
from ossuary.collectors.git import CommitData
from ossuary.scoring.engine import PackageMetrics, RiskScorer
from ossuary.scoring.factors import RiskLevel
from ossuary.scoring.reputation import ReputationBreakdown, ReputationTier
from ossuary.services.scorer import CollectedData, _rebuild_breakdown, calculate_score_for_date


class TestRiskScorer:
    """Tests for RiskScorer class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.scorer = RiskScorer()

    def test_base_risk_very_low_concentration(self):
        """Test base risk with <30% concentration."""
        assert self.scorer.calculate_base_risk(25) == 20

    def test_base_risk_low_concentration(self):
        """Test base risk with 30-50% concentration."""
        assert self.scorer.calculate_base_risk(40) == 40

    def test_base_risk_moderate_concentration(self):
        """Test base risk with 50-70% concentration."""
        assert self.scorer.calculate_base_risk(60) == 60

    def test_base_risk_high_concentration(self):
        """Test base risk with 70-90% concentration."""
        assert self.scorer.calculate_base_risk(80) == 80

    def test_base_risk_critical_concentration(self):
        """Test base risk with >90% concentration."""
        assert self.scorer.calculate_base_risk(95) == 100

    def test_activity_modifier_active(self):
        """Test activity modifier for active projects (>50 commits)."""
        assert self.scorer.calculate_activity_modifier(100) == -30

    def test_activity_modifier_moderate(self):
        """Test activity modifier for moderate activity (12-50 commits)."""
        assert self.scorer.calculate_activity_modifier(30) == -15

    def test_activity_modifier_low(self):
        """Test activity modifier for low activity (4-11 commits)."""
        assert self.scorer.calculate_activity_modifier(8) == 0

    def test_activity_modifier_abandoned(self):
        """Test activity modifier for abandoned projects (<4 commits)."""
        assert self.scorer.calculate_activity_modifier(2) == 20

    def test_risk_level_from_score(self):
        """Test risk level classification from scores."""
        assert RiskLevel.from_score(85) == RiskLevel.CRITICAL
        assert RiskLevel.from_score(70) == RiskLevel.HIGH
        assert RiskLevel.from_score(50) == RiskLevel.MODERATE
        assert RiskLevel.from_score(30) == RiskLevel.LOW
        assert RiskLevel.from_score(10) == RiskLevel.VERY_LOW

    def test_event_stream_scenario(self):
        """Test scoring for event-stream-like scenario (abandoned, high concentration)."""
        metrics = PackageMetrics(
            maintainer_concentration=90,
            commits_last_year=4,
            unique_contributors=1,
            weekly_downloads=2_000_000,
        )

        breakdown = self.scorer.calculate("event-stream", "npm", metrics)

        assert breakdown.base_risk == 100  # >90% concentration
        assert breakdown.activity_modifier == 0  # 4 commits = low
        assert breakdown.final_score >= 80  # Should be critical
        assert breakdown.risk_level == RiskLevel.CRITICAL

    def test_chalk_scenario(self):
        """Test scoring for chalk-like scenario (high concentration but protective factors)."""
        # Pre-calculate a tier-1 reputation to inject directly
        tier1_reputation = ReputationBreakdown(
            username="sindresorhus",
            tenure_score=15,
            portfolio_score=15,
            stars_score=15,
            sponsors_score=15,
        )
        metrics = PackageMetrics(
            maintainer_concentration=80,
            commits_last_year=5,
            unique_contributors=5,
            weekly_downloads=60_000_000,
            has_github_sponsors=True,
            reputation=tier1_reputation,
        )

        breakdown = self.scorer.calculate("chalk", "npm", metrics)

        assert breakdown.base_risk == 80  # 70-90% concentration
        # Should have significant protective factor reduction
        assert breakdown.protective_factors.reputation_score == -25  # Tier-1
        assert breakdown.protective_factors.funding_score == -15  # Sponsors
        assert breakdown.protective_factors.visibility_score == -20  # >50M downloads
        # Final score should be low despite high concentration
        assert breakdown.final_score <= 40
        assert breakdown.risk_level in (RiskLevel.LOW, RiskLevel.VERY_LOW)

    def test_urllib3_scenario(self):
        """Test scoring for urllib3-like scenario (distributed, active)."""
        metrics = PackageMetrics(
            maintainer_concentration=37,
            commits_last_year=109,
            unique_contributors=31,
            weekly_downloads=50_000_000,
            is_org_owned=True,
            org_admin_count=4,
        )

        breakdown = self.scorer.calculate("urllib3", "pypi", metrics)

        assert breakdown.base_risk == 40  # 30-50% concentration
        assert breakdown.activity_modifier == -30  # Active
        assert breakdown.protective_factors.distributed_score == -10  # <40%
        assert breakdown.protective_factors.community_score == -10  # >20 contributors
        assert breakdown.protective_factors.org_score == -15  # Org with 3+ admins
        # Final score should be very low
        assert breakdown.final_score <= 20
        assert breakdown.risk_level == RiskLevel.VERY_LOW

    def test_frustration_increases_risk(self):
        """Test that frustration detection increases risk score."""
        metrics_without = PackageMetrics(
            maintainer_concentration=80,
            commits_last_year=10,
            frustration_detected=False,
        )

        metrics_with = PackageMetrics(
            maintainer_concentration=80,
            commits_last_year=10,
            frustration_detected=True,
            frustration_evidence=["Public protest about funding"],
        )

        score_without = self.scorer.calculate("test", "npm", metrics_without)
        score_with = self.scorer.calculate("test", "npm", metrics_with)

        assert score_with.final_score > score_without.final_score
        assert score_with.protective_factors.frustration_score == 15

    def test_score_clamping(self):
        """Test that scores are clamped to 0-100 range."""
        # Scenario that would exceed 100
        metrics_high = PackageMetrics(
            maintainer_concentration=95,
            commits_last_year=2,
            frustration_detected=True,
        )

        # Scenario that would go below 0
        metrics_low = PackageMetrics(
            maintainer_concentration=20,
            commits_last_year=200,
            unique_contributors=50,
            weekly_downloads=100_000_000,
            maintainer_public_repos=600,
            maintainer_total_stars=200_000,
            has_github_sponsors=True,
            is_org_owned=True,
            org_admin_count=5,
        )

        high_breakdown = self.scorer.calculate("high", "npm", metrics_high)
        low_breakdown = self.scorer.calculate("low", "npm", metrics_low)

        assert high_breakdown.final_score == 100
        assert low_breakdown.final_score == 0


class TestHistoricalScoring:
    """Regression tests for historical scoring behavior."""

    def test_historical_reputation_is_neutralized_when_top_identity_changed(self):
        commits = [
            CommitData(
                sha="old",
                author_name="Alice",
                author_email="alice@example.com",
                authored_date=datetime(2020, 1, 1),
                committer_name="Alice",
                committer_email="alice@example.com",
                committed_date=datetime(2020, 1, 1),
                message="initial commit",
            ),
            CommitData(
                sha="new",
                author_name="Bob",
                author_email="bob@example.com",
                authored_date=datetime.now() - timedelta(days=1),
                committer_name="Bob",
                committer_email="bob@example.com",
                committed_date=datetime.now() - timedelta(days=1),
                message="current maintenance",
            ),
        ]
        data = CollectedData(
            repo_url="https://github.com/example/pkg",
            all_commits=commits,
            github_data=GitHubData(
                maintainer_username="bob",
                maintainer_source_email="bob@example.com",
                maintainer_public_repos=50,
                maintainer_total_stars=10_000,
                maintainer_repos=[
                    {
                        "created_at": "2019-01-01T00:00:00Z",
                        "stargazers_count": 10_000,
                    }
                ],
            ),
            weekly_downloads=1_000,
            maintainer_account_created=datetime(2010, 1, 1),
        )

        historical = calculate_score_for_date(
            "pkg", "github", data, datetime(2021, 1, 1)
        )

        assert historical.protective_factors.reputation_score == 0
        assert historical.factor_availability["reputation"] == (
            "unavailable_historical_maintainer_identity_changed"
        )
        assert any("top contributor at the cutoff differs" in warning
                   for warning in historical.warnings)

    def test_historical_reputation_is_neutralized_without_identity_binding(self):
        commit = CommitData(
            sha="old", author_name="Alice", author_email="alice@example.com",
            authored_date=datetime(2020, 1, 1), committer_name="Alice",
            committer_email="alice@example.com",
            committed_date=datetime(2020, 1, 1), message="initial",
        )
        data = CollectedData(
            repo_url="https://github.com/example/pkg",
            all_commits=[commit],
            github_data=GitHubData(
                maintainer_username="someone",
                maintainer_source_email="",
                maintainer_repos=[{
                    "created_at": "2010-01-01T00:00:00Z",
                    "stargazers_count": 100_000,
                }],
            ),
            weekly_downloads=0,
            maintainer_account_created=datetime(2010, 1, 1),
        )
        result = calculate_score_for_date(
            "pkg", "github", data, datetime(2021, 1, 1)
        )
        assert result.protective_factors.reputation_score == 0
        assert result.factor_availability["reputation"] == (
            "unavailable_historical_maintainer_identity_changed"
        )

    def test_historical_scoring_uses_tenure_only_from_current_profile(self):
        cutoff = datetime(2021, 1, 1)
        commit = CommitData(
            sha="old",
            author_name="Alice",
            author_email="alice@example.com",
            authored_date=datetime(2020, 6, 1),
            committer_name="Alice",
            committer_email="alice@example.com",
            committed_date=datetime(2020, 6, 1),
            message="initial",
        )
        current_repos = [
            {
                "created_at": "2010-01-01T00:00:00Z",
                "fork": False,
                "stargazers_count": 2_000,
            }
            for _ in range(60)
        ]
        data = CollectedData(
            repo_url="https://github.com/example/lodash",
            all_commits=[commit],
            github_data=GitHubData(
                maintainer_username="alice",
                maintainer_source_email="alice@example.com",
                maintainer_repos=current_repos,
                maintainer_orgs=["nodejs"],
                maintainer_sponsor_count=10,
                has_github_sponsors=True,
                cii_badge_level="passing",
            ),
            weekly_downloads=0,
            maintainer_account_created=datetime(2010, 1, 1),
        )

        current = calculate_score_for_date(
            "lodash", "npm", data, cutoff, is_historical=False
        )
        historical = calculate_score_for_date(
            "lodash", "npm", data, cutoff, is_historical=True
        )

        assert current.protective_factors.reputation_score == -25
        assert current.protective_factors.funding_score == -15
        assert current.protective_factors.cii_score == -10
        assert historical.protective_factors.reputation_score == 0
        assert historical.protective_factors.funding_score == 0
        assert historical.protective_factors.cii_score == 0
        assert historical.factor_availability["reputation"] == (
            "historical_tenure_only"
        )
        assert historical.factor_availability["cii_badge"] == (
            "unavailable_historical_neutralized"
        )

    def test_yesterday_defaults_to_historical_but_explicit_intent_wins(self):
        cutoff = datetime.now() - timedelta(days=1)
        data = CollectedData(
            repo_url="https://github.com/example/pkg",
            all_commits=[],
            github_data=GitHubData(),
            weekly_downloads=100_000_000,
            maintainer_account_created=None,
        )

        inferred = calculate_score_for_date("pkg", "npm", data, cutoff)
        explicit_current = calculate_score_for_date(
            "pkg", "npm", data, cutoff, is_historical=False
        )

        assert inferred.protective_factors.visibility_score == 0
        assert explicit_current.protective_factors.visibility_score == -20

    def test_named_cutoff_includes_whole_day_and_excludes_next_day(self):
        cutoff = parse_utc_date_end("2026-08-14")
        commits = [
            CommitData(
                sha=sha,
                author_name="Alice",
                author_email="alice@example.com",
                authored_date=timestamp,
                committer_name="Alice",
                committer_email="alice@example.com",
                committed_date=timestamp,
                message="maintenance",
            )
            for sha, timestamp in (
                ("before", datetime(2026, 8, 13, 12, 0)),
                ("same-day", datetime(2026, 8, 14, 20, 0)),
                ("next-day", datetime(2026, 8, 15, 0, 0)),
            )
        ]
        data = CollectedData(
            repo_url="https://github.com/example/pkg",
            all_commits=commits,
            github_data=GitHubData(),
            weekly_downloads=0,
            maintainer_account_created=None,
        )

        result = calculate_score_for_date(
            "pkg", "github", data, cutoff, is_historical=True
        )

        assert result.commits_last_year == 2

    def test_historical_scoring_excludes_commits_committed_after_cutoff(self):
        cutoff = datetime(2020, 6, 1)
        commits = [
            CommitData(
                sha="visible", author_name="Alice",
                author_email="alice@example.com",
                authored_date=datetime(2020, 1, 1),
                committer_name="Alice", committer_email="alice@example.com",
                committed_date=datetime(2020, 1, 1), message="visible",
            ),
            CommitData(
                sha="backdated", author_name="Bob",
                author_email="bob@example.com",
                authored_date=datetime(2020, 2, 1),
                committer_name="Bob", committer_email="bob@example.com",
                committed_date=datetime(2020, 7, 1), message="future merge",
            ),
        ]
        data = CollectedData(
            repo_url="https://github.com/example/pkg",
            all_commits=commits, github_data=GitHubData(),
            weekly_downloads=0, maintainer_account_created=None,
        )
        result = calculate_score_for_date("pkg", "github", data, cutoff)
        assert result.commits_last_year == 1

    def test_historical_scoring_neutralizes_current_downloads(self):
        commits = [CommitData(
            sha="1", author_name="Alice", author_email="alice@example.com",
            authored_date=datetime(2020, 1, 1), committer_name="Alice",
            committer_email="alice@example.com",
            committed_date=datetime(2020, 1, 1), message="visible",
        )]
        data = CollectedData(
            repo_url="https://github.com/example/pkg",
            all_commits=commits, github_data=GitHubData(),
            weekly_downloads=100_000_000, maintainer_account_created=None,
        )
        result = calculate_score_for_date(
            "pkg", "npm", data, datetime(2021, 1, 1)
        )
        assert result.weekly_downloads == 0
        assert result.protective_factors.visibility_score == 0
        assert result.factor_availability["visibility"] == (
            "unavailable_historical_neutralized"
        )

    def test_historical_scoring_neutralizes_current_merge_sample(self):
        commits = [CommitData(
            sha="1", author_name="Alice", author_email="alice@example.com",
            authored_date=datetime(2020, 1, 1), committer_name="Alice",
            committer_email="alice@example.com",
            committed_date=datetime(2020, 1, 1), message="visible",
        )]
        data = CollectedData(
            repo_url="https://github.com/example/pkg", all_commits=commits,
            github_data=GitHubData(
                merged_prs=[
                    {"login": "alice", "merged_at": "2020-01-02T00:00:00Z"}
                    for _ in range(20)
                ],
                merge_bus_factor=1,
            ),
            weekly_downloads=0, maintainer_account_created=None,
        )
        result = calculate_score_for_date(
            "pkg", "github", data, datetime(2021, 1, 1)
        )
        assert result.factor_availability["merge_signals"] == (
            "unavailable_historical_neutralized"
        )
        assert any("merge-author signals" in warning for warning in result.warnings)

    def test_calculate_score_for_date_ignores_future_issue_sentiment(self):
        """Historical scores must not include issue content created after cutoff."""
        commits = [
            CommitData(
                sha="1",
                author_name="maintainer",
                author_email="maintainer@example.com",
                authored_date=datetime(2020, 1, 1),
                committer_name="maintainer",
                committer_email="maintainer@example.com",
                committed_date=datetime(2020, 1, 1),
                message="initial commit",
            )
        ]
        future_issue = IssueData(
            number=1,
            title="Burnout",
            body="I am burned out and tired of this free work",
            state="open",
            is_pull_request=False,
            author_login="maintainer",
            created_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",
            closed_at=None,
            comments=[],
        )
        data = CollectedData(
            repo_url="https://github.com/example/pkg",
            all_commits=commits,
            github_data=GitHubData(issues=[future_issue]),
            weekly_downloads=0,
            maintainer_account_created=None,
        )

        breakdown = calculate_score_for_date(
            "pkg", "github", data, datetime(2021, 1, 1)
        )

        assert breakdown.protective_factors.frustration_score == 0
        assert breakdown.protective_factors.sentiment_score == 0

    def test_calculate_score_for_date_disables_historical_star_proxy(self):
        """Historical scoring must not use present-day GitHub stars as a past visibility proxy."""
        commits = [
            CommitData(
                sha="1",
                author_name="maintainer",
                author_email="maintainer@example.com",
                authored_date=datetime(2020, 1, 1),
                committer_name="maintainer",
                committer_email="maintainer@example.com",
                committed_date=datetime(2020, 1, 1),
                message="initial commit",
            )
        ]
        data = CollectedData(
            repo_url="https://github.com/example/pkg",
            all_commits=commits,
            github_data=GitHubData(),
            weekly_downloads=0,
            maintainer_account_created=None,
            repo_stargazers=60_000,
        )

        current = calculate_score_for_date(
            "pkg", "github", data, datetime.now()
        )
        historical = calculate_score_for_date(
            "pkg", "github", data, datetime(2021, 1, 1)
        )

        assert current.protective_factors.visibility_score == -20
        assert historical.protective_factors.visibility_score == 0
        assert historical.factor_availability["visibility"] == "unavailable_historical_neutralized"
        assert any("GitHub-star visibility proxy" in warning for warning in historical.warnings)

    def test_calculate_score_for_date_disables_historical_issue_sentiment_from_current_snapshot(self):
        """Historical scoring should not use current issue snapshots for past sentiment."""
        commits = [
            CommitData(
                sha="1",
                author_name="maintainer",
                author_email="maintainer@example.com",
                authored_date=datetime(2024, 1, 1),
                committer_name="maintainer",
                committer_email="maintainer@example.com",
                committed_date=datetime(2024, 1, 1),
                message="normal maintenance",
            )
        ]
        issue = IssueData(
            number=1,
            title="Burnout",
            body="I am burned out and tired of this free work",
            state="open",
            is_pull_request=False,
            author_login="maintainer",
            created_at="2024-01-02T00:00:00Z",
            updated_at="2024-01-02T00:00:00Z",
            closed_at=None,
            comments=[
                {
                    "id": 1,
                    "author": "maintainer",
                    "body": "Corporate exploitation is exhausting",
                    "created_at": "2024-01-03T00:00:00Z",
                }
            ],
        )
        data = CollectedData(
            repo_url="https://github.com/example/pkg",
            all_commits=commits,
            github_data=GitHubData(issues=[issue]),
            weekly_downloads=0,
            maintainer_account_created=None,
        )

        current = calculate_score_for_date(
            "pkg", "github", data, datetime.now()
        )
        historical = calculate_score_for_date(
            "pkg", "github", data, datetime(2024, 12, 31)
        )

        assert current.protective_factors.frustration_score == 15
        assert historical.protective_factors.frustration_score == 0
        assert historical.protective_factors.sentiment_score == 0
        assert historical.factor_availability["issue_sentiment"] == "disabled_historical_partial_snapshot"
        assert any("issue/comment sentiment" in warning for warning in historical.warnings)

    # Removed in v6.3: sentiment_score is structurally 0 in the scoring
    # formula (see ProtectiveFactors.sentiment_score docstring). The
    # commit-vs-issue sample-count weighting that this test exercised is
    # now a sentiment-analyzer-layer concern; if it needs coverage it
    # belongs in test_sentiment.py against the analyzer's own output, not
    # against the scoring engine's protective-factor breakdown.

    def test_calculate_score_for_date_passes_through_cii_badge(self):
        """CII badge data from the collector should affect scoring."""
        commits = [
            CommitData(
                sha="1",
                author_name="maintainer",
                author_email="maintainer@example.com",
                authored_date=datetime(2024, 1, 1),
                committer_name="maintainer",
                committer_email="maintainer@example.com",
                committed_date=datetime(2024, 1, 1),
                message="normal maintenance",
            )
        ]
        data = CollectedData(
            repo_url="https://github.com/example/pkg",
            all_commits=commits,
            github_data=GitHubData(cii_badge_level="passing"),
            weekly_downloads=0,
            maintainer_account_created=None,
        )

        breakdown = calculate_score_for_date(
            "pkg",
            "github",
            data,
            datetime(2024, 12, 31),
            is_historical=False,
        )

        assert breakdown.protective_factors.cii_score == -10


class TestCachedBreakdownRebuild:
    """Regression tests for cache reconstruction."""

    def test_rebuild_breakdown_preserves_chaoss_signals(self):
        cached_score = type(
            "CachedScore",
            (),
            {
                "breakdown": {
                    "package": {"repo_url": "https://github.com/example/pkg"},
                    "metrics": {
                        "maintainer_concentration": 50,
                        "commits_last_year": 12,
                        "unique_contributors": 4,
                        "weekly_downloads": 100,
                    },
                    "chaoss_signals": {
                        "bus_factor": 2,
                        "elephant_factor": 1,
                        "inactive_contributor_ratio": 0.5,
                    },
                    "score": {"components": {"protective_factors": {}}},
                    "explanation": "cached",
                    "recommendations": [],
                    "data_sources": {},
                    "factor_availability": {"visibility": "registry_downloads"},
                    "warnings": [],
                },
                "risk_level": "LOW",
                "base_risk": 40,
                "activity_modifier": -15,
                "final_score": 25,
                "maintainer_concentration": 50,
                "commits_last_year": 12,
                "unique_contributors": 4,
                "weekly_downloads": 100,
            },
        )()

        breakdown = _rebuild_breakdown(cached_score, "pkg", "github")

        assert breakdown is not None
        assert breakdown.bus_factor == 2
        assert breakdown.elephant_factor == 1
        assert breakdown.inactive_contributor_ratio == 0.5
        assert breakdown.factor_availability["visibility"] == "registry_downloads"
