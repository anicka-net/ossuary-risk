"""Shared utilities for the Ossuary dashboard."""

import asyncio
import os
import sys
from contextlib import contextmanager

import streamlit as st

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from ossuary.db.session import get_session
from ossuary.db.models import Package, Score


# -- Async helper --

def run_async(coro):
    """Run async coroutine in Streamlit context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# -- Color palette (muted, accessible) --

COLORS = {
    "critical": "#c0392b",
    "high": "#d35400",
    "moderate": "#b7950b",
    "low": "#27ae60",
    "very_low": "#27ae60",
    "bg_critical": "#f5dddb",
    "bg_high": "#fae5d3",
    "bg_moderate": "#fef9e7",
    "bg_low": "#d5f5e3",
    "bg_very_low": "#d5f5e3",
    "text": "#2c3e50",
    "text_muted": "#7f8c8d",
    "border": "#bdc3c7",
    "surface": "#f8f9fa",
}


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
    /* Clean, muted theme */
    .block-container { max-width: 1100px; }

    /* Monospace for data */
    [data-testid="stMetricValue"] {
        font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    }

    /* Muted dividers */
    hr { border-color: #ecf0f1 !important; }

    /* Tighter tables */
    .stDataFrame { font-size: 0.9em; }

    /* Remove streamlit branding */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    header { visibility: hidden; }

    /* Muted sidebar */
    [data-testid="stSidebar"] {
        background-color: #f8f9fa;
    }

    /* Custom metric styling */
    [data-testid="stMetricLabel"] {
        color: #7f8c8d;
        font-size: 0.85em;
    }
</style>
"""


def apply_style():
    """Apply custom CSS to the page."""
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# -- Database queries --

def get_all_tracked_packages() -> list[dict]:
    """Get all packages with their latest scores from DB."""
    with next(get_session()) as session:
        packages = session.query(Package).all()
        results = []
        for pkg in packages:
            latest_score = (
                session.query(Score)
                .filter(Score.package_id == pkg.id)
                .order_by(Score.calculated_at.desc())
                .first()
            )
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


def get_score_history(package_name: str, ecosystem: str) -> list[dict]:
    """Get historical scores for a package from DB."""
    with next(get_session()) as session:
        pkg = (
            session.query(Package)
            .filter(Package.name == package_name, Package.ecosystem == ecosystem)
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
