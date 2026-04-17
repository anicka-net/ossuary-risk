"""Risk scoring engine."""

from ossuary.scoring.engine import PackageMetrics, RiskScorer
from ossuary.scoring.factors import ProtectiveFactors, RiskBreakdown, RiskLevel
from ossuary.scoring.reputation import ReputationBreakdown, ReputationScorer, ReputationTier

# Methodology version used to identify the scoring rules behind a result.
# Mirrors the Version field in docs/methodology.md so that audit-ready
# outputs (Annex VII export, enriched SBOMs) can declare which methodology
# revision produced the score.
METHODOLOGY_VERSION = "6.0"

__all__ = [
    "METHODOLOGY_VERSION",
    "PackageMetrics",
    "RiskScorer",
    "ProtectiveFactors",
    "RiskBreakdown",
    "RiskLevel",
    "ReputationBreakdown",
    "ReputationScorer",
    "ReputationTier",
]
