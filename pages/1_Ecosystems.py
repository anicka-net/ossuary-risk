"""Ecosystem overview — browse tracked packages by ecosystem."""

import os
import sys

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ossuary.db.session import init_db
from dashboard_utils import (
    apply_style, get_packages_by_ecosystem, get_ecosystem_summary,
    risk_color, COLORS,
)

st.set_page_config(page_title="Ossuary — Ecosystems", layout="wide", initial_sidebar_state="collapsed")
apply_style()

@st.cache_resource
def _init_db():
    init_db()
    return True

_init_db()

st.markdown(
    '<h1 style="margin-bottom:0;color:#2c3e50;">Ecosystems</h1>'
    '<p style="color:#7f8c8d;margin-top:0;">Browse tracked packages by ecosystem</p>',
    unsafe_allow_html=True,
)
st.divider()

# -- Ecosystem selector --

eco_summary = get_ecosystem_summary()
ecosystems = sorted(eco_summary.keys()) if eco_summary else []

if not ecosystems:
    st.info("No packages tracked yet. Score some packages first.")
    st.stop()

selected = st.selectbox("Ecosystem", ecosystems, label_visibility="collapsed")

if not selected:
    st.stop()

# -- Summary stats --

data = eco_summary[selected]
col1, col2, col3, col4 = st.columns(4)
col1.metric("Packages", data["count"])
col2.metric("Avg score", f'{data["avg_score"]:.0f}')
col3.metric("Max score", data["max_score"])
col4.metric("At risk (60+)", data["critical"] + data["high"])

st.divider()

# -- Package table --

packages = get_packages_by_ecosystem(selected)

if packages:
    df = pd.DataFrame(packages)
    df = df[df["score"].notna()].copy()

    if df.empty:
        st.caption("No scored packages in this ecosystem.")
        st.stop()

    # Format for display
    df = df.sort_values("score", ascending=False)
    df["score_display"] = df["score"].astype(int)
    df["concentration_display"] = df["concentration"].apply(
        lambda x: f"{x:.0f}%" if pd.notna(x) else "—"
    )
    df["commits_display"] = df["commits_year"].apply(
        lambda x: str(int(x)) if pd.notna(x) else "—"
    )
    df["contributors_display"] = df["contributors"].apply(
        lambda x: str(int(x)) if pd.notna(x) else "—"
    )
    df["analyzed"] = df["last_analyzed"].apply(
        lambda x: x.strftime("%Y-%m-%d") if x else "—"
    )

    display_df = df[["name", "score_display", "risk_level", "concentration_display",
                      "commits_display", "contributors_display", "analyzed"]].copy()
    display_df.columns = ["Package", "Score", "Level", "Concentration",
                          "Commits/yr", "Contributors", "Last analyzed"]

    # Color the score column
    def color_score(val):
        if val >= 80:
            return f"color: {COLORS['critical']}"
        elif val >= 60:
            return f"color: {COLORS['high']}"
        elif val >= 40:
            return f"color: {COLORS['moderate']}"
        return ""

    styled = display_df.style.map(color_score, subset=["Score"])
    st.dataframe(styled, use_container_width=True, hide_index=True, height=600)

    # -- Score distribution --

    st.divider()
    st.markdown("#### Score distribution")

    import plotly.graph_objects as go

    scores = df["score_display"].tolist()
    fig = go.Figure(go.Histogram(
        x=scores,
        xbins=dict(start=0, end=100, size=10),
        marker_color="#7f8c8d",
        marker_line_color="#2c3e50",
        marker_line_width=1,
    ))

    # Risk band backgrounds
    fig.add_vrect(x0=0, x1=40, fillcolor=COLORS["bg_low"], opacity=0.4, line_width=0)
    fig.add_vrect(x0=40, x1=60, fillcolor=COLORS["bg_moderate"], opacity=0.4, line_width=0)
    fig.add_vrect(x0=60, x1=80, fillcolor=COLORS["bg_high"], opacity=0.4, line_width=0)
    fig.add_vrect(x0=80, x1=100, fillcolor=COLORS["bg_critical"], opacity=0.4, line_width=0)

    fig.add_vline(x=60, line_dash="dot", line_color=COLORS["high"],
                  annotation_text="risk threshold", annotation_position="top")

    fig.update_layout(
        xaxis_title="Score",
        yaxis_title="Count",
        height=300,
        margin=dict(l=40, r=20, t=20, b=40),
        bargap=0.05,
        plot_bgcolor="white",
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.caption("No packages in this ecosystem.")

st.divider()
st.caption("Ossuary v0.2.0")
