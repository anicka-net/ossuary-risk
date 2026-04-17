"""Implied maximum support period derivation.

CRA Article 13(8) requires a manufacturer to determine a product's support
period taking into account, among other factors, "the support periods of
integrated components that provide core functions and are sourced from
third parties." For OSS components there is no formally declared support
period; this module derives a defensible upper bound from Ossuary's
governance score.

The derivation is intentionally heuristic and clearly labelled as such.
The score-to-horizon table below captures the reasoning; it is not derived
from incident data and a manufacturer may justify a different mapping.
The CRA floor is 5 years (60 months) and the value of this analytic is
the binary question: does this dependency support a defensible 5-year
claim, and if not, what does its governance signal suggest as a softer
estimate of remaining viability?
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Optional


CRA_MINIMUM_SUPPORT_MONTHS = 60  # Article 13(8): support period shall be at least five years
DEFAULT_CRITICAL_TOP_N = 5


# Score-to-horizon mapping. Each entry is (score_strict_upper_bound,
# horizon_months, reason). A score of S is matched by the first entry
# where S < score_strict_upper_bound.
DEFAULT_HORIZON_TABLE: list[tuple[int, int, str]] = [
    (20, CRA_MINIMUM_SUPPORT_MONTHS,
     "very low governance risk: no constraint on a 5-year support claim"),
    (40, CRA_MINIMUM_SUPPORT_MONTHS,
     "low governance risk: meets the CRA 5-year minimum"),
    (60, 36,
     "moderate governance risk: 3 years defensible; reassess before extending"),
    (80, 18,
     "high governance risk: 18 months at most without compensating controls"),
    (101, 6,
     "critical governance risk: 6 months at most; consider replacing or forking"),
]


@dataclass
class SupportPeriodEstimate:
    """Per-component implied maximum support period."""

    package_name: str
    ecosystem: str
    score: int
    risk_level: str
    horizon_months: int
    cra_minimum_supportable: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProductSupportPeriod:
    """SBOM-level implied maximum support period.

    The horizon is the minimum of per-component horizons across the
    'critical' subset (top-N by structural importance, or top-N by score
    if structural importance cannot be computed). Limiting components
    are those whose horizon equals the product horizon.
    """

    horizon_months: int
    cra_minimum_supportable: bool
    critical_top_n: int
    critical_selection_method: str  # "structural_importance" or "worst_score"
    limiting_components: list[SupportPeriodEstimate]
    critical_components: list[SupportPeriodEstimate]
    components_total: int
    components_scored: int

    def to_dict(self) -> dict:
        return {
            "horizon_months": self.horizon_months,
            "cra_minimum_supportable": self.cra_minimum_supportable,
            "critical_top_n": self.critical_top_n,
            "critical_selection_method": self.critical_selection_method,
            "limiting_components": [c.to_dict() for c in self.limiting_components],
            "critical_components": [c.to_dict() for c in self.critical_components],
            "components_total": self.components_total,
            "components_scored": self.components_scored,
        }


def horizon_from_score(
    score: int,
    table: Optional[list[tuple[int, int, str]]] = None,
) -> tuple[int, str]:
    """Map a 0-100 governance score to (horizon_months, reason)."""
    table = table or DEFAULT_HORIZON_TABLE
    for upper, months, reason in table:
        if score < upper:
            return months, reason
    return 0, "score out of range"


def estimate_for_score(
    package_name: str,
    ecosystem: str,
    score: int,
    risk_level: str,
    table: Optional[list[tuple[int, int, str]]] = None,
) -> SupportPeriodEstimate:
    """Build a SupportPeriodEstimate from a single component's score."""
    horizon, reason = horizon_from_score(score, table)
    return SupportPeriodEstimate(
        package_name=package_name,
        ecosystem=ecosystem,
        score=score,
        risk_level=risk_level,
        horizon_months=horizon,
        cra_minimum_supportable=horizon >= CRA_MINIMUM_SUPPORT_MONTHS,
        reason=reason,
    )


def compute_structural_importance(
    score: int,
    contributors: int,
    concentration: float,
    commits_last_year: int,
    lifetime_commits: int,
    n_dependents: int,
) -> float:
    """Combine governance fragility, code complexity, and tree position into one score.

    Mirrors the formula used by ``ossuary xkcd-tree --tower`` to highlight
    the most structurally critical dependency. Returns 0 for components with
    no measured dependents (importance cannot be derived from tree position).
    """
    if score < 0 or n_dependents == 0:
        return 0.0
    contribs = max(contributors, 1)
    fragility = (concentration / 100.0) / math.sqrt(contribs)
    # Low recent activity amplifies fragility — the project is closer to abandoned.
    if commits_last_year <= 1:
        fragility = min(1.0, fragility * 1.5)
    elif commits_last_year <= 5:
        fragility = min(1.0, fragility * 1.2)
    # Irreplaceability: accumulated code complexity, log-scaled.
    irreplaceability = min(1.0, math.log2(max(lifetime_commits, 10)) / 12)
    tree_impact = 1 + n_dependents
    return fragility * irreplaceability * tree_impact * 100


def derive_product_support_period(
    component_scores: list[dict],
    dependents_count: Optional[dict[str, int]] = None,
    critical_top_n: int = DEFAULT_CRITICAL_TOP_N,
    table: Optional[list[tuple[int, int, str]]] = None,
) -> ProductSupportPeriod:
    """Compute the product-level implied support period from per-component scores.

    ``component_scores`` is a list of dicts; each dict must include at least
    ``name`` (str), ``ecosystem`` (str), ``score`` (int), ``risk_level``
    (str). Optional fields ``contributors`` (int), ``concentration`` (float),
    ``commits_last_year`` (int), ``lifetime_commits`` (int) enable structural
    importance ranking.

    ``dependents_count`` maps component name → number of components that
    depend on it. When provided and non-zero for at least one component,
    structural importance is used to pick the critical subset. Otherwise
    the critical subset is the top-N by raw score.

    The product horizon is the minimum horizon across the critical subset.
    Limiting components are those whose individual horizon equals that
    minimum.

    ``critical_top_n`` must be ≥ 1. A value of zero would otherwise produce
    an empty critical subset, which would silently fall back to the CRA
    floor and report the product as supportable regardless of dependency
    health — bypassing the analytic. ``ValueError`` is raised in that case.
    """
    if critical_top_n < 1:
        raise ValueError(
            f"critical_top_n must be >= 1 (got {critical_top_n}); "
            "a smaller value would coerce a 'CRA-supportable' verdict regardless of "
            "dependency health and is not allowed."
        )
    if not component_scores:
        return ProductSupportPeriod(
            horizon_months=CRA_MINIMUM_SUPPORT_MONTHS,
            cra_minimum_supportable=True,
            critical_top_n=critical_top_n,
            critical_selection_method="none",
            limiting_components=[],
            critical_components=[],
            components_total=0,
            components_scored=0,
        )

    estimates_by_name: dict[str, SupportPeriodEstimate] = {}
    importance_by_name: dict[str, float] = {}
    scored_count = 0

    for cs in component_scores:
        score = cs.get("score")
        if score is None or score < 0:
            continue
        scored_count += 1
        name = cs["name"]
        estimates_by_name[name] = estimate_for_score(
            package_name=name,
            ecosystem=cs.get("ecosystem", ""),
            score=score,
            risk_level=cs.get("risk_level", ""),
            table=table,
        )
        if dependents_count:
            importance_by_name[name] = compute_structural_importance(
                score=score,
                contributors=cs.get("contributors", 1),
                concentration=cs.get("concentration", 50.0),
                commits_last_year=cs.get("commits_last_year", 0),
                lifetime_commits=cs.get("lifetime_commits", 0),
                n_dependents=dependents_count.get(name, 0),
            )

    if importance_by_name and any(v > 0 for v in importance_by_name.values()):
        method = "structural_importance"
        ranked_names = sorted(
            importance_by_name,
            key=lambda n: (importance_by_name[n], estimates_by_name[n].score),
            reverse=True,
        )
    else:
        method = "worst_score"
        ranked_names = sorted(
            estimates_by_name,
            key=lambda n: estimates_by_name[n].score,
            reverse=True,
        )

    critical_names = ranked_names[:critical_top_n]
    critical = [estimates_by_name[n] for n in critical_names]

    if not critical:
        return ProductSupportPeriod(
            horizon_months=CRA_MINIMUM_SUPPORT_MONTHS,
            cra_minimum_supportable=True,
            critical_top_n=critical_top_n,
            critical_selection_method=method,
            limiting_components=[],
            critical_components=[],
            components_total=len(component_scores),
            components_scored=scored_count,
        )

    horizon = min(c.horizon_months for c in critical)
    limiting = [c for c in critical if c.horizon_months == horizon]

    return ProductSupportPeriod(
        horizon_months=horizon,
        cra_minimum_supportable=horizon >= CRA_MINIMUM_SUPPORT_MONTHS,
        critical_top_n=critical_top_n,
        critical_selection_method=method,
        limiting_components=limiting,
        critical_components=critical,
        components_total=len(component_scores),
        components_scored=scored_count,
    )


def parse_cyclonedx_dependents(raw: dict) -> dict[str, int]:
    """Build name → dependents-count from a CycloneDX dependencies block.

    CycloneDX models dependencies as ``dependencies: [{ref: ..., dependsOn: [...]}]``
    where refs match component bom-refs. Returns counts keyed by component
    *name* (resolved via bom-ref → name lookup). Missing relationships
    yield an empty dict.
    """
    components = raw.get("components", []) or []
    name_by_ref = {}
    for c in components:
        ref = c.get("bom-ref")
        name = c.get("name")
        if ref and name:
            name_by_ref[ref] = name

    counts: dict[str, int] = {n: 0 for n in name_by_ref.values()}
    for dep in raw.get("dependencies", []) or []:
        for child_ref in dep.get("dependsOn", []) or []:
            child_name = name_by_ref.get(child_ref)
            if child_name:
                counts[child_name] = counts.get(child_name, 0) + 1
    return counts


def parse_spdx_dependents(raw: dict) -> dict[str, int]:
    """Build name → dependents-count from SPDX relationships.

    SPDX uses ``relationships: [{spdxElementId, relatedSpdxElement, relationshipType}]``.
    Counts DEPENDS_ON edges (and the inverse DEPENDENCY_OF) toward the dependent side.
    """
    packages = raw.get("packages", []) or []
    name_by_id = {}
    for p in packages:
        spdx_id = p.get("SPDXID")
        name = p.get("name")
        if spdx_id and name:
            name_by_id[spdx_id] = name

    counts: dict[str, int] = {n: 0 for n in name_by_id.values()}
    for rel in raw.get("relationships", []) or []:
        rtype = rel.get("relationshipType")
        if rtype == "DEPENDS_ON":
            child_name = name_by_id.get(rel.get("relatedSpdxElement"))
        elif rtype == "DEPENDENCY_OF":
            child_name = name_by_id.get(rel.get("spdxElementId"))
        else:
            continue
        if child_name:
            counts[child_name] = counts.get(child_name, 0) + 1
    return counts
