"""Tests for risk factors and levels."""

import pytest

from ossuary.scoring.factors import ProtectiveFactors, RiskBreakdown, RiskLevel


class TestRiskLevel:
    """Tests for RiskLevel enum."""

    def test_critical_threshold(self):
        assert RiskLevel.from_score(80) == RiskLevel.CRITICAL
        assert RiskLevel.from_score(100) == RiskLevel.CRITICAL

    def test_high_threshold(self):
        assert RiskLevel.from_score(60) == RiskLevel.HIGH
        assert RiskLevel.from_score(79) == RiskLevel.HIGH

    def test_moderate_threshold(self):
        assert RiskLevel.from_score(40) == RiskLevel.MODERATE
        assert RiskLevel.from_score(59) == RiskLevel.MODERATE

    def test_low_threshold(self):
        assert RiskLevel.from_score(20) == RiskLevel.LOW
        assert RiskLevel.from_score(39) == RiskLevel.LOW

    def test_very_low_threshold(self):
        assert RiskLevel.from_score(0) == RiskLevel.VERY_LOW
        assert RiskLevel.from_score(19) == RiskLevel.VERY_LOW

    def test_semaphore_colors(self):
        assert RiskLevel.CRITICAL.semaphore == "\U0001f534"  # red circle
        assert RiskLevel.VERY_LOW.semaphore == "\U0001f7e2"  # green circle

    def test_description_not_empty(self):
        for level in RiskLevel:
            assert level.description


class TestProtectiveFactors:
    """Tests for ProtectiveFactors dataclass."""

    def test_total_sums_all_factors(self):
        pf = ProtectiveFactors(
            reputation_score=-25,
            funding_score=-15,
            org_score=-15,
            visibility_score=-20,
        )
        assert pf.total == -75

    def test_total_includes_positive_factors(self):
        pf = ProtectiveFactors(
            reputation_score=-25,
            frustration_score=20,
        )
        assert pf.total == -5

    def test_total_zero_when_empty(self):
        pf = ProtectiveFactors()
        assert pf.total == 0

    def test_to_dict(self):
        pf = ProtectiveFactors(reputation_score=-25)
        d = pf.to_dict()
        assert d["reputation"]["score"] == -25
        assert "total" in d


class TestRiskBreakdown:
    """Tests for RiskBreakdown dataclass."""

    def test_to_dict_includes_all_fields(self):
        pf = ProtectiveFactors()
        rb = RiskBreakdown(
            package_name="test",
            ecosystem="npm",
            base_risk=80,
            activity_modifier=-15,
            protective_factors=pf,
            final_score=65,
            risk_level=RiskLevel.HIGH,
            explanation="Test explanation",
            recommendations=["Do something"],
        )
        d = rb.to_dict()
        assert d["package"]["name"] == "test"
        assert d["package"]["ecosystem"] == "npm"
        assert d["score"]["final"] == 65
        assert d["score"]["risk_level"] == "HIGH"
        assert "protective_factors" in d["score"]["components"]
