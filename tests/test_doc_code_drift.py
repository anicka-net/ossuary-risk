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


# --- Frustration weight: negative check on active-doc sections ----------

def test_frustration_active_row_uses_current_weight():
    """The §4.3 Risk Increasers table is what defines the *active* scoring
    contribution for a factor. The Frustration row in that table must
    cite the current ``FRUSTRATION_WEIGHT``; it must not be left at the
    legacy +20. Historical values may appear in narrative prose
    (e.g. "lowered from +20 in v6.3") but not in the active row.
    """
    section = _section_text(
        METHODOLOGY, "#### Risk Increasers (Positive Points)"
    )
    frustration_rows = [
        line for line in section.splitlines()
        if line.lstrip().startswith("|") and "Frustration" in line
    ]
    assert frustration_rows, (
        "no Frustration row found in §4.3 Risk Increasers — table moved? "
        "If yes, update this guardrail."
    )
    expected = f"+{FRUSTRATION_WEIGHT}"
    for row in frustration_rows:
        assert expected in row, (
            f"§4.3 Risk Increasers Frustration row must cite "
            f"{expected}: {row!r}"
        )


def test_sentiment_not_listed_as_active_factor_when_disabled():
    """When ``SENTIMENT_IN_SCORE`` is False, §4.3 must not list a
    'Positive Sentiment' / 'Negative Sentiment' row as an active scoring
    contribution. Past wording cited -5 / +10; the rows must be removed
    or moved into v6.3-history prose."""
    if SENTIMENT_IN_SCORE:
        return
    section = _section_text(METHODOLOGY, "### 4.3 Protective Factors")
    forbidden_phrases = ("Positive Sentiment", "Negative Sentiment")
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue  # narrative prose / table separator, not an active row
        if line.lstrip().startswith("|---"):
            continue  # markdown table divider
        for phrase in forbidden_phrases:
            assert phrase not in line, (
                f"§4.3 protective factors table row still mentions "
                f"{phrase!r}: {line!r}. SENTIMENT_IN_SCORE is False; "
                f"remove the row or move it to v6.3-history prose."
            )


def test_analyzer_docstring_matches_active_methodology():
    """``sentiment/analyzer.py`` module docstring must not claim the
    VADER score 'feeds the ±10 sentiment factor' once that branch is
    disabled. The GPT review caught this docstring still asserting the
    old contract after v6.3 removed the scoring branch."""
    analyzer_src = (
        REPO_ROOT / "src" / "ossuary" / "sentiment" / "analyzer.py"
    ).read_text(encoding="utf-8")
    if SENTIMENT_IN_SCORE:
        return
    forbidden = "feeds the ±10 sentiment"
    assert forbidden not in analyzer_src, (
        f"sentiment/analyzer.py docstring still claims {forbidden!r}, "
        f"but SENTIMENT_IN_SCORE is False. Update the module docstring."
    )
    # Frustration weight in the docstring must also stay aligned.
    legacy_frustration = "+20 risk\n   factor in the engine"
    assert legacy_frustration not in analyzer_src, (
        "sentiment/analyzer.py docstring still cites '+20 risk factor in "
        f"the engine'; the active weight is +{FRUSTRATION_WEIGHT}."
    )


# --- Out-of-scope count: negative + positive presence in active docs ----

def test_out_of_scope_count_consistent_across_docs():
    """The artifact's out-of-scope count (T4 + T5) must appear with the
    matching wording in methodology.md and validation.md, and the legacy
    "14 out-of-scope" wording must not be present anywhere in the active
    docs."""
    data = _load_validation_artifact()
    per_tier = data.get("scopes", {}).get("per_tier_incidents")
    if not per_tier:
        pytest.skip("legacy artifact (no per-tier block); skip.")

    oos_total = sum(
        info["detected"] + info["missed"]
        for info in per_tier.values()
        if not info.get("in_scope")
    )

    legacy = "14 out-of-scope"
    expected = f"{oos_total} out-of-scope"

    for doc_name, doc in (
        ("docs/methodology.md", METHODOLOGY),
        ("docs/validation.md", VALIDATION_DOC),
        ("README.md", README),
    ):
        assert legacy not in doc, (
            f"{doc_name} still contains {legacy!r}; the artifact "
            f"reports {oos_total} out-of-scope incidents (T4+T5). "
            f"Update the active-doc count."
        )

    # Positive presence: at least one doc must cite the actual count
    # (methodology.md §3 + §10 are the obvious places). Don't enforce
    # in every doc — the headline n=170 already does that work — but
    # do require the count appear *somewhere* so authors can't silently
    # drop it.
    docs_with_count = sum(
        expected in doc
        for doc in (METHODOLOGY, VALIDATION_DOC, README)
    )
    assert docs_with_count >= 1, (
        f"no public doc cites '{expected}'. Add it to methodology.md "
        f"§3 (Detection Scope) or §10 (Threats to Validity) so the "
        f"detection-boundary claim is verifiable."
    )


# --- §3.2 per-tier counts must match the artifact -----------------------

def test_detection_scope_per_tier_counts_match_artifact():
    """methodology.md §3.2 'What Ossuary Cannot Detect' enumerates T4 and
    T5 case counts. Both must equal the per-tier counts in the artifact.
    The previous review caught T4 cited as 8 (still v6.1 era) and T5 as
    6, when the v6.3 dataset has 11 and 7. This test pins the specific
    table cells."""
    data = _load_validation_artifact()
    per_tier = data.get("scopes", {}).get("per_tier_incidents")
    if not per_tier:
        pytest.skip("legacy artifact (no per-tier block); skip.")

    section = _section_text(METHODOLOGY, "### 3.2 What Ossuary Cannot Detect")
    for tier, label in (("T4", "Account Compromise"), ("T5", "CI/CD")):
        info = per_tier.get(tier)
        if not info:
            pytest.fail(f"artifact has no {tier} bucket; dataset shifted?")
        n = info["detected"] + info["missed"]
        # The §3.2 row for this tier must contain "N cases (TTier)".
        # We accept either "N cases (T4)" or "N cases (T5)" form so
        # rewording around "all expected FN" / "1 bonus detection" stays
        # editorial.
        needle = f"{n} cases ({tier})"
        assert needle in section, (
            f"§3.2 row for {label} must mention '{needle}' "
            f"(per the artifact). Found stale or missing count."
        )


# --- §3.3 worked-example scores must match the artifact -----------------

def test_detection_boundary_table_scores_match_artifact():
    """methodology.md §3.4 (the 2025 npm phishing case study) cites
    scores for specific packages in a table ('chalk | 35 LOW',
    'eslint-config-prettier | 55 MODERATE'). Each row that names a
    package present in the validation artifact must agree with that
    package's actual score. This is the section that drifted silently
    after v6.3 without showing up in headline-metric tests."""
    data = _load_validation_artifact()
    by_name = {r["case"]["name"]: r for r in data.get("results", [])}
    if not by_name:
        pytest.skip("legacy artifact (no 'results'); skip.")

    section = _section_text(
        METHODOLOGY, "### 3.4 Case Study: The 2025 npm Phishing Wave"
    )
    # Table row form: "| **`name`** | NN LABEL | ...". The first column
    # may use one or both of bold (**...**) and inline code (`...`).
    row_re = re.compile(
        r"\|\s*\*{0,2}`?([A-Za-z][\w./-]*)`?\*{0,2}\s*\|\s*"
        r"\*{0,2}(\d+)\s+\w+",
        re.MULTILINE,
    )
    matched_any = False
    for m in row_re.finditer(section):
        name, claimed = m.group(1), int(m.group(2))
        if name not in by_name:
            continue
        matched_any = True
        actual = by_name[name].get("score")
        assert actual == claimed, (
            f"§3.3 table claims {name} scored {claimed}, but the "
            f"artifact reports {actual}. Update the doc cell."
        )
    assert matched_any, (
        "§3.3 table parsed no rows matching artifact packages — either "
        "the table format changed or no validation-set packages are "
        "cited there. Update this guardrail's regex if the table moved."
    )


# --- T-1 claim guardrail -------------------------------------------------

def test_no_universal_t1_recall_claim():
    """The §8.7 worked examples are 3-4 packages with explicit cutoff
    runs; they cannot ground a "100% T-1 detection rate" claim across
    all governance-detectable incidents. The headline recall is the §8.4
    Scope B figure. The forbidden phrasing was caught by the GPT review
    after the v6.3 reconciliation."""
    forbidden = (
        "100% detection rate for governance-detectable incidents at T-1",
        "All governance-detectable incidents scored CRITICAL",
    )
    for phrase in forbidden:
        assert phrase not in METHODOLOGY, (
            f"methodology.md still contains the unsupported T-1 claim "
            f"{phrase!r}. The §8.7 worked-example set is 3-4 packages "
            f"and cannot ground a universal recall claim; the headline "
            f"recall is §8.4 Scope B."
        )


# --- Section-numbering hygiene --------------------------------------------

def test_methodology_section_numbers_unique():
    """No ``### N.M`` heading may appear more than once in methodology.md.
    The GPT review caught a duplicate ``### 8.7`` (one for Out-of-Scope
    Incident Analysis, another for T-1 Validation) — the section-scoped
    helpers in this file then matched the wrong section's body."""
    section_re = re.compile(r"^###\s+(\d+\.\d+)\s+", re.MULTILINE)
    seen: dict[str, int] = {}
    for match in section_re.finditer(METHODOLOGY):
        num = match.group(1)
        seen[num] = seen.get(num, 0) + 1
    duplicates = {n: c for n, c in seen.items() if c > 1}
    assert not duplicates, (
        f"methodology.md has duplicate section numbers: {duplicates}. "
        f"Renumber so each ### N.M heading is unique."
    )


def test_methodology_section_cross_refs_resolve():
    """Every ``§N.M`` and ``§N.M.K`` cross-reference in methodology.md
    must resolve to an existing ``### N.M`` or ``#### N.M.K`` heading.
    The GPT review caught §3.2 pointing to §8.6 for out-of-scope
    analysis (was §8.7) and several §5.5 / §5.7.1 / §5.10 / §5.10.1
    refs left over from a thesis-chapter draft (no §5.5 etc. exist in
    the standalone methodology doc)."""
    section_re = re.compile(r"^###\s+(\d+\.\d+)\s+", re.MULTILINE)
    subsection_re = re.compile(r"^####\s+(\d+\.\d+\.\d+)\s+", re.MULTILINE)
    headings = {m.group(1) for m in section_re.finditer(METHODOLOGY)} | {
        m.group(1) for m in subsection_re.finditer(METHODOLOGY)
    }
    # Match §N.M or §N.M.K. Word-boundary guard at the end so §6.4.1 is
    # captured as 6.4.1 and not as 6.4 with trailing junk.
    xref_re = re.compile(r"§\s*(\d+\.\d+(?:\.\d+)?)\b")
    bad: list[tuple[str, str]] = []
    for m in xref_re.finditer(METHODOLOGY):
        ref = m.group(1)
        if ref not in headings:
            ctx_start = max(0, m.start() - 60)
            ctx = METHODOLOGY[ctx_start:m.end() + 20].replace("\n", " ")
            bad.append((ref, ctx))
    assert not bad, (
        "methodology.md contains §N.M[.K] cross-references that don't "
        f"resolve to any ### or #### heading:\n"
        + "\n".join(f"  §{ref}  in: …{ctx}" for ref, ctx in bad[:5])
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
