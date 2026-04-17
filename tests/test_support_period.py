"""Tests for the implied support period derivation."""

import pytest

from ossuary.services.support_period import (
    CRA_MINIMUM_SUPPORT_MONTHS,
    compute_structural_importance,
    derive_product_support_period,
    estimate_for_score,
    horizon_from_score,
    parse_cyclonedx_dependents,
    parse_spdx_dependents,
)


class TestHorizonFromScore:
    @pytest.mark.parametrize("score,expected_months", [
        (0, 60), (10, 60), (19, 60),
        (20, 60), (30, 60), (39, 60),
        (40, 36), (50, 36), (59, 36),
        (60, 18), (70, 18), (79, 18),
        (80, 6), (90, 6), (100, 6),
    ])
    def test_table_boundaries(self, score, expected_months):
        months, reason = horizon_from_score(score)
        assert months == expected_months
        assert reason  # non-empty justification

    def test_out_of_range_score_returns_zero(self):
        months, reason = horizon_from_score(999)
        assert months == 0
        assert "out of range" in reason


class TestEstimateForScore:
    def test_critical_is_not_cra_minimum_supportable(self):
        est = estimate_for_score("xz-utils", "github", 80, "CRITICAL")
        assert est.horizon_months == 6
        assert est.cra_minimum_supportable is False

    def test_low_risk_meets_cra_minimum(self):
        est = estimate_for_score("requests", "pypi", 25, "LOW")
        assert est.horizon_months >= CRA_MINIMUM_SUPPORT_MONTHS
        assert est.cra_minimum_supportable is True


class TestStructuralImportance:
    def test_no_dependents_returns_zero(self):
        assert compute_structural_importance(80, 1, 100, 0, 1000, 0) == 0.0

    def test_unscored_returns_zero(self):
        assert compute_structural_importance(-1, 1, 100, 0, 1000, 5) == 0.0

    def test_high_concentration_low_contributors_high_dependents(self):
        # Worst-case: one maintainer, no recent activity, many dependents.
        threat = compute_structural_importance(
            score=80, contributors=1, concentration=100,
            commits_last_year=0, lifetime_commits=1000, n_dependents=20,
        )
        # vs. a healthier component with same dependents
        baseline = compute_structural_importance(
            score=20, contributors=20, concentration=20,
            commits_last_year=200, lifetime_commits=1000, n_dependents=20,
        )
        assert threat > baseline


class TestDeriveProductSupportPeriod:
    def _component(self, name, score, level, **extra):
        base = {"name": name, "ecosystem": "npm", "score": score, "risk_level": level}
        base.update(extra)
        return base

    def test_empty_components_defaults_to_cra_floor(self):
        result = derive_product_support_period([])
        assert result.horizon_months == CRA_MINIMUM_SUPPORT_MONTHS
        assert result.cra_minimum_supportable is True
        assert result.critical_components == []

    def test_all_low_risk_meets_cra_minimum(self):
        components = [
            self._component("a", 10, "VERY_LOW"),
            self._component("b", 25, "LOW"),
        ]
        result = derive_product_support_period(components)
        assert result.cra_minimum_supportable is True
        assert result.horizon_months >= CRA_MINIMUM_SUPPORT_MONTHS

    def test_one_critical_component_drives_product_horizon_down(self):
        components = [
            self._component("safe-1", 10, "VERY_LOW"),
            self._component("safe-2", 15, "VERY_LOW"),
            self._component("xz-utils", 80, "CRITICAL"),
        ]
        result = derive_product_support_period(components)
        # No dep counts → ranking by score → xz-utils is in critical subset.
        assert result.horizon_months == 6
        assert result.cra_minimum_supportable is False
        names = [c.package_name for c in result.limiting_components]
        assert "xz-utils" in names
        assert result.critical_selection_method == "worst_score"

    def test_critical_top_n_limits_subset(self):
        components = [
            self._component(f"pkg-{i}", 80, "CRITICAL") for i in range(10)
        ]
        result = derive_product_support_period(components, critical_top_n=3)
        assert len(result.critical_components) == 3
        assert result.horizon_months == 6
        # All ten are critical, so the 3 chosen will all be limiting.
        assert len(result.limiting_components) == 3

    def test_structural_importance_used_when_dependents_provided(self):
        # The critical rank should pick the dep with most dependents
        # even if its score is lower than another orphan dep.
        components = [
            self._component(
                "load-bearing", 60, "HIGH",
                contributors=1, concentration=95, commits_last_year=0,
                lifetime_commits=2000,
            ),
            self._component(
                "orphan-critical", 90, "CRITICAL",
                contributors=3, concentration=70, commits_last_year=20,
                lifetime_commits=300,
            ),
            self._component(
                "filler", 10, "VERY_LOW",
                contributors=20, concentration=20, commits_last_year=100,
                lifetime_commits=500,
            ),
        ]
        # load-bearing has 15 dependents, orphan-critical has 0.
        result = derive_product_support_period(
            components,
            dependents_count={"load-bearing": 15, "orphan-critical": 0, "filler": 5},
            critical_top_n=2,
        )
        assert result.critical_selection_method == "structural_importance"
        critical_names = [c.package_name for c in result.critical_components]
        # load-bearing dominates structural importance and should be included.
        assert "load-bearing" in critical_names
        # The 90-score orphan with 0 dependents has importance 0; ranked by score as fallback.
        # load-bearing has horizon 18 (HIGH), so it limits the product.
        assert result.horizon_months == 18

    def test_skips_unscored_components(self):
        components = [
            self._component("scored", 80, "CRITICAL"),
            self._component("unscored", -1, ""),
        ]
        result = derive_product_support_period(components)
        assert result.components_total == 2
        assert result.components_scored == 1
        # Critical subset only contains the scored one.
        assert all(c.package_name == "scored" for c in result.critical_components)

    def test_critical_top_n_zero_raises(self):
        # Regression: critical_top_n=0 used to silently return the CRA floor
        # ("supportable") regardless of how risky the dependencies were.
        components = [self._component("xz-utils", 80, "CRITICAL")]
        with pytest.raises(ValueError, match="critical_top_n must be >= 1"):
            derive_product_support_period(components, critical_top_n=0)

    def test_critical_top_n_negative_raises(self):
        components = [self._component("xz-utils", 80, "CRITICAL")]
        with pytest.raises(ValueError, match="critical_top_n must be >= 1"):
            derive_product_support_period(components, critical_top_n=-3)

    def test_critical_top_n_one_still_works(self):
        components = [
            self._component("xz-utils", 80, "CRITICAL"),
            self._component("safe", 10, "VERY_LOW"),
        ]
        result = derive_product_support_period(components, critical_top_n=1)
        assert len(result.critical_components) == 1
        assert result.critical_components[0].package_name == "xz-utils"
        assert result.cra_minimum_supportable is False


class TestCycloneDXDependentParsing:
    def test_counts_inbound_edges(self):
        raw = {
            "components": [
                {"bom-ref": "ref-a", "name": "a"},
                {"bom-ref": "ref-b", "name": "b"},
                {"bom-ref": "ref-c", "name": "c"},
            ],
            "dependencies": [
                {"ref": "ref-a", "dependsOn": ["ref-b", "ref-c"]},
                {"ref": "ref-b", "dependsOn": ["ref-c"]},
            ],
        }
        counts = parse_cyclonedx_dependents(raw)
        assert counts["a"] == 0
        assert counts["b"] == 1  # a depends on b
        assert counts["c"] == 2  # a and b depend on c

    def test_no_dependencies_block(self):
        raw = {"components": [{"bom-ref": "r", "name": "n"}]}
        counts = parse_cyclonedx_dependents(raw)
        assert counts == {"n": 0}


class TestSPDXDependentParsing:
    def test_depends_on_counts(self):
        raw = {
            "packages": [
                {"SPDXID": "SPDXRef-a", "name": "a"},
                {"SPDXID": "SPDXRef-b", "name": "b"},
                {"SPDXID": "SPDXRef-c", "name": "c"},
            ],
            "relationships": [
                {"spdxElementId": "SPDXRef-a", "relatedSpdxElement": "SPDXRef-b",
                 "relationshipType": "DEPENDS_ON"},
                {"spdxElementId": "SPDXRef-c", "relatedSpdxElement": "SPDXRef-b",
                 "relationshipType": "DEPENDENCY_OF"},
            ],
        }
        counts = parse_spdx_dependents(raw)
        # b is depended upon by a (DEPENDS_ON) → +1; c is DEPENDENCY_OF b → c depends on b → b +1
        # Wait: DEPENDENCY_OF semantics — element is a dependency OF the related element.
        # So "spdxElementId: c, relatedSpdxElement: b, DEPENDENCY_OF" means c is a dependency of b → b depends on c → c gets a dependent.
        assert counts["b"] == 1
        assert counts["c"] == 1
        assert counts["a"] == 0

    def test_irrelevant_relationship_types_ignored(self):
        raw = {
            "packages": [{"SPDXID": "SPDXRef-a", "name": "a"}],
            "relationships": [
                {"spdxElementId": "SPDXRef-a", "relatedSpdxElement": "SPDXRef-doc",
                 "relationshipType": "DESCRIBED_BY"},
            ],
        }
        counts = parse_spdx_dependents(raw)
        assert counts["a"] == 0
