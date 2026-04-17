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

from ossuary.scoring import METHODOLOGY_VERSION
from ossuary.scoring.reputation import TOP_PACKAGES


REPO_ROOT = Path(__file__).resolve().parent.parent
METHODOLOGY = (REPO_ROOT / "docs" / "methodology.md").read_text(encoding="utf-8")


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
