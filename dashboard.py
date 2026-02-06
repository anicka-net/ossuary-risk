"""Ossuary Risk Dashboard - Streamlit visualization for OSS supply chain risk."""

import subprocess
import json
import re

import plotly.graph_objects as go
import streamlit as st

# Set page config first
st.set_page_config(
    page_title="Ossuary - OSS Supply Chain Risk",
    page_icon="💀",
    layout="wide",
)


def score_to_color(score: int) -> str:
    """Convert score to color."""
    if score >= 80:
        return "#dc3545"  # Red
    elif score >= 60:
        return "#fd7e14"  # Orange
    elif score >= 40:
        return "#ffc107"  # Yellow
    else:
        return "#28a745"  # Green


def create_gauge(score: int, title: str = "Risk Score") -> go.Figure:
    """Create a gauge chart for risk score."""
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=score,
            domain={"x": [0, 1], "y": [0, 1]},
            title={"text": title, "font": {"size": 24}},
            gauge={
                "axis": {"range": [0, 100], "tickwidth": 1},
                "bar": {"color": score_to_color(score)},
                "steps": [
                    {"range": [0, 20], "color": "#d4edda"},
                    {"range": [20, 40], "color": "#d4edda"},
                    {"range": [40, 60], "color": "#fff3cd"},
                    {"range": [60, 80], "color": "#ffe5d0"},
                    {"range": [80, 100], "color": "#f8d7da"},
                ],
                "threshold": {
                    "line": {"color": "black", "width": 4},
                    "thickness": 0.75,
                    "value": score,
                },
            },
        )
    )
    fig.update_layout(height=300, margin=dict(l=20, r=20, t=50, b=20))
    return fig


def create_breakdown_chart(base: int, activity: int, protective: int) -> go.Figure:
    """Create a waterfall chart showing score breakdown."""
    categories = ["Base Risk", "Activity", "Protective", "Final"]
    final = max(0, min(100, base + activity + protective))

    fig = go.Figure(
        go.Waterfall(
            name="Score",
            orientation="v",
            measure=["relative", "relative", "relative", "total"],
            x=categories,
            y=[base, activity, protective, 0],
            text=[f"+{base}", f"{activity:+d}", f"{protective:+d}", str(final)],
            textposition="outside",
            connector={"line": {"color": "rgb(63, 63, 63)"}},
            decreasing={"marker": {"color": "#28a745"}},
            increasing={"marker": {"color": "#dc3545"}},
            totals={"marker": {"color": score_to_color(final)}},
        )
    )

    fig.update_layout(
        title="Score Breakdown",
        showlegend=False,
        height=300,
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


def create_validation_chart() -> go.Figure:
    """Create confusion matrix visualization."""
    fig = go.Figure(
        data=go.Heatmap(
            z=[[72, 0], [7, 13]],
            x=["Predicted Safe", "Predicted Risky"],
            y=["Actually Safe", "Actually Risky"],
            text=[["TN: 72", "FP: 0"], ["FN: 7", "TP: 13"]],
            texttemplate="%{text}",
            colorscale=[[0, "#d4edda"], [0.5, "#fff3cd"], [1, "#f8d7da"]],
            showscale=False,
        )
    )
    fig.update_layout(
        title="Validation Confusion Matrix (n=92)",
        height=300,
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


def create_comparison_chart() -> go.Figure:
    """Create bar chart comparing tools."""
    tools = ["Ossuary", "Scorecard"]

    fig = go.Figure()

    # event-stream scores (inverted for Scorecard to show risk)
    fig.add_trace(go.Bar(
        name="event-stream",
        x=tools,
        y=[100, 76],  # Ossuary 100, Scorecard 2.4/10 = 76% risk
        marker_color="#dc3545",
    ))

    # express scores
    fig.add_trace(go.Bar(
        name="express",
        x=tools,
        y=[0, 18],  # Ossuary 0, Scorecard 8.2/10 = 18% risk
        marker_color="#28a745",
    ))

    fig.update_layout(
        title="Tool Comparison (event-stream vs express)",
        barmode="group",
        height=300,
        yaxis_title="Risk Score",
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


def run_ossuary(package: str, ecosystem: str, cutoff: str = None) -> dict:
    """Run ossuary CLI and parse output."""
    cmd = ["ossuary", "score", package, "-e", ecosystem, "-j"]
    if cutoff:
        cmd.extend(["-c", cutoff])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env={"GITHUB_TOKEN": subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()}
        )

        # Parse JSON output
        for line in result.stdout.split("\n"):
            if line.strip().startswith("{"):
                return json.loads(line)

        return {"error": result.stderr or "No JSON output"}
    except subprocess.TimeoutExpired:
        return {"error": "Analysis timed out"}
    except Exception as e:
        return {"error": str(e)}


# Main app
st.title("💀 Ossuary")
st.subheader("OSS Supply Chain Risk Scoring")

# Sidebar
st.sidebar.header("About")
st.sidebar.markdown("""
**Ossuary** analyzes open source packages to identify
governance-based supply chain risks before incidents occur.

**Validation Metrics:**
- 92.4% Accuracy
- 100% Precision
- 65% Recall
- F1 Score: 0.79
""")

st.sidebar.header("Risk Levels")
st.sidebar.markdown("""
| Score | Level |
|-------|-------|
| 0-19 | 🟢 Very Low |
| 20-39 | 🟢 Low |
| 40-59 | 🟡 Moderate |
| 60-79 | 🟠 High |
| 80-100 | 🔴 Critical |
""")

# Main content
tab1, tab2, tab3 = st.tabs(["📊 Score Package", "📈 Validation Results", "📚 Methodology"])

with tab1:
    st.header("Score a Package")

    # Quick examples
    st.markdown("**Quick Examples:**")
    col1, col2, col3, col4 = st.columns(4)

    if col1.button("event-stream", use_container_width=True):
        st.session_state.package = "event-stream"
        st.session_state.ecosystem = "npm"
    if col2.button("colors", use_container_width=True):
        st.session_state.package = "colors"
        st.session_state.ecosystem = "npm"
    if col3.button("express", use_container_width=True):
        st.session_state.package = "express"
        st.session_state.ecosystem = "npm"
    if col4.button("requests", use_container_width=True):
        st.session_state.package = "requests"
        st.session_state.ecosystem = "pypi"

    st.divider()

    col1, col2 = st.columns([2, 1])

    with col1:
        package_name = st.text_input(
            "Package Name",
            value=st.session_state.get("package", ""),
            placeholder="e.g., lodash"
        )
        ecosystem = st.selectbox(
            "Ecosystem",
            ["npm", "pypi"],
            index=0 if st.session_state.get("ecosystem", "npm") == "npm" else 1
        )
        cutoff_date = st.text_input(
            "Cutoff Date (optional)",
            placeholder="YYYY-MM-DD for T-1 analysis"
        )

    if st.button("🔍 Calculate Risk Score", type="primary", use_container_width=True):
        if not package_name:
            st.error("Please enter a package name")
        else:
            with st.spinner(f"Analyzing {package_name}... (this may take a minute)"):
                # For demo, use pre-computed results for known packages
                demo_results = {
                    "event-stream": {"score": 100, "level": "CRITICAL", "base": 80, "activity": 0, "protective": 20,
                                    "concentration": 75, "commits": 4, "frustration": True, "keywords": ["free work"]},
                    "colors": {"score": 100, "level": "CRITICAL", "base": 100, "activity": 20, "protective": -5,
                              "concentration": 100, "commits": 0, "frustration": True, "keywords": ["protest", "exploitation"]},
                    "express": {"score": 0, "level": "VERY_LOW", "base": 60, "activity": -15, "protective": -75,
                               "concentration": 58, "commits": 45, "frustration": False, "keywords": []},
                    "lodash": {"score": 0, "level": "VERY_LOW", "base": 40, "activity": -30, "protective": -25,
                              "concentration": 35, "commits": 120, "frustration": False, "keywords": []},
                    "requests": {"score": 0, "level": "VERY_LOW", "base": 40, "activity": -15, "protective": -35,
                                "concentration": 45, "commits": 30, "frustration": False, "keywords": []},
                }

                if package_name.lower() in demo_results:
                    st.session_state.result = demo_results[package_name.lower()]
                    st.session_state.result_package = package_name
                else:
                    st.warning(f"Live analysis not available in demo. Try: event-stream, colors, express, lodash, requests")

    # Display results
    if "result" in st.session_state and st.session_state.get("result_package"):
        result = st.session_state.result
        pkg_name = st.session_state.result_package

        st.divider()

        # Score display
        col1, col2 = st.columns(2)

        with col1:
            st.plotly_chart(
                create_gauge(result["score"], pkg_name),
                use_container_width=True,
            )

        with col2:
            st.plotly_chart(
                create_breakdown_chart(
                    result["base"],
                    result["activity"],
                    result["protective"]
                ),
                use_container_width=True,
            )

        # Details
        st.subheader("Details")
        col1, col2, col3 = st.columns(3)

        with col1:
            level_colors = {
                "CRITICAL": "🔴", "HIGH": "🟠", "MODERATE": "🟡",
                "LOW": "🟢", "VERY_LOW": "🟢"
            }
            st.metric("Risk Level", f"{level_colors.get(result['level'], '')} {result['level']}")
            st.metric("Concentration", f"{result['concentration']}%")

        with col2:
            st.metric("Commits/Year", result["commits"])

        with col3:
            if result["frustration"]:
                st.error("⚠️ Frustration Signals")
                for kw in result["keywords"]:
                    st.write(f"• \"{kw}\"")
            else:
                st.success("✅ No Frustration Signals")

with tab2:
    st.header("Validation Results")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Performance Metrics")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Accuracy", "92.4%")
        m2.metric("Precision", "100%")
        m3.metric("Recall", "65%")
        m4.metric("F1", "0.79")

        st.plotly_chart(create_validation_chart(), use_container_width=True)

    with col2:
        st.subheader("T-1 Historical Analysis")
        st.markdown("Packages scored **before** their incidents:")

        st.dataframe({
            "Package": ["event-stream", "colors", "coa"],
            "Incident": ["Sept 2018", "Jan 2022", "Nov 2021"],
            "T-1 Score": [100, 100, 100],
            "Level": ["🔴 CRITICAL", "🔴 CRITICAL", "🔴 CRITICAL"],
            "Key Signal": ["'free work'", "'protest'", "abandoned"],
        }, use_container_width=True, hide_index=True)

        st.subheader("Control Comparison")
        st.dataframe({
            "Package": ["express"],
            "Score": [0],
            "Level": ["🟢 VERY_LOW"],
            "Why": ["Org-backed, 30 admins"],
        }, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Tool Comparison")

    col1, col2 = st.columns([1, 2])
    with col1:
        st.markdown("""
        | Tool | Focus |
        |------|-------|
        | **Ossuary** | Governance risk |
        | **Scorecard** | Security practices |
        | **CHAOSS** | Community health |
        """)

    with col2:
        st.plotly_chart(create_comparison_chart(), use_container_width=True)

with tab3:
    st.header("Scoring Methodology")

    st.latex(r"\text{Score} = \text{Base Risk} + \text{Activity Modifier} + \text{Protective Factors}")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Base Risk")
        st.dataframe({
            "Concentration": ["<30%", "30-49%", "50-69%", "70-89%", "≥90%"],
            "Points": [20, 40, 60, 80, 100],
            "Risk": ["Low", "Moderate", "Elevated", "High", "Critical"],
        }, use_container_width=True, hide_index=True)

        st.subheader("Activity Modifier")
        st.dataframe({
            "Commits/Year": [">50", "12-50", "4-11", "<4"],
            "Points": ["-30", "-15", "0", "+20"],
        }, use_container_width=True, hide_index=True)

    with col2:
        st.subheader("Protective Factors")
        st.dataframe({
            "Factor": [
                "Tier-1 Reputation",
                "GitHub Sponsors",
                "Organization (3+ admins)",
                "High Visibility (>50M/wk)",
                "Frustration Detected",
            ],
            "Points": ["-25", "-15", "-15", "-20", "+20"],
        }, use_container_width=True, hide_index=True)

        st.subheader("Detection Scope")
        st.markdown("""
        ✅ **Detects**: Abandonment, concentration risk, frustration signals

        ❌ **Cannot detect**: Account compromise, typosquatting, insider threats
        """)

# Footer
st.divider()
col1, col2, col3 = st.columns(3)
col1.caption("Ossuary v0.1.1")
col2.caption("[GitHub](https://github.com/anicka-net/ossuary-risk)")
col3.caption("[PyPI](https://pypi.org/project/ossuary-risk/)")
