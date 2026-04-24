"""Shared utilities for the Ossuary dashboard."""

import asyncio
import re as _re
from typing import Optional

try:
    import streamlit as st
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    st = None

from ossuary import __version__ as VERSION
from ossuary.db.session import get_session
from ossuary.db.models import Package, Score


# Local copy of the PyPI PEP 503 normaliser. Streamlit reuses its worker
# process across reruns, so a long-running dashboard can hold a stale
# ``ossuary.services.cache`` module from before ``normalize_package_name``
# was added; importing it inside a request would then crash with
# ``ImportError`` and break the page. Inlining the tiny PEP 503 rule
# (lowercase + collapse runs of ``-``/``_``/``.`` to ``-``) keeps the
# dashboard self-contained for read paths. The cache module remains the
# source of truth for *write* paths (``ScoreCache.get_or_create_package``).
_PYPI_NORMALIZE_RE = _re.compile(r"[-_.]+")


def _normalize_package_name(name: str, ecosystem: str) -> str:
    if ecosystem == "pypi":
        return _PYPI_NORMALIZE_RE.sub("-", name.strip().lower())
    return name


# -- Async helper --

def run_async(coro):
    """Run async coroutine in Streamlit context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# -- Color palette (openSUSE branding) --
# Based on https://opensuse.github.io/branding-guidelines/
# and the chameleon theme: github.com/openSUSE/chameleon

COLORS = {
    "critical": "#a55860",    # openSUSE red
    "high": "#b96a35",        # openSUSE orange
    "moderate": "#bb9d43",    # openSUSE yellow
    "low": "#73ba25",         # openSUSE green
    "very_low": "#73ba25",    # openSUSE green
    "bg_critical": "#f5dddb",
    "bg_high": "#fae5d3",
    "bg_moderate": "#fef9e7",
    "bg_low": "#e8f5d6",
    "bg_very_low": "#e8f5d6",
    "text": "#173f4f",        # openSUSE teal (primary)
    "text_muted": "#6c757d",  # gray-600
    "border": "#ced4da",      # gray-400
    "surface": "#f8f9fa",     # gray-100
    "accent": "#35b9ab",      # openSUSE turquoise
    "link": "#21a4df",        # openSUSE blue
}


def risk_level_str(risk_level) -> str:
    """Return the string form of a risk level, robust to stale imports.

    Streamlit reuses its worker process across reruns, so a long-running
    dashboard can hold an older ``RiskLevel`` import that doesn't yet have
    ``INSUFFICIENT_DATA``. Comparing with ``RiskLevel.INSUFFICIENT_DATA``
    in that situation crashes with ``AttributeError`` even though the
    breakdown carries the right value.

    The same helper also handles the case where the breakdown has been
    round-tripped through the cache JSON and ``risk_level`` arrives as a
    plain string instead of the enum. Always compare on the string form
    returned here.
    """
    return getattr(risk_level, "value", risk_level)


def risk_color(level: str) -> str:
    """Get color for a risk level."""
    return COLORS.get(level.lower().replace(" ", "_"), COLORS["text_muted"])


def risk_dot(level: str) -> str:
    """HTML colored dot for risk level."""
    color = risk_color(level)
    return f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{color};margin-right:6px;vertical-align:middle;"></span>'


def risk_badge(level: str, score: int) -> str:
    """HTML badge with score and level."""
    color = risk_color(level)
    bg = COLORS.get(f"bg_{level.lower().replace(' ', '_')}", COLORS["surface"])
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:3px;'
        f'background:{bg};color:{color};font-weight:600;font-family:monospace;'
        f'font-size:0.9em;border:1px solid {color}30;">'
        f'{score} {level}</span>'
    )


# -- Custom CSS --

CUSTOM_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Source+Sans+Pro:wght@300;400;600&display=swap');

    /* openSUSE-inspired theme */
    .block-container { max-width: 1100px; }

    /* Source Sans Pro for body text */
    .stApp, .stMarkdown, p, li, td, th {
        font-family: 'Source Sans Pro', 'Open Sans', sans-serif;
    }

    /* Monospace for data values */
    [data-testid="stMetricValue"] {
        font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    }

    /* openSUSE teal headings */
    h1, h2, h3 { color: #173f4f; }

    /* openSUSE green links */
    a { color: #21a4df; }

    /* Muted dividers */
    hr { border-color: #dee2e6 !important; }

    /* Tighter tables */
    .stDataFrame { font-size: 0.9em; }

    /* Remove streamlit branding */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    header { visibility: hidden; }

    /* Sidebar with teal tint */
    [data-testid="stSidebar"] {
        background-color: #f0f7f9;
        border-right: 2px solid #173f4f20;
    }

    /* Metric labels */
    [data-testid="stMetricLabel"] {
        color: #6c757d;
        font-size: 0.85em;
        font-family: 'Source Sans Pro', sans-serif;
    }

    /* Expander headers in teal */
    .streamlit-expanderHeader {
        color: #173f4f;
        font-weight: 600;
    }
</style>
"""


def apply_style():
    """Apply custom CSS to the page."""
    if st is None:
        return
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# -- Database queries --

def get_all_tracked_packages() -> list[dict]:
    """Get all packages with their latest scores and deltas from DB."""
    with next(get_session()) as session:
        packages = session.query(Package).all()
        results = []
        for pkg in packages:
            recent_scores = (
                session.query(Score)
                .filter(Score.package_id == pkg.id)
                .order_by(Score.calculated_at.desc())
                .limit(2)
                .all()
            )
            latest_score = recent_scores[0] if recent_scores else None
            previous_score = recent_scores[1] if len(recent_scores) >= 2 else None
            # Extract v4.1 fields from breakdown JSON
            maturity_evidence = None
            takeover_evidence = None
            is_mature = False
            has_takeover_risk = False
            if latest_score and latest_score.breakdown:
                pf = (latest_score.breakdown
                      .get("score", {})
                      .get("components", {})
                      .get("protective_factors", {}))
                mat = pf.get("maturity", {})
                tak = pf.get("takeover_risk", {})
                maturity_evidence = mat.get("evidence")
                takeover_evidence = tak.get("evidence")
                is_mature = maturity_evidence is not None
                has_takeover_risk = (tak.get("score", 0) > 0)

            # Compute delta from previous score. Either side may be None
            # if it's an INSUFFICIENT_DATA row (the data-completeness
            # contract leaves final_score = NULL in that case); skip the
            # delta in that case rather than crash.
            delta = None
            previous_score_val = None
            if (
                latest_score
                and previous_score
                and latest_score.final_score is not None
                and previous_score.final_score is not None
            ):
                delta = latest_score.final_score - previous_score.final_score
                previous_score_val = previous_score.final_score

            results.append({
                "id": pkg.id,
                "name": pkg.name,
                "ecosystem": pkg.ecosystem,
                "repo_url": pkg.repo_url or "",
                "last_analyzed": pkg.last_analyzed,
                "score": latest_score.final_score if latest_score else None,
                "risk_level": latest_score.risk_level if latest_score else None,
                "concentration": latest_score.maintainer_concentration if latest_score else None,
                "commits_year": latest_score.commits_last_year if latest_score else None,
                "contributors": latest_score.unique_contributors if latest_score else None,
                "downloads": latest_score.weekly_downloads if latest_score else None,
                "is_mature": is_mature,
                "maturity_evidence": maturity_evidence,
                "has_takeover_risk": has_takeover_risk,
                "takeover_evidence": takeover_evidence,
                "delta": delta,
                "previous_score": previous_score_val,
            })
        return results


def get_packages_by_ecosystem(ecosystem: str) -> list[dict]:
    """Get packages filtered by ecosystem."""
    all_pkgs = get_all_tracked_packages()
    return [p for p in all_pkgs if p["ecosystem"] == ecosystem]


def get_comparison_packages(name: str, ecosystem: str, score: int) -> dict:
    """Find nearest safe and nearest risky package in same ecosystem for comparison."""
    pkgs = get_packages_by_ecosystem(ecosystem)
    pkgs = [p for p in pkgs if p["name"] != name and p["score"] is not None]

    nearest_safe = None
    nearest_risky = None
    safe_dist = float("inf")
    risky_dist = float("inf")

    for p in pkgs:
        s = p["score"]
        if s < 60:
            dist = abs(s - score)
            if dist < safe_dist:
                safe_dist = dist
                nearest_safe = p
        else:
            dist = abs(s - score)
            if dist < risky_dist:
                risky_dist = dist
                nearest_risky = p

    return {"safe": nearest_safe, "risky": nearest_risky}


def get_ecosystem_summary() -> dict:
    """Get summary stats per ecosystem."""
    all_pkgs = get_all_tracked_packages()
    ecosystems = {}
    for p in all_pkgs:
        eco = p["ecosystem"]
        if eco not in ecosystems:
            ecosystems[eco] = {"count": 0, "scored": 0, "scores": [], "critical": 0, "high": 0}
        ecosystems[eco]["count"] += 1
        if p["score"] is not None:
            ecosystems[eco]["scored"] += 1
            ecosystems[eco]["scores"].append(p["score"])
            if p["score"] >= 80:
                ecosystems[eco]["critical"] += 1
            elif p["score"] >= 60:
                ecosystems[eco]["high"] += 1

    for eco, data in ecosystems.items():
        scores = data["scores"]
        data["avg_score"] = sum(scores) / len(scores) if scores else 0
        data["max_score"] = max(scores) if scores else 0

    return ecosystems


def get_unscored_packages(ecosystem: Optional[str] = None) -> list[dict]:
    """Return packages registered in the DB but never successfully scored.

    These are rows where ``last_analyzed IS NULL`` — typically the
    residue of a scoring attempt that failed mid-flight (transient
    registry/GitHub error). The dashboard's ecosystem page surfaces them
    with a retry button so the user can clear the orphan without
    dropping to the CLI.
    """
    with next(get_session()) as session:
        q = session.query(Package).filter(Package.last_analyzed.is_(None))
        if ecosystem:
            q = q.filter(Package.ecosystem == ecosystem)
        return [
            {
                "name": p.name,
                "ecosystem": p.ecosystem,
                "repo_url": p.repo_url or None,
            }
            for p in q.all()
        ]


def _run_score_targets(targets: list[dict], *, force: bool, use_cache: bool) -> dict:
    """Score a list of targets sequentially with the given cache flags.

    Returns ``{"success": int, "errors": list[(name, error)]}``. Runs in
    the Streamlit request thread; suitable for small N (typically <50).
    """
    from ossuary.services.scorer import score_package

    async def _run():
        success = 0
        errors: list[tuple[str, str]] = []
        for t in targets:
            r = await score_package(
                t["name"], t["ecosystem"],
                repo_url=t.get("repo_url"),
                force=force,
                use_cache=use_cache,
            )
            if r.success:
                success += 1
            else:
                errors.append((t["name"], r.error or "unknown"))
        return {"success": success, "errors": errors}

    return run_async(_run())


def rescore_packages(targets: list[dict]) -> dict:
    """Re-score every target, bypassing the score cache only.

    ``force=True`` skips the cached Score lookup so each call recomputes
    a fresh breakdown, but ``use_cache=True`` lets the snapshot cache
    serve cheap upstream-data reuse where the SLA is still good (so a
    re-score-all on 100 packages doesn't issue 100 GitHub round-trips).
    Negative-cache entries (``failure_kind=repo_not_found`` etc.) are
    still respected — use :func:`retry_packages` to bypass those.
    """
    return _run_score_targets(targets, force=True, use_cache=True)


def retry_packages(targets: list[dict]) -> dict:
    """Retry every target from scratch, bypassing all caches.

    ``use_cache=False`` skips the score cache, the snapshot cache, and
    the negative cache, so a package stuck in
    ``failure_kind=repo_not_found`` will actually re-attempt upstream
    collection. Caveat: a true upstream-data problem (e.g. a typo in
    the registry's repository URL) will still fail — retry helps with
    transient failures, not bad source data.
    """
    return _run_score_targets(targets, force=True, use_cache=False)


def get_score_history(package_name: str, ecosystem: str) -> list[dict]:
    """Get historical scores for a package from DB.

    PyPI rows are stored under the PEP 503 canonical name (lowercase,
    runs of ``-``/``_``/``.`` collapsed to ``-``). Normalise the lookup
    so that a user-entered ``PyYAML`` finds the row stored as ``pyyaml``.

    Uses the dashboard-local copy of the normaliser; see
    ``_normalize_package_name`` for why.
    """
    canonical = _normalize_package_name(package_name, ecosystem)
    with next(get_session()) as session:
        pkg = (
            session.query(Package)
            .filter(Package.name == canonical, Package.ecosystem == ecosystem)
            .first()
        )
        if not pkg:
            return []

        scores = (
            session.query(Score)
            .filter(Score.package_id == pkg.id)
            .order_by(Score.cutoff_date.asc())
            .all()
        )
        return [
            {
                "date": s.cutoff_date,
                "score": s.final_score,
                "risk_level": s.risk_level,
                "concentration": s.maintainer_concentration,
                "commits_year": s.commits_last_year,
                "contributors": s.unique_contributors,
            }
            for s in scores
        ]
