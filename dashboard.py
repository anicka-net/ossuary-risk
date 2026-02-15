"""Ossuary - OSS Supply Chain Risk Scoring Dashboard."""

import os
import sys

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from ossuary.db.session import init_db
from dashboard_utils import (
    apply_style, get_all_tracked_packages, get_ecosystem_summary,
    risk_color, COLORS,
)

st.set_page_config(
    page_title="Ossuary",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="collapsed",
)

apply_style()


@st.cache_resource
def _init_db():
    init_db()
    return True


_init_db()

# -- Header --

st.markdown(
    '<h1 style="margin-bottom:0;color:#2c3e50;">Ossuary</h1>'
    '<p style="color:#7f8c8d;margin-top:0;">OSS Supply Chain Risk Scoring</p>',
    unsafe_allow_html=True,
)

if not os.getenv("GITHUB_TOKEN"):
    st.caption("GITHUB_TOKEN not set — API rate limits will be restrictive.")

st.divider()

# -- Load data --

all_packages = get_all_tracked_packages()
scored = [p for p in all_packages if p["score"] is not None]

if not scored:
    st.markdown(
        "No packages tracked yet. Analyze a package to get started, "
        "or run `ossuary seed` to populate with a starter set."
    )
    st.markdown("")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.page_link("pages/3_Score.py", label="Score a package", icon=None)
    with col2:
        st.page_link("pages/4_Methodology.py", label="View methodology", icon=None)
    with col3:
        st.page_link("pages/1_Ecosystems.py", label="Browse ecosystems", icon=None)
    st.divider()
    st.caption("Ossuary v0.2.0 · [source](https://github.com/anicka-net/ossuary-risk)")
    st.stop()

# -- Key metrics --

critical = [p for p in scored if p["score"] >= 80]
high = [p for p in scored if 60 <= p["score"] < 80]
moderate = [p for p in scored if 40 <= p["score"] < 60]
safe = [p for p in scored if p["score"] < 40]

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Tracked", len(scored))
col2.metric("Critical", len(critical))
col3.metric("High", len(high))
col4.metric("Moderate", len(moderate))
col5.metric("Safe", len(safe))

st.divider()

# -- Ecosystem summary cards --

st.markdown("#### By ecosystem")

eco_summary = get_ecosystem_summary()

if eco_summary:
    cols = st.columns(min(len(eco_summary), 4))
    for i, (eco, data) in enumerate(sorted(eco_summary.items())):
        with cols[i % len(cols)]:
            avg = data["avg_score"]
            color = risk_color(
                "critical" if avg >= 80 else "high" if avg >= 60
                else "moderate" if avg >= 40 else "low"
            )
            st.markdown(
                f'<div style="padding:12px;border:1px solid #ecf0f1;border-radius:4px;'
                f'margin-bottom:8px;border-left:3px solid {color};">'
                f'<a href="/Ecosystems?eco={eco}" target="_self" style="color:inherit;text-decoration:none;"><strong>{eco}</strong></a><br>'
                f'<span style="font-family:monospace;font-size:1.4em;">{data["count"]}</span> '
                f'<span style="color:#7f8c8d;">packages</span><br>'
                f'<span style="color:#7f8c8d;font-size:0.85em;">'
                f'avg {avg:.0f} · max {data["max_score"]}'
                f'{" · " + str(data["critical"]) + " critical" if data["critical"] else ""}'
                f'{" · " + str(data["high"]) + " high" if data["high"] else ""}'
                f'</span></div>',
                unsafe_allow_html=True,
            )

st.divider()

# -- Highest risk packages --

st.markdown("#### Highest risk")

at_risk = [p for p in scored if p["score"] >= 40]
at_risk.sort(key=lambda p: p["score"], reverse=True)

if at_risk:
    for p in at_risk[:15]:
        score = p["score"]
        level = p["risk_level"] or ""
        color = risk_color(level)
        conc = f'{p["concentration"]:.0f}%' if p["concentration"] is not None else "—"
        commits = p["commits_year"] if p["commits_year"] is not None else "—"

        col1, col2, col3, col4 = st.columns([3, 1, 2, 2])
        with col1:
            st.markdown(f"**{p['name']}** · {p['ecosystem']}")
        with col2:
            st.markdown(
                f'<span style="color:{color};font-family:monospace;font-weight:600;">'
                f'{score}</span> <span style="color:#7f8c8d;font-size:0.85em;">{level}</span>',
                unsafe_allow_html=True,
            )
        with col3:
            st.caption(f"concentration {conc}")
        with col4:
            st.caption(f"{commits} commits/yr")
else:
    st.caption("No packages at moderate risk or above.")

# -- Navigation --

st.divider()
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.page_link("pages/3_Score.py", label="Score a package")
with col2:
    st.page_link("pages/1_Ecosystems.py", label="Browse ecosystems")
with col3:
    st.page_link("pages/2_Package.py", label="Package detail")
with col4:
    st.page_link("pages/4_Methodology.py", label="Methodology")

st.caption("Ossuary v0.2.0 · [source](https://github.com/anicka-net/ossuary-risk)")
