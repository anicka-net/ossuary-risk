"""Score a new package — simple input form."""

import os
import sys
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ossuary.db.session import init_db
from ossuary.services.scorer import score_package
from dashboard_utils import apply_style, run_async, risk_color, COLORS

st.set_page_config(page_title="Ossuary — Score", layout="wide", initial_sidebar_state="collapsed")
apply_style()

@st.cache_resource
def _init_db():
    init_db()
    return True

_init_db()

st.markdown(
    '<h1 style="margin-bottom:0;color:#2c3e50;">Score</h1>'
    '<p style="color:#7f8c8d;margin-top:0;">Analyze a package</p>',
    unsafe_allow_html=True,
)
st.divider()

# -- Input form --

col1, col2 = st.columns([3, 1])

with col1:
    package_name = st.text_input(
        "Package name",
        placeholder="e.g., lodash, requests, owner/repo",
    )

with col2:
    ecosystem = st.selectbox(
        "Ecosystem",
        ["npm", "pypi", "cargo", "rubygems", "packagist", "nuget", "go", "github"],
    )

cutoff_input = st.text_input(
    "Cutoff date (optional, for T-1 analysis)",
    placeholder="YYYY-MM-DD",
)

if st.button("Analyze", type="primary", use_container_width=True):
    if not package_name:
        st.error("Enter a package name.")
        st.stop()

    cutoff_date = None
    if cutoff_input:
        try:
            cutoff_date = datetime.strptime(cutoff_input, "%Y-%m-%d")
        except ValueError:
            st.error("Invalid date format. Use YYYY-MM-DD.")
            st.stop()

    with st.status(f"Analyzing {package_name}...", expanded=True) as status:
        result = run_async(score_package(package_name, ecosystem, cutoff_date=cutoff_date))

        if result.success and result.breakdown:
            status.update(label="Done", state="complete")
            st.session_state.score_result = result
            st.session_state.score_pkg = package_name
            st.session_state.score_eco = ecosystem
        else:
            status.update(label=f"Error: {result.error}", state="error")

# -- Display results --

if "score_result" in st.session_state and st.session_state.get("score_pkg"):
    result = st.session_state.score_result
    b = result.breakdown
    pkg = st.session_state.score_pkg
    eco = st.session_state.score_eco

    score = b.final_score
    level = b.risk_level.value
    color = risk_color(level)

    if result.warnings:
        for w in result.warnings:
            st.caption(f"Note: {w}")

    st.divider()

    # Score display
    st.markdown(
        f'<div style="display:flex;align-items:baseline;gap:16px;">'
        f'<span style="font-size:2.5em;font-family:monospace;font-weight:700;color:{color};">{score}</span>'
        f'<span style="font-size:1.2em;color:{color};">{level}</span>'
        f'<span style="color:#7f8c8d;">{pkg} · {eco}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown("")

    # Metrics
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Base risk", b.base_risk)
    col2.metric("Activity", f"{b.activity_modifier:+d}")
    col3.metric("Protective", f"{b.protective_factors.total:+d}")
    col4.metric("Concentration", f"{b.maintainer_concentration:.0f}%")
    col5.metric("Commits/yr", b.commits_last_year)

    if b.explanation:
        st.markdown(f"**Analysis:** {b.explanation}")

    if b.recommendations:
        with st.expander("Recommendations"):
            for rec in b.recommendations:
                st.markdown(f"- {rec}")

    # Link to package detail page
    st.markdown("")
    st.markdown(f'<a href="/Package?name={pkg}&eco={eco}" target="_self">View full detail for {pkg}</a>', unsafe_allow_html=True)

st.divider()
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.page_link("dashboard.py", label="Home")
with col2:
    st.page_link("pages/1_Ecosystems.py", label="Browse ecosystems")
with col3:
    st.page_link("pages/2_Package.py", label="Package detail")
with col4:
    st.page_link("pages/4_Methodology.py", label="Methodology")

st.caption("Ossuary v0.3.0")
