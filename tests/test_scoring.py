"""Tests for the scoring engine."""

import pytest

from ossuary.scoring.engine import PackageMetrics, RiskScorer
from ossuary.scoring.factors import RiskLevel
from ossuary.scoring.reputation import ReputationBreakdown, ReputationTier


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
        assert score_with.protective_factors.frustration_score == 20

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
