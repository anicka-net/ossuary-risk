"""Guardrail tests against doc/code drift in the scoring methodology.

Background: a Gemini code review (April 2026) surfaced multiple instances
where ``docs/methodology.md`` claimed one number but the implementation
used another (takeover historical-share threshold 5% vs 10%; mature-project
activity penalty "never penalized" vs +20 for zero commits; TOP_PACKAGES
covering only npm+pypi while the doc implied broader coverage). Each
divergence quietly invalidates the published methodology against the
actual scoring behaviour, which is exactly the contract academic readers
rely on.

These tests do not validate that the methodology *itself* is correct —
they validate that the doc and the code are describing the same thing.
A failure means either the code changed without the doc being updated,
or the doc changed without the code being updated. Either way the
contract is broken; pick a side and align.

Approach: extract concrete values (constants, ecosystem coverage, version
strings) from the source code by import / AST, then search for them in
the rendered methodology text. The assertions deliberately use phrase
matching that points to the specific divergence on failure rather than
a brittle structural match.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ossuary.scoring import (
    FRUSTRATION_WEIGHT,
    METHODOLOGY_VERSION,
    PREDICTION_THRESHOLD,
    RISK_THRESHOLDS,
    SENTIMENT_IN_SCORE,
)
from ossuary.scoring.reputation import TOP_PACKAGES


REPO_ROOT = Path(__file__).resolve().parent.parent
METHODOLOGY = (REPO_ROOT / "docs" / "methodology.md").read_text(encoding="utf-8")
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
VALIDATION_DOC = (REPO_ROOT / "docs" / "validation.md").read_text(encoding="utf-8")
DASHBOARD_METHODOLOGY = (
    REPO_ROOT / "src" / "ossuary" / "dashboard" / "pages" / "4_Methodology.py"
).read_text(encoding="utf-8")


# --- Methodology version --------------------------------------------------

def test_methodology_version_in_doc_matches_code():
    """``METHODOLOGY_VERSION`` constant must equal the ``**Version**:`` line.

    Annex VII output declares which methodology produced the score by
    referencing this version. Drift here means the audit record points
    at the wrong methodology document.
    """
    match = re.search(r"\*\*Version\*\*\s*:\s*(\d+\.\d+)", METHODOLOGY)
    assert match, "no '**Version**: X.Y' line found in methodology.md"
    assert match.group(1) == METHODOLOGY_VERSION, (
        f"methodology.md declares version {match.group(1)} but "
        f"METHODOLOGY_VERSION constant is {METHODOLOGY_VERSION}. "
        f"Either bump the doc or revert the constant."
    )


# --- Takeover historical-share threshold ---------------------------------

def test_takeover_historical_share_threshold_documented():
    """Code in ``collectors/git.py`` filters takeover suspects with hist
    share >= a threshold; the doc must cite the same percentage."""
    git_source = (REPO_ROOT / "src" / "ossuary" / "collectors" / "git.py").read_text(
        encoding="utf-8"
    )
    # The takeover-suspect filter is a line of the shape:
    #   'if hist_pct >= N or merged_hist_pct >= N: continue'
    # The 'domain_hist_pct' check elsewhere is the org-continuity guard
    # — a separate concept — and is intentionally excluded here.
    pattern = re.compile(
        r"\bhist_pct\s*>=\s*(\d+)\s+or\s+merged_hist_pct\s*>=\s*(\d+)\b"
    )
    match = pattern.search(git_source)
    assert match, (
        "could not find the takeover-suspect filter "
        "('hist_pct >= N or merged_hist_pct >= N') in collectors/git.py. "
        "If it was refactored, update this guardrail."
    )
    code_thresholds = {match.group(1), match.group(2)}
    assert len(code_thresholds) == 1, (
        f"the two halves of the historical-share filter use different "
        f"thresholds {code_thresholds}; that's almost certainly a typo."
    )
    threshold = code_thresholds.pop()
    needle = f"<{threshold}% of historical commits"
    assert needle in METHODOLOGY, (
        f"code uses historical-share threshold of {threshold}% (in "
        f"`hist_pct >= {threshold}`), but methodology.md does not contain "
        f"the phrase '{needle}'. Update doc §4.4."
    )


# --- Mature-project zero-activity penalty --------------------------------

def test_mature_project_zero_activity_branch_documented():
    """``engine.py`` has an explicit branch applying the full +20 abandonment
    penalty to mature projects with zero commits in the last year. The doc
    must acknowledge that branch — the older 'never penalized' wording is
    flatly wrong."""
    engine_source = (REPO_ROOT / "src" / "ossuary" / "scoring" / "engine.py").read_text(
        encoding="utf-8"
    )
    # The branch is marked by 'commits_last_year == 0' inside the is_mature path.
    assert "commits_last_year == 0" in engine_source, (
        "expected an explicit zero-activity branch for mature projects in "
        "engine.py; if removed, this guardrail and methodology §4.2 need updating."
    )
    # The doc must NOT claim mature projects are unconditionally exempt from the
    # abandonment penalty. The phrase 'never penalized' was the broken wording.
    bad_phrase = "never penalized"
    if bad_phrase in METHODOLOGY:
        # Only flag if the phrase is in the §4.2 / mature-project context.
        # Allow appearances inside cautionary notes, bibliography, etc.
        section = _section_text(METHODOLOGY, "### 4.2 Activity Modifier")
        assert bad_phrase not in section, (
            "methodology.md §4.2 still contains 'never penalized' wording, but "
            "engine.py applies +20 to mature projects with zero commits/year. "
            "Update the doc to describe the three-way split."
        )
    # Positive check: the doc must describe the three-way split somewhere.
    section = _section_text(METHODOLOGY, "### 4.2 Activity Modifier")
    assert "+20" in section and "abandoned" in section.lower(), (
        "methodology.md §4.2 must describe the +20 zero-activity penalty for "
        "truly abandoned mature projects."
    )


# --- TOP_PACKAGES ecosystem coverage -------------------------------------

def test_top_packages_ecosystem_coverage_documented():
    """The doc previously claimed 'top-1000 ecosystem package' which implied
    full coverage. The actual list is curated, ecosystem-specific, and may
    grow. The doc must enumerate which ecosystems are currently covered.
    """
    code_ecosystems = set(TOP_PACKAGES.keys())
    section = _section_text(METHODOLOGY, "### 5.1 Reputation Signals")
    # Allow display-friendly capitalisation in the doc (PyPI vs pypi, etc.).
    section_normalised = section.lower()
    missing = [eco for eco in code_ecosystems if eco.lower() not in section_normalised]
    assert not missing, (
        f"methodology.md §5.1 / 5.2 does not enumerate these ecosystems "
        f"covered by TOP_PACKAGES: {missing}. Either expand the doc or "
        f"remove them from TOP_PACKAGES."
    )


def test_top_packages_lists_are_non_empty():
    """A typo or accidental clear of an ecosystem entry would silently
    remove the +15 top-package bonus for every maintainer in that
    ecosystem. Cheap to assert here."""
    for eco, packages in TOP_PACKAGES.items():
        assert packages, f"TOP_PACKAGES['{eco}'] is empty"
        assert all(p == p.lower() for p in packages), (
            f"TOP_PACKAGES['{eco}'] contains non-lowercase entries; the "
            "lookup uses .lower() and would silently miss them."
        )


# --- Frustration weight ---------------------------------------------------

def test_frustration_weight_in_dashboard_matches_code():
    """Dashboard methodology page must show the same frustration points
    that ``FRUSTRATION_WEIGHT`` reports. Drift here was the v6.3 case the
    GPT review caught — code lowered to +15, dashboard still showed +20."""
    expected = f'"+{FRUSTRATION_WEIGHT}"'
    assert expected in DASHBOARD_METHODOLOGY, (
        f"dashboard methodology page must include {expected} in the "
        f"protective-factors Points list, but it does not. "
        f"FRUSTRATION_WEIGHT is {FRUSTRATION_WEIGHT}."
    )
    # Negative check: the prior weight must not be present in the same
    # protective-factors row. The "Points" array is the row to check.
    bad = '"+20"'
    if bad in DASHBOARD_METHODOLOGY:
        # Allow +20 elsewhere (takeover_risk uses +20 too); enforce that
        # the frustration row's slot is the v6.3 value. Cheapest reliable
        # form: the row that lists frustration must have the right weight.
        section = DASHBOARD_METHODOLOGY[
            DASHBOARD_METHODOLOGY.find('"Frustration signals detected"'):
        ]
        section = section[: section.find("st.caption")]
        assert expected in section, (
            "dashboard 'Frustration signals detected' row must show "
            f"{expected}; found a stale +20 in this region."
        )


def test_frustration_weight_documented_in_methodology():
    """methodology.md must cite the +FRUSTRATION_WEIGHT value as the active
    weight, not the older +20."""
    needle = f"+{FRUSTRATION_WEIGHT} in v"
    assert needle in METHODOLOGY, (
        f"methodology.md must mention '{needle}' to declare the active "
        f"frustration weight. Either update §6.4.1 or revert "
        f"FRUSTRATION_WEIGHT (currently {FRUSTRATION_WEIGHT})."
    )


# --- Sentiment branch removal --------------------------------------------

def test_sentiment_in_score_documented_consistently():
    """When SENTIMENT_IN_SCORE is False, methodology §6 must reflect that
    the VADER scoring branch was removed. When True, the doc must not say
    'removed from formula'."""
    removed_phrases = [
        "removed from formula",
        "structurally always 0",
        "no longer contributes",
    ]
    has_removed_marker = any(p in METHODOLOGY for p in removed_phrases)
    if not SENTIMENT_IN_SCORE:
        assert has_removed_marker, (
            "SENTIMENT_IN_SCORE is False but methodology.md does not say "
            "the VADER scoring branch was removed (looked for any of "
            f"{removed_phrases}). Update §6 / §6.3."
        )
    else:
        assert not has_removed_marker, (
            "SENTIMENT_IN_SCORE is True but methodology.md still says the "
            "branch was removed. Either re-enable in code or update doc."
        )


# --- Risk bucket boundaries ----------------------------------------------

def test_risk_bucket_boundaries_match_dashboard():
    """Dashboard risk-levels table must match RISK_THRESHOLDS lower bounds.
    The previous table used closed boundaries that overlapped with the
    next bucket (0–20, 21–40, ...) which contradicted the >= boundaries
    in code."""
    # Build the expected bucket strings: e.g. "0–19", "20–39", "40–59",
    # "60–79", "80–100" — non-overlapping, matching the >= boundaries.
    sorted_thresholds = sorted(RISK_THRESHOLDS)  # ascending by min_score
    upper_bounds = [t for t, _ in sorted_thresholds[1:]] + [101]
    expected_ranges = []
    for (low, _), high in zip(sorted_thresholds, upper_bounds):
        expected_ranges.append(f"{low}–{high - 1}" if high <= 100 else f"{low}–100")

    for needle in expected_ranges:
        assert needle in DASHBOARD_METHODOLOGY, (
            f"dashboard methodology risk-levels table must include the "
            f"non-overlapping range '{needle}'. RISK_THRESHOLDS is "
            f"{RISK_THRESHOLDS}."
        )


# --- Validation artifact contract pinning --------------------------------

def _load_validation_artifact():
    """Load validation_results.json or skip if missing.

    The artifact is regenerated by ``scripts/validate.py``; we don't
    require CI to re-run validation, but if the file exists, its
    declared metrics must agree with the public docs."""
    import json
    path = REPO_ROOT / "validation_results.json"
    if not path.exists():
        pytest.skip("validation_results.json not present; skip drift check.")
    return json.loads(path.read_text(encoding="utf-8"))


def test_validation_artifact_methodology_version_matches_code():
    """The methodology version stamped into the artifact must equal the
    code constant. Drift here means either the artifact is stale or the
    constant moved without re-running validation."""
    data = _load_validation_artifact()
    methodology = data.get("methodology", {})
    if not methodology:
        pytest.skip("legacy artifact (no 'methodology' block); skip.")
    assert methodology.get("version") == METHODOLOGY_VERSION, (
        f"validation_results.json declares methodology version "
        f"{methodology.get('version')!r} but METHODOLOGY_VERSION is "
        f"{METHODOLOGY_VERSION!r}. Re-run scripts/validate.py."
    )
    assert methodology.get("frustration_weight") == FRUSTRATION_WEIGHT
    assert methodology.get("sentiment_in_score") == SENTIMENT_IN_SCORE
    assert methodology.get("prediction_threshold") == PREDICTION_THRESHOLD


def test_validation_dataset_size_matches_public_docs():
    """README, methodology.md, and validation.md must agree with the
    artifact on the headline sample size. The GPT review caught three
    different values (164/167/170) live across these surfaces."""
    data = _load_validation_artifact()
    n = data.get("dataset", {}).get("total_cases")
    if not n:
        pytest.skip("legacy artifact (no 'dataset' block); skip.")
    needle = f"{n} packages"
    for doc_name, doc in (
        ("README.md", README),
        ("docs/methodology.md", METHODOLOGY),
        ("docs/validation.md", VALIDATION_DOC),
    ):
        assert needle in doc, (
            f"{doc_name} must mention '{needle}' (the validation set "
            f"size from validation_results.json). Either re-run "
            f"scripts/validate.py or update the doc."
        )


def test_validation_scope_b_metrics_match_public_docs():
    """Scope B precision / recall / F1 in README, methodology.md, and
    validation.md must agree with what scripts/validate.py produced.

    Phrase-matching uses one-decimal-place rounding, which matches the
    rendered table style ("96.0%", "75.0%", "0.842") and is the cheapest
    reliable form. If the doc table ever switches to two decimals this
    test must be revisited."""
    data = _load_validation_artifact()
    scope_b = data.get("scopes", {}).get("scope_b")
    if not scope_b:
        pytest.skip("legacy artifact (no 'scopes.scope_b' block); skip.")

    prec = round(scope_b["precision"] * 100, 1)
    rec = round(scope_b["recall"] * 100, 1)
    f1 = round(scope_b["f1"], 3)
    needles = [f"{prec:.1f}%", f"{rec:.1f}%", f"{f1:.3f}"]

    for doc_name, doc in (
        ("README.md", README),
        ("docs/methodology.md", METHODOLOGY),
        ("docs/validation.md", VALIDATION_DOC),
    ):
        for needle in needles:
            assert needle in doc, (
                f"{doc_name} must include Scope B metric '{needle}' "
                f"(from validation_results.json). Re-run validate.py "
                f"and update the doc."
            )


# --- helpers --------------------------------------------------------------

def _section_text(doc: str, heading: str) -> str:
    """Return the text of a Markdown section starting at ``heading`` and
    ending at the next sibling-or-higher heading. Used so each assertion
    points at a specific section of the doc, not the whole file."""
    start = doc.find(heading)
    if start == -1:
        pytest.fail(f"section '{heading}' not found in methodology.md")
    rest = doc[start + len(heading):]
    # Stop at the next heading of the same depth or shallower (## or ###).
    depth = heading.count("#")
    pattern = re.compile(rf"^#{{1,{depth}}} ", re.MULTILINE)
    match = pattern.search(rest)
    end = match.start() if match else len(rest)
    return rest[:end]
