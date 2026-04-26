"""Methodology contracts: single source of truth for documentation.

Constants here are the authoritative values for the scoring methodology.
Documentation (README, docs/methodology.md, docs/validation.md, dashboard
methodology page) and the validation artifact (scripts/validate.py output)
must agree with these values; ``tests/test_doc_code_drift.py`` enforces
that contract.

Bump ``METHODOLOGY_VERSION`` whenever any value in this module changes
that affects produced scores or label boundaries.
"""

from __future__ import annotations

METHODOLOGY_VERSION = "6.3"
"""Active scoring methodology version (without ``v`` prefix; prose
references add the ``v`` themselves). v6.3 lowered the frustration weight
from +20 to +15 and removed the VADER sentiment branch from the score
formula (see the factor-ablation pass)."""

FRUSTRATION_WEIGHT = 15
"""Points added when economic/maintainer frustration is detected.
Lowered from 20 in v6.3."""

SENTIMENT_IN_SCORE = False
"""Whether the VADER sentiment magnitude contributes to the final score.
False in v6.3: the factor-ablation pass found that no packages crossed the
±0.3 threshold on the v6.2.1 validation baseline, so the signal earned no
detectable contribution. Field retained on ``ProtectiveFactors`` as
structurally 0 for cached-score deserialisation compatibility."""

PREDICTION_THRESHOLD = 60
"""Score >= PREDICTION_THRESHOLD means "predicted risky" in validation.
Aligns with the HIGH risk bucket boundary in ``RISK_THRESHOLDS``."""

RISK_THRESHOLDS: list[tuple[int, str]] = [
    (80, "CRITICAL"),
    (60, "HIGH"),
    (40, "MODERATE"),
    (20, "LOW"),
    (0, "VERY_LOW"),
]
"""Risk bucket thresholds, ordered high to low. Each pair ``(threshold,
label)`` means "score >= threshold and below the previous threshold gets
this label". A score of 80 is CRITICAL; 60 is HIGH; 40 is MODERATE; 20 is
LOW; below 20 is VERY_LOW."""


def label_for_score(score: int) -> str:
    """Risk bucket label for a numeric score, derived from RISK_THRESHOLDS."""
    for threshold, label in RISK_THRESHOLDS:
        if score >= threshold:
            return label
    return RISK_THRESHOLDS[-1][1]


# Validation scope (per docs/methodology.md §8.2 Scoped Validation Framework)
IN_SCOPE_TIERS: frozenset[str] = frozenset({"T1", "T2", "T3", "T_risk"})
"""Detectability tiers counted toward Scope B precision/recall:
T1 governance decay, T2 protestware/sabotage, T3 weak-governance
account compromise, T_risk governance risk (no incident yet)."""

OUT_OF_SCOPE_TIERS: frozenset[str] = frozenset({"T4", "T5"})
"""Tiers excluded from Scope B: T4 well-governed credential theft,
T5 CI/CD pipeline exploits. Included in the dataset to validate the
detection boundary; not penalised as false negatives."""
