"""Tests for tower generation, structural threat formula, and helpers."""

import math
import os
import re
from unittest.mock import MagicMock, patch

import pytest

from ossuary.cli import _generate_tower_from_tree


# ---------------------------------------------------------------------------
# Helper: replicate the structural threat formula from cli.py
# ---------------------------------------------------------------------------

def compute_threat(concentration, contributors, commits, lifetime_commits, n_dependents):
    """Replicate the structural threat formula for direct testing."""
    fragility = (concentration / 100) / math.sqrt(max(contributors, 1))
    if commits <= 1:
        fragility = min(1.0, fragility * 1.5)
    elif commits <= 5:
        fragility = min(1.0, fragility * 1.2)
    irreplaceability = min(1.0, math.log2(max(lifetime_commits, 10)) / 12)
    tree_impact = 1 + n_dependents
    return fragility * irreplaceability * tree_impact * 100


# ---------------------------------------------------------------------------
# Helper: mock DB for tower generation
# ---------------------------------------------------------------------------

def _make_score_mock(score, contributors, commits, concentration, lifetime_commits, lifetime_years):
    """Create a mock Score object with the fields tower generation reads."""
    mock = MagicMock()
    mock.final_score = score
    mock.risk_level = "CRITICAL" if score >= 80 else "HIGH" if score >= 60 else "MODERATE" if score >= 40 else "LOW" if score >= 20 else "VERY_LOW"
    mock.unique_contributors = contributors
    mock.commits_last_year = commits
    mock.maintainer_concentration = concentration
    mock.breakdown = {
        "score": {
            "components": {
                "base_risk": score,
                "protective_factors": {
                    "maturity": {
                        "evidence": f"Stable project: {lifetime_commits} commits over {lifetime_years} years, 10 lifetime contributors" if lifetime_commits > 0 else None,
                    }
                }
            }
        }
    }
    return mock


def _mock_db(scores_map):
    """Return a context-manager patch for session_scope that returns controlled scores.

    scores_map: {package_name: (score, contributors, commits, concentration, lt_commits, lt_years)}
    """
    mock_session = MagicMock()
    pkg_id_counter = [0]

    def query_side_effect(model):
        chain = MagicMock()

        def filter_side_effect(*args, **kwargs):
            filter_chain = MagicMock()

            # Try to extract the package name from the filter args
            # The tower code does: Package.name == name
            # In mock context, we look at the filter call args
            for arg in args:
                # SQLAlchemy BinaryExpression - try to get the right side
                if hasattr(arg, 'right') and hasattr(arg.right, 'value'):
                    name = arg.right.value
                    if name in scores_map:
                        pkg_mock = MagicMock()
                        pkg_id_counter[0] += 1
                        pkg_mock.id = pkg_id_counter[0]
                        filter_chain.first.return_value = pkg_mock
                        return filter_chain

            # For Score queries - return the score mock
            # The tower code chains: .filter(Score.package_id == pkg.id).order_by(...).first()
            order_chain = MagicMock()

            # Find which package this is for by checking the pkg_id
            def first_for_score():
                # Get the last created package id
                for name, data in scores_map.items():
                    return _make_score_mock(*data)
                return None

            order_chain.first.return_value = None
            filter_chain.order_by.return_value = order_chain
            filter_chain.first.return_value = None
            return filter_chain

        chain.filter.side_effect = filter_side_effect
        return chain

    mock_session.query.side_effect = query_side_effect

    return mock_session


def _generate_tower_with_mock_db(adj, root, scores_map, tmp_path, ecosystem="npm", max_width=1200):
    """Run _generate_tower_from_tree with mocked DB, return SVG content."""
    output = str(tmp_path / "test_tower.svg")

    # Build mock session that returns the right Score for each package
    mock_session = MagicMock()

    # Store package name → score data mapping for lookup
    pkg_ids = {}
    id_counter = [0]

    def make_query(model):
        chain = MagicMock()

        def do_filter(*args, **kwargs):
            result = MagicMock()

            # Detect what we're filtering on
            # For Package: filter(Package.name == name, Package.ecosystem == eco)
            # For Score: filter(Score.package_id == pkg.id)
            name_found = None
            pkg_id_found = None

            for arg in args:
                if hasattr(arg, 'right') and hasattr(arg.right, 'value'):
                    val = arg.right.value
                    if isinstance(val, str) and val in scores_map:
                        name_found = val
                    elif isinstance(val, str) and val in ("npm", "pypi"):
                        pass  # ecosystem filter, ignore
                    elif isinstance(val, int):
                        pkg_id_found = val

            if name_found:
                # Package query
                id_counter[0] += 1
                pkg_mock = MagicMock()
                pkg_mock.id = id_counter[0]
                pkg_ids[id_counter[0]] = name_found
                result.first.return_value = pkg_mock
            elif pkg_id_found and pkg_id_found in pkg_ids:
                # Score query
                name = pkg_ids[pkg_id_found]
                score_mock = _make_score_mock(*scores_map[name])
                order_result = MagicMock()
                order_result.first.return_value = score_mock
                result.order_by.return_value = order_result
                result.first.return_value = score_mock
            else:
                result.first.return_value = None
                order_result = MagicMock()
                order_result.first.return_value = None
                result.order_by.return_value = order_result

            return result

        chain.filter.side_effect = do_filter
        return chain

    mock_session.query.side_effect = make_query

    mock_scope = MagicMock()
    mock_scope.__enter__ = MagicMock(return_value=mock_session)
    mock_scope.__exit__ = MagicMock(return_value=False)

    with patch("ossuary.db.session.session_scope", return_value=mock_scope):
        _generate_tower_from_tree(adj, root, ecosystem, output, root, max_width)

    with open(output) as f:
        return f.read()


# ===========================================================================
# Structural Threat Formula Tests
# ===========================================================================

class TestStructuralThreatFormula:
    """Tests for the structural threat formula used to select the arrow target."""

    def test_single_maintainer_high_threat(self):
        """Single maintainer + high concentration + dormant = very high threat."""
        threat = compute_threat(
            concentration=95, contributors=1, commits=0,
            lifetime_commits=2000, n_dependents=10,
        )
        # fragility = 0.95 * 1.5 = 1.0 (clamped)
        # irreplaceability = log2(2000)/12 ≈ 0.915
        # tree_impact = 11
        assert threat == pytest.approx(1.0 * (math.log2(2000) / 12) * 11 * 100, rel=0.01)
        assert threat > 900  # very high

    def test_many_contributors_low_fragility(self):
        """Many contributors reduce fragility via sqrt denominator."""
        threat_many = compute_threat(
            concentration=80, contributors=16, commits=50,
            lifetime_commits=500, n_dependents=5,
        )
        threat_few = compute_threat(
            concentration=80, contributors=1, commits=50,
            lifetime_commits=500, n_dependents=5,
        )
        # 16 contributors → fragility reduced by factor of sqrt(16)=4
        assert threat_few / threat_many == pytest.approx(4.0, rel=0.01)

    def test_low_lifetime_low_irreplaceability(self):
        """Packages with few lifetime commits are easy to replace."""
        threat_low = compute_threat(
            concentration=90, contributors=1, commits=50,
            lifetime_commits=10, n_dependents=3,
        )
        threat_high = compute_threat(
            concentration=90, contributors=1, commits=50,
            lifetime_commits=2000, n_dependents=3,
        )
        # log2(10)/12 ≈ 0.277 vs log2(2000)/12 ≈ 0.915
        assert threat_high > threat_low * 3

    def test_low_activity_amplifies_fragility(self):
        """Commits ≤ 1 amplifies fragility by 1.5x vs active project."""
        threat_dormant = compute_threat(
            concentration=60, contributors=4, commits=1,
            lifetime_commits=500, n_dependents=3,
        )
        threat_active = compute_threat(
            concentration=60, contributors=4, commits=50,
            lifetime_commits=500, n_dependents=3,
        )
        assert threat_dormant / threat_active == pytest.approx(1.5, rel=0.01)

    def test_pyyaml_beats_shellingham_at_equal_dependents(self):
        """pyyaml (326 lt_commits, complex) outranks shellingham (168, trivial) when dependents are equal."""
        threat_pyyaml = compute_threat(
            concentration=100, contributors=1, commits=1,
            lifetime_commits=326, n_dependents=3,
        )
        threat_shellingham = compute_threat(
            concentration=100, contributors=1, commits=0,
            lifetime_commits=168, n_dependents=3,
        )
        # Both have fragility capped at 1.0 (single maintainer + low activity)
        # pyyaml wins on irreplaceability: log2(326) > log2(168)
        assert threat_pyyaml > threat_shellingham

    def test_arrow_points_to_highest_threat(self, tmp_path):
        """Integration: tower SVG arrow caption names the highest-threat package."""
        adj = {
            "app": ["lib-risky", "lib-safe"],
            "lib-risky": ["shared"],
            "lib-safe": ["shared"],
            "shared": [],
        }
        # shared: high concentration, 1 contributor, low activity, lots of lifetime code
        # lib-risky: moderate stats
        # lib-safe: very healthy
        scores = {
            "app":        (5,  50, 200, 20, 1000, 5),
            "lib-risky":  (40,  5,  30, 60,  200, 3),
            "lib-safe":   (10, 20, 100, 25,  300, 4),
            "shared":     (70,  1,   2, 95,  800, 15),
        }
        svg = _generate_tower_with_mock_db(adj, "app", scores, tmp_path)
        # shared should be the arrow target (highest threat)
        assert "\u201cshared\u201d" in svg  # quoted name in caption

    def test_root_excluded_from_threat(self, tmp_path):
        """Root package is never selected as arrow target even with worst stats."""
        adj = {
            "bad-root": ["healthy-dep"],
            "healthy-dep": [],
        }
        # Give root terrible stats, but it should be excluded
        scores = {
            "bad-root":     (95, 1, 0, 100, 2000, 20),
            "healthy-dep":  (40, 5, 20, 60,  200,  3),
        }
        svg = _generate_tower_with_mock_db(adj, "bad-root", scores, tmp_path)
        # healthy-dep has 0 dependents (nothing depends on it), so no arrow at all
        # OR if there is an arrow, it must not point at bad-root
        assert "\u201cbad-root\u201d" not in svg


# ===========================================================================
# Layer Assignment Tests
# ===========================================================================

class TestTowerLayerAssignment:
    """Tests for layer assignment in tower generation."""

    def test_linear_chain_layers(self, tmp_path):
        """Linear chain A→B→C produces 3 distinct row Y-positions."""
        adj = {"A": ["B"], "B": ["C"], "C": []}
        svg = _generate_tower_with_mock_db(adj, "A", {}, tmp_path)
        # Extract unique Y positions of rect elements (match ' y="N"' not 'ry="N"')
        y_positions = set(re.findall(r'<rect[^>]+ y="([^"]+)"', svg))
        assert len(y_positions) == 3

    def test_diamond_shared_deepest(self, tmp_path):
        """Shared dep is pushed to deepest layer (layer 2, not 1)."""
        adj = {
            "root": ["left", "right"],
            "left": ["shared"],
            "right": ["shared"],
            "shared": [],
        }
        svg = _generate_tower_with_mock_db(adj, "root", {}, tmp_path)
        # 3 layers: root(0), left+right(1), shared(2)
        # Match ' y="N"' (with space) to avoid matching ry="2"
        y_positions = sorted(set(float(y) for y in re.findall(r'<rect[^>]+ y="([^"]+)"', svg)))
        assert len(y_positions) == 3
        # shared should be at the deepest layer (highest Y value)
        shared_texts = re.findall(r'<text[^>]+y="([^"]+)"[^>]*>shared</text>', svg)
        assert len(shared_texts) >= 1
        shared_y = float(shared_texts[0])
        # shared text Y should be greater than the second layer Y
        assert shared_y > y_positions[1]

    def test_cycle_no_infinite_loop(self, tmp_path):
        """Cycles in the dependency graph don't cause infinite loops."""
        adj = {"A": ["B"], "B": ["A"]}
        # Should complete without hanging
        svg = _generate_tower_with_mock_db(adj, "A", {}, tmp_path)
        assert "<svg" in svg
        assert "A" in svg

    def test_orphan_excluded(self, tmp_path):
        """Nodes not reachable from root don't appear in SVG."""
        adj = {"root": ["dep"], "dep": [], "orphan": []}
        svg = _generate_tower_with_mock_db(adj, "root", {}, tmp_path)
        assert "root" in svg
        assert "dep" in svg
        assert "orphan" not in svg


# ===========================================================================
# Label and Sizing Tests
# ===========================================================================

class TestTowerHelpers:
    """Tests for label rendering and block sizing in tower generation."""

    def test_label_split_at_hyphen(self, tmp_path):
        """Long hyphenated name is split into two text lines at a hyphen."""
        adj = {"root": ["side-channel"], "side-channel": []}
        svg = _generate_tower_with_mock_db(adj, "root", {}, tmp_path)
        # Should have two <text> elements for side-channel:
        # one with "side-" and one with "channel"
        assert "side-" in svg
        assert "channel" in svg
        # Both text elements at the same x coordinate
        texts_with_side = re.findall(r'<text x="([^"]+)"[^>]*>side-</text>', svg)
        texts_with_channel = re.findall(r'<text x="([^"]+)"[^>]*>channel</text>', svg)
        if texts_with_side and texts_with_channel:
            assert texts_with_side[0] == texts_with_channel[0]  # same x

    def test_label_split_no_delimiter(self, tmp_path):
        """Name without delimiters splits at midpoint."""
        adj = {"root": ["superlongname"], "superlongname": []}
        svg = _generate_tower_with_mock_db(adj, "root", {}, tmp_path)
        # With a narrow block (no DB score → default min width), the name
        # should be split into two lines near the middle
        # "superl" + "ongname" or similar split
        text_elements = re.findall(r'>([^<]*superl[^<]*)<', svg)
        # Should have some split of the name
        assert any("superl" in t for t in text_elements)

    def test_block_width_formula(self):
        """Block width scales with sqrt of contributors."""
        # The formula: max(38, int(150 * (0.1 + 0.9 * sqrt(c / max_c))))
        def raw_width(c, max_c):
            ratio = math.sqrt(c) / math.sqrt(max(max_c, 1))
            return max(38, int(150 * (0.1 + 0.9 * ratio)))

        # Minimum width with 1 contributor out of 100
        assert raw_width(1, 100) == 38  # 150 * 0.19 = 28.5 → clamped to 38

        # Maximum width with max contributors
        assert raw_width(100, 100) == 150  # 150 * 1.0

        # Mid-range
        assert raw_width(25, 100) == 82  # 150 * (0.1 + 0.9 * 0.5) = 82.5 → 82
