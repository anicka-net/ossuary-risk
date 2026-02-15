"""Tests for reputation scoring."""

from datetime import datetime, timedelta

import pytest

from ossuary.scoring.reputation import ReputationBreakdown, ReputationScorer, ReputationTier


class TestReputationTier:
    """Tests for ReputationTier enum."""

    def test_tier1_from_high_score(self):
        assert ReputationTier.from_score(60) == ReputationTier.TIER_1
        assert ReputationTier.from_score(100) == ReputationTier.TIER_1

    def test_tier2_from_mid_score(self):
        assert ReputationTier.from_score(30) == ReputationTier.TIER_2
        assert ReputationTier.from_score(59) == ReputationTier.TIER_2

    def test_unknown_from_low_score(self):
        assert ReputationTier.from_score(0) == ReputationTier.UNKNOWN
        assert ReputationTier.from_score(29) == ReputationTier.UNKNOWN

    def test_risk_reduction_values(self):
        assert ReputationTier.TIER_1.risk_reduction == -25
        assert ReputationTier.TIER_2.risk_reduction == -10
        assert ReputationTier.UNKNOWN.risk_reduction == 0


class TestReputationBreakdown:
    """Tests for ReputationBreakdown dataclass."""

    def test_total_score_sums_signals(self):
        b = ReputationBreakdown(tenure_score=15, stars_score=15)
        assert b.total_score == 30

    def test_total_score_zero_for_empty(self):
        b = ReputationBreakdown()
        assert b.total_score == 0

    def test_tier_derived_from_total(self):
        b = ReputationBreakdown(tenure_score=15, stars_score=15, portfolio_score=15, sponsors_score=15)
        assert b.tier == ReputationTier.TIER_1

    def test_to_dict_includes_tier(self):
        b = ReputationBreakdown(tenure_score=15)
        d = b.to_dict()
        assert "total_score" in d
        assert "tier" in d
        assert d["signals"]["tenure"]["score"] == 15


class TestReputationScorer:
    """Tests for ReputationScorer."""

    def setup_method(self):
        self.scorer = ReputationScorer()

    def test_tenure_points_for_old_account(self):
        created = datetime.now() - timedelta(days=365 * 10)
        result = self.scorer.calculate(
            username="veteran",
            account_created=created,
            repos=[],
            sponsor_count=0,
            orgs=[],
            packages_maintained=[],
        )
        assert result.tenure_score == 15

    def test_no_tenure_points_for_new_account(self):
        created = datetime.now() - timedelta(days=365)
        result = self.scorer.calculate(
            username="newbie",
            account_created=created,
            repos=[],
            sponsor_count=0,
            orgs=[],
            packages_maintained=[],
        )
        assert result.tenure_score == 0

    def test_portfolio_points_for_quality_repos(self):
        repos = [{"stargazers_count": 20, "fork": False} for _ in range(60)]
        result = self.scorer.calculate(
            username="prolific",
            account_created=None,
            repos=repos,
            sponsor_count=0,
            orgs=[],
            packages_maintained=[],
        )
        assert result.portfolio_score == 15

    def test_no_portfolio_points_for_forks(self):
        repos = [{"stargazers_count": 20, "fork": True} for _ in range(60)]
        result = self.scorer.calculate(
            username="forker",
            account_created=None,
            repos=repos,
            sponsor_count=0,
            orgs=[],
            packages_maintained=[],
        )
        assert result.portfolio_score == 0

    def test_stars_points_for_popular_repos(self):
        repos = [{"stargazers_count": 60000, "fork": False}]
        result = self.scorer.calculate(
            username="star",
            account_created=None,
            repos=repos,
            sponsor_count=0,
            orgs=[],
            packages_maintained=[],
        )
        assert result.stars_score == 15

    def test_sponsor_points(self):
        result = self.scorer.calculate(
            username="sponsored",
            account_created=None,
            repos=[],
            sponsor_count=15,
            orgs=[],
            packages_maintained=[],
        )
        assert result.sponsors_score == 15

    def test_no_sponsor_points_below_threshold(self):
        result = self.scorer.calculate(
            username="few_sponsors",
            account_created=None,
            repos=[],
            sponsor_count=5,
            orgs=[],
            packages_maintained=[],
        )
        assert result.sponsors_score == 0

    def test_recognized_org_points(self):
        result = self.scorer.calculate(
            username="member",
            account_created=None,
            repos=[],
            sponsor_count=0,
            orgs=["nodejs", "random-org"],
            packages_maintained=[],
        )
        assert result.org_membership_score == 15

    def test_no_org_points_for_unknown_orgs(self):
        result = self.scorer.calculate(
            username="member",
            account_created=None,
            repos=[],
            sponsor_count=0,
            orgs=["my-company", "random-org"],
            packages_maintained=[],
        )
        assert result.org_membership_score == 0

    def test_packages_points(self):
        result = self.scorer.calculate(
            username="maintainer",
            account_created=None,
            repos=[],
            sponsor_count=0,
            orgs=[],
            packages_maintained=[f"pkg-{i}" for i in range(25)],
        )
        assert result.packages_score == 10

    def test_tier1_scenario(self):
        """Full tier-1 maintainer should score >= 60."""
        created = datetime.now() - timedelta(days=365 * 10)
        repos = [{"stargazers_count": 1000, "fork": False} for _ in range(60)]
        result = self.scorer.calculate(
            username="sindresorhus",
            account_created=created,
            repos=repos,
            sponsor_count=20,
            orgs=["nodejs"],
            packages_maintained=[f"pkg-{i}" for i in range(25)],
        )
        assert result.tier == ReputationTier.TIER_1
        assert result.total_score >= 60

    def test_unknown_scenario(self):
        """New user with no signals should be UNKNOWN."""
        result = self.scorer.calculate(
            username="newuser123",
            account_created=datetime.now() - timedelta(days=30),
            repos=[],
            sponsor_count=0,
            orgs=[],
            packages_maintained=["one-package"],
        )
        assert result.tier == ReputationTier.UNKNOWN
        assert result.total_score < 30
