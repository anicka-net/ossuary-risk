"""Risk scoring factors and data structures."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RiskLevel(str, Enum):
    """Risk level classification."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    LOW = "LOW"
    VERY_LOW = "VERY_LOW"

    @classmethod
    def from_score(cls, score: int) -> "RiskLevel":
        """Get risk level from numeric score."""
        if score >= 80:
            return cls.CRITICAL
        elif score >= 60:
            return cls.HIGH
        elif score >= 40:
            return cls.MODERATE
        elif score >= 20:
            return cls.LOW
        else:
            return cls.VERY_LOW

    @property
    def semaphore(self) -> str:
        """Get semaphore emoji for this risk level."""
        return {
            RiskLevel.CRITICAL: "🔴",
            RiskLevel.HIGH: "🟠",
            RiskLevel.MODERATE: "🟡",
            RiskLevel.LOW: "🟢",
            RiskLevel.VERY_LOW: "🟢",
        }[self]

    @property
    def description(self) -> str:
        """Human-readable description of the risk level."""
        return {
            RiskLevel.CRITICAL: "Immediate risk - action required",
            RiskLevel.HIGH: "Elevated risk - intervention recommended",
            RiskLevel.MODERATE: "Requires active monitoring",
            RiskLevel.LOW: "Minor concerns, generally stable",
            RiskLevel.VERY_LOW: "Safe, well-governed package",
        }[self]


@dataclass
class ProtectiveFactors:
    """Breakdown of protective factors that reduce risk."""

    # Factor scores (negative = reduces risk, positive = increases risk)
    reputation_score: int = 0  # -25 for tier-1 maintainer
    funding_score: int = 0  # -15 for GitHub Sponsors
    org_score: int = 0  # -15 for org with 3+ admins
    visibility_score: int = 0  # -20 for >50M downloads, -10 for >10M
    distributed_score: int = 0  # -10 for <40% concentration
    community_score: int = 0  # -10 for >20 contributors
    cii_score: int = 0  # -10 for CII badge
    frustration_score: int = 0  # +20 for detected frustration
    sentiment_score: int = 0  # -10 to +20 based on sentiment analysis

    # Evidence for each factor
    reputation_evidence: Optional[str] = None
    funding_evidence: Optional[str] = None
    frustration_evidence: list[str] = field(default_factory=list)
    sentiment_evidence: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Calculate total protective factor modifier."""
        return (
            self.reputation_score
            + self.funding_score
            + self.org_score
            + self.visibility_score
            + self.distributed_score
            + self.community_score
            + self.cii_score
            + self.frustration_score
            + self.sentiment_score
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "reputation": {
                "score": self.reputation_score,
                "evidence": self.reputation_evidence,
            },
            "funding": {"score": self.funding_score, "evidence": self.funding_evidence},
            "organization": {"score": self.org_score},
            "visibility": {"score": self.visibility_score},
            "distributed_governance": {"score": self.distributed_score},
            "community": {"score": self.community_score},
            "cii_badge": {"score": self.cii_score},
            "frustration": {
                "score": self.frustration_score,
                "evidence": self.frustration_evidence,
            },
            "sentiment": {
                "score": self.sentiment_score,
                "evidence": self.sentiment_evidence,
            },
            "total": self.total,
        }


@dataclass
class RiskBreakdown:
    """Complete risk assessment result."""

    # Package identification
    package_name: str
    ecosystem: str
    repo_url: Optional[str] = None

    # Core metrics
    maintainer_concentration: float = 0.0
    commits_last_year: int = 0
    unique_contributors: int = 0
    weekly_downloads: int = 0

    # Score components
    base_risk: int = 0
    activity_modifier: int = 0
    protective_factors: ProtectiveFactors = field(default_factory=ProtectiveFactors)

    # Final score
    final_score: int = 0
    risk_level: RiskLevel = RiskLevel.VERY_LOW

    # Explanation
    explanation: str = ""
    recommendations: list[str] = field(default_factory=list)

    # Data completeness tracking
    data_sources: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "package": {
                "name": self.package_name,
                "ecosystem": self.ecosystem,
                "repo_url": self.repo_url,
            },
            "metrics": {
                "maintainer_concentration": self.maintainer_concentration,
                "commits_last_year": self.commits_last_year,
                "unique_contributors": self.unique_contributors,
                "weekly_downloads": self.weekly_downloads,
            },
            "score": {
                "final": self.final_score,
                "risk_level": self.risk_level.value,
                "semaphore": self.risk_level.semaphore,
                "components": {
                    "base_risk": self.base_risk,
                    "activity_modifier": self.activity_modifier,
                    "protective_factors": self.protective_factors.to_dict(),
                },
            },
            "explanation": self.explanation,
            "recommendations": self.recommendations,
            "data_sources": self.data_sources,
            "warnings": self.warnings,
        }
