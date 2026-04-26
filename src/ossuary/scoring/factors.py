"""Risk scoring factors and data structures."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ossuary.scoring.methodology import RISK_THRESHOLDS


class RiskLevel(str, Enum):
    """Risk level classification."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    LOW = "LOW"
    VERY_LOW = "VERY_LOW"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    """One or more required input fetches failed. The score has not been
    computed because the methodology's contract is not to score on partial
    data. Reasons are recorded in ``RiskBreakdown.incomplete_reasons``;
    use ``ossuary rescore-invalid`` to retry."""

    @classmethod
    def from_score(cls, score: int) -> "RiskLevel":
        """Get risk level from numeric score, derived from
        ``methodology.RISK_THRESHOLDS`` so the bucket boundaries have a
        single source of truth."""
        for threshold, label in RISK_THRESHOLDS:
            if score >= threshold:
                return cls(label)
        return cls(RISK_THRESHOLDS[-1][1])

    @property
    def semaphore(self) -> str:
        """Get semaphore emoji for this risk level."""
        return {
            RiskLevel.CRITICAL: "🔴",
            RiskLevel.HIGH: "🟠",
            RiskLevel.MODERATE: "🟡",
            RiskLevel.LOW: "🟢",
            RiskLevel.VERY_LOW: "🟢",
            RiskLevel.INSUFFICIENT_DATA: "⚪",
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
            RiskLevel.INSUFFICIENT_DATA: "Score not computed: required input data unavailable",
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
    frustration_score: int = 0
    """+15 when economic / maintainer frustration is detected (v6.3,
    lowered from +20). The constant lives in
    ``ossuary.scoring.methodology.FRUSTRATION_WEIGHT``."""
    sentiment_score: int = 0
    """Structurally always 0 as of v6.3. Retained for cached-score
    deserialization compatibility. The factor-ablation pass found that the
    VADER sentiment-magnitude signal never crossed the ±0.3 threshold on the
    v6.2.1 validation baseline, so it contributed nothing detectable. The
    rule-based frustration layer captures the detectable emotional signal;
    sentiment may earn its place back when the deferred layer-3 embedding
    work (methodology.md §6.6) ships."""
    maturity_score: int = 0
    """Structurally always 0. Retained only so ``maturity_evidence`` has a
    natural home on ``ProtectiveFactors``. The real maturity contribution is
    in ``RiskScorer.calculate()``: it suppresses the activity penalty and
    swaps in lifetime concentration for mature-but-quiet projects. See the
    v6.3 factor-ablation note."""
    takeover_risk_score: int = 0  # +20 for newcomer takeover signal

    # Evidence for each factor
    reputation_evidence: Optional[str] = None
    funding_evidence: Optional[str] = None
    frustration_evidence: list[str] = field(default_factory=list)
    sentiment_evidence: list[str] = field(default_factory=list)
    maturity_evidence: Optional[str] = None
    takeover_risk_evidence: Optional[str] = None

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
            + self.maturity_score
            + self.takeover_risk_score
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
            "maturity": {
                "score": self.maturity_score,
                "evidence": self.maturity_evidence,
            },
            "takeover_risk": {
                "score": self.takeover_risk_score,
                "evidence": self.takeover_risk_evidence,
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
    # CHAOSS-aligned governance signals
    bus_factor: int = 0               # minimum contributors for 50% of commits
    elephant_factor: int = 0          # minimum organizations for 50% of commits
    inactive_contributor_ratio: float = 0.0  # fraction of lifetime contributors absent recently
    commits_last_year: int = 0
    unique_contributors: int = 0
    weekly_downloads: int = 0

    # Score components
    base_risk: int = 0
    activity_modifier: int = 0
    protective_factors: ProtectiveFactors = field(default_factory=ProtectiveFactors)

    # Final score. ``None`` when ``risk_level == INSUFFICIENT_DATA`` —
    # the methodology contract is not to compute a number from partial
    # input data; reasons are listed in ``incomplete_reasons``.
    final_score: Optional[int] = 0
    risk_level: RiskLevel = RiskLevel.VERY_LOW

    # Reasons the score is INSUFFICIENT_DATA, populated only in that case.
    # Each entry is a single-line, human-readable failure description such
    # as ``"pypi.weekly_downloads: HTTP 429 from pypistats.org"``.
    incomplete_reasons: list[str] = field(default_factory=list)

    # Reasons the score is PROVISIONAL: a non-essential signal failed
    # (e.g. GitHub Sponsors lookup rate-limited) and the score was still
    # computed, but with at least one protective factor missing. Because
    # missing protective factors default to 0, a provisional score is
    # *higher than the true score* — conservative, not dangerous, but
    # the user should re-run once the upstream recovers.
    # The split vs ``incomplete_reasons`` is signal-magnitude based, not
    # direction-of-bias based: both classes of failure raise the score.
    # Visibility (downloads) is the largest protective factor and gates
    # the popular-vs-obscure distinction → refused as INSUFFICIENT_DATA.
    # Auxiliary GitHub signals (sponsors, orgs, etc.) are smaller and
    # corroborating → score still produced, flagged provisional.
    # Reasons are single-line strings such as
    # ``"github.sponsors: HTTP 403 from api.github.com"``.
    provisional_reasons: list[str] = field(default_factory=list)

    # Explanation
    explanation: str = ""
    recommendations: list[str] = field(default_factory=list)

    # Data completeness tracking
    data_sources: dict[str, bool] = field(default_factory=dict)
    factor_availability: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_provisional(self) -> bool:
        """True iff the score was computed with one or more non-essential
        signals missing. The score is conservative (likely too high) and
        should be retried via ``ossuary rescore-invalid``."""
        return bool(self.provisional_reasons)

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
            "chaoss_signals": {
                "bus_factor": self.bus_factor,
                "elephant_factor": self.elephant_factor,
                "inactive_contributor_ratio": round(self.inactive_contributor_ratio, 2),
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
            "factor_availability": self.factor_availability,
            "warnings": self.warnings,
            "incomplete_reasons": self.incomplete_reasons,
            "provisional_reasons": self.provisional_reasons,
            "is_provisional": self.is_provisional,
        }
