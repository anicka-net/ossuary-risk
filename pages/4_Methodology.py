"""Methodology — scoring formula, validation, and detection scope."""

import json
import os
import sys

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dashboard_utils import apply_style, COLORS

st.set_page_config(page_title="Ossuary — Methodology", layout="wide", initial_sidebar_state="collapsed")
apply_style()

st.markdown(
    '<h1 style="margin-bottom:0;color:#2c3e50;">Methodology</h1>'
    '<p style="color:#7f8c8d;margin-top:0;">Scoring formula and validation</p>',
    unsafe_allow_html=True,
)
st.divider()

# -- Formula --

st.markdown("#### Scoring formula")

st.latex(r"\text{Score} = \text{Base Risk} + \text{Activity Modifier} + \text{Protective Factors}")
st.caption("Clamped to 0–100")

st.markdown("")

col1, col2 = st.columns(2)

with col1:
    st.markdown("##### Base risk (maintainer concentration)")
    st.dataframe({
        "Concentration": ["< 30%", "30–49%", "50–69%", "70–89%", ">= 90%"],
        "Points": [20, 40, 60, 80, 100],
    }, use_container_width=True, hide_index=True)

    st.markdown("##### Activity modifier (commits/year)")
    st.dataframe({
        "Commits/year": ["> 50", "12–50", "4–11", "< 4"],
        "Points": ["-30", "-15", "0", "+20"],
    }, use_container_width=True, hide_index=True)

with col2:
    st.markdown("##### Protective factors")
    st.dataframe({
        "Factor": [
            "Tier-1 reputation (500+ repos or 100K+ stars)",
            "GitHub Sponsors enabled",
            "Organization with 3+ admins",
            "Weekly downloads > 50M",
            "Weekly downloads > 10M",
            "Concentration < 40%",
            "Contributors > 20",
            "CII Best Practices badge",
            "Frustration signals detected",
        ],
        "Points": ["-25", "-15", "-15", "-20", "-10", "-10", "-10", "-10", "+20"],
    }, use_container_width=True, hide_index=True)

st.divider()

# -- Detection scope --

st.markdown("#### Detection scope")

col1, col2 = st.columns(2)

with col1:
    st.markdown("**Can detect**")
    st.markdown("""
    - Maintainer abandonment
    - High concentration risk (bus factor)
    - Economic frustration signals
    - Declining activity trends
    - Governance centralization
    """)

with col2:
    st.markdown("**Cannot detect**")
    st.markdown("""
    - Account compromise (credential theft)
    - Dependency confusion attacks
    - Typosquatting
    - Malicious code injection
    - Sophisticated social engineering (xz-utils)
    """)

st.divider()

# -- Validation results --

st.markdown("#### Validation")

# Load from JSON
results_dir = os.path.join(os.path.dirname(__file__), "..")
results_files = sorted(
    f for f in os.listdir(results_dir)
    if f.startswith("validation_results") and f.endswith(".json")
)

validation_data = None
if results_files:
    latest = os.path.join(results_dir, results_files[-1])
    try:
        with open(latest) as f:
            validation_data = json.load(f)
    except Exception:
        pass

if validation_data:
    st.caption(f"Source: {results_files[-1]} ({validation_data.get('timestamp', '')[:10]})")

    # Key metrics
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Packages", validation_data.get("total", 0))
    col2.metric("Accuracy", f"{validation_data.get('accuracy', 0)*100:.1f}%")
    col3.metric("Precision", f"{validation_data.get('precision', 0)*100:.0f}%")
    col4.metric("Recall", f"{validation_data.get('recall', 0)*100:.0f}%")
    col5.metric("F1", f"{validation_data.get('f1_score', 0):.2f}")

    st.markdown("")

    col1, col2 = st.columns(2)

    with col1:
        # Confusion matrix
        cm = validation_data.get("confusion_matrix", {})
        tn, fp = cm.get("TN", 0), cm.get("FP", 0)
        fn, tp = cm.get("FN", 0), cm.get("TP", 0)

        fig_cm = go.Figure(data=go.Heatmap(
            z=[[tn, fp], [fn, tp]],
            x=["Predicted Safe", "Predicted Risky"],
            y=["Actually Safe", "Actually Risky"],
            text=[[f"TN: {tn}", f"FP: {fp}"], [f"FN: {fn}", f"TP: {tp}"]],
            texttemplate="%{text}",
            textfont={"size": 14},
            colorscale=[[0, COLORS["bg_low"]], [0.5, COLORS["bg_moderate"]], [1, COLORS["bg_critical"]]],
            showscale=False,
        ))
        fig_cm.update_layout(
            height=280,
            margin=dict(l=20, r=20, t=20, b=20),
            plot_bgcolor="white",
        )
        st.plotly_chart(fig_cm, use_container_width=True)

    with col2:
        # By ecosystem bar chart
        by_eco = validation_data.get("by_ecosystem", {})
        if by_eco:
            eco_names = sorted(by_eco.keys())
            correct = [by_eco[e]["correct"] for e in eco_names]
            total = [by_eco[e]["total"] for e in eco_names]
            incorrect = [t - c for t, c in zip(total, correct)]

            fig_eco = go.Figure()
            fig_eco.add_trace(go.Bar(
                name="Correct",
                x=eco_names,
                y=correct,
                marker_color=COLORS["low"],
            ))
            fig_eco.add_trace(go.Bar(
                name="Incorrect",
                x=eco_names,
                y=incorrect,
                marker_color=COLORS["bg_critical"],
            ))
            fig_eco.update_layout(
                barmode="stack",
                height=280,
                margin=dict(l=20, r=20, t=20, b=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                plot_bgcolor="white",
            )
            st.plotly_chart(fig_eco, use_container_width=True)

    # By attack type
    by_attack = validation_data.get("by_attack_type", {})
    if by_attack:
        st.markdown("##### By attack type")
        rows = []
        for atype, stats in sorted(by_attack.items()):
            pct = stats["correct"] / stats["total"] * 100 if stats["total"] > 0 else 0
            rows.append({
                "Type": atype,
                "Correct": f"{stats['correct']}/{stats['total']}",
                "Rate": f"{pct:.0f}%",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # All results expandable
    results = validation_data.get("results", [])
    if results:
        with st.expander(f"All {len(results)} results"):
            show = st.radio(
                "Filter:",
                ["All", "Incidents", "Controls", "Errors/FN"],
                horizontal=True,
                label_visibility="collapsed",
            )

            rows = []
            for r in results:
                case = r.get("case", {})
                error = r.get("error")

                if show == "Incidents" and case.get("expected_outcome") != "incident":
                    continue
                if show == "Controls" and case.get("expected_outcome") != "safe":
                    continue
                if show == "Errors/FN" and r.get("classification") != "FN" and not error:
                    continue

                rows.append({
                    "Package": case.get("name", ""),
                    "Eco": case.get("ecosystem", ""),
                    "Expected": case.get("expected_outcome", ""),
                    "Score": r.get("score", "—") if not error else "ERR",
                    "Level": r.get("risk_level", "") if not error else "",
                    "Class": r.get("classification", ""),
                    "OK": "Y" if r.get("correct") else ("ERR" if error else "N"),
                })

            if rows:
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=400)

else:
    st.info("No validation results found. Run: `python scripts/validate.py -o validation_results_v2.json`")

st.divider()

# -- Risk levels reference --

st.markdown("#### Risk levels")

st.dataframe({
    "Score": ["0–20", "21–40", "41–60", "61–80", "81–100"],
    "Level": ["Very Low", "Low", "Moderate", "High", "Critical"],
    "Action": [
        "Routine monitoring",
        "Quarterly review",
        "Monthly review",
        "Weekly review + contingency plan",
        "Immediate action required",
    ],
}, use_container_width=True, hide_index=True)

st.divider()
st.caption("Ossuary v0.2.0 · [Full methodology](https://github.com/anicka-net/ossuary-risk/blob/main/docs/methodology.md)")
