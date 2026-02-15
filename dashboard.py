"""Ossuary Risk Dashboard - Streamlit visualization for OSS supply chain risk."""

import asyncio
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from ossuary.db.session import init_db
from ossuary.services.scorer import score_package, get_historical_scores, HistoricalScore

# Set page config first
st.set_page_config(
    page_title="Ossuary - OSS Supply Chain Risk",
    page_icon="💀",
    layout="wide",
)


# Initialize database on startup
@st.cache_resource
def initialize_database():
    """Initialize database once on startup."""
    init_db()
    return True


initialize_database()


def run_async(coro):
    """Run async coroutine in Streamlit context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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

    fig.add_trace(go.Bar(
        name="event-stream",
        x=tools,
        y=[100, 76],
        marker_color="#dc3545",
    ))

    fig.add_trace(go.Bar(
        name="express",
        x=tools,
        y=[0, 18],
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


def create_historical_chart(df: pd.DataFrame, package: str) -> go.Figure:
    """Create historical score evolution chart."""
    fig = go.Figure()

    # Add score line
    fig.add_trace(go.Scatter(
        x=df["date"],
        y=df["score"],
        mode="lines+markers",
        name="Risk Score",
        line=dict(color="#dc3545", width=3),
        marker=dict(size=8),
        hovertemplate="<b>%{x}</b><br>Score: %{y}<extra></extra>",
    ))

    # Add risk level bands
    fig.add_hrect(y0=0, y1=40, fillcolor="#d4edda", opacity=0.3, line_width=0)
    fig.add_hrect(y0=40, y1=60, fillcolor="#fff3cd", opacity=0.3, line_width=0)
    fig.add_hrect(y0=60, y1=80, fillcolor="#ffe5d0", opacity=0.3, line_width=0)
    fig.add_hrect(y0=80, y1=100, fillcolor="#f8d7da", opacity=0.3, line_width=0)

    # Add threshold line
    fig.add_hline(y=60, line_dash="dot", line_color="orange",
                  annotation_text="Risk Threshold (60)", annotation_position="right")

    fig.update_layout(
        title=f"📈 {package} - Risk Score Evolution (24 months)",
        xaxis_title="Date",
        yaxis_title="Risk Score",
        yaxis=dict(range=[0, 105]),
        height=400,
        margin=dict(l=20, r=20, t=50, b=20),
        hovermode="x unified",
    )

    return fig


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

# Check for GITHUB_TOKEN
if not os.getenv("GITHUB_TOKEN"):
    st.sidebar.warning("⚠️ GITHUB_TOKEN not set. Results may be limited.")

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
tab1, tab2, tab_watch, tab3, tab4 = st.tabs([
    "📊 Score Package",
    "📈 Historical Analysis",
    "⚠️ Watchlist",
    "✅ Validation Results",
    "📚 Methodology"
])

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
        ecosystems = ["npm", "pypi", "cargo", "rubygems", "packagist", "nuget", "go", "github"]
        default_eco = st.session_state.get("ecosystem", "npm")
        eco_index = ecosystems.index(default_eco) if default_eco in ecosystems else 0

        package_name = st.text_input(
            "Package Name",
            value=st.session_state.get("package", ""),
            placeholder="e.g., lodash (npm/pypi) or owner/repo (github)"
        )
        ecosystem = st.selectbox(
            "Ecosystem",
            ecosystems,
            index=eco_index
        )
        cutoff_input = st.text_input(
            "Cutoff Date (optional)",
            placeholder="YYYY-MM-DD for T-1 analysis"
        )

    if st.button("🔍 Calculate Risk Score", type="primary", use_container_width=True):
        if not package_name:
            st.error("Please enter a package name")
        else:
            # Parse cutoff date if provided
            cutoff_date = None
            if cutoff_input:
                try:
                    cutoff_date = datetime.strptime(cutoff_input, "%Y-%m-%d")
                except ValueError:
                    st.error("Invalid date format. Use YYYY-MM-DD")
                    st.stop()

            with st.spinner(f"Analyzing {package_name}... (this may take a minute for first-time analysis)"):
                result = run_async(score_package(package_name, ecosystem, cutoff_date=cutoff_date))

                if result.success and result.breakdown:
                    breakdown = result.breakdown
                    st.session_state.result = {
                        "score": breakdown.final_score,
                        "level": breakdown.risk_level.value,
                        "base": breakdown.base_risk,
                        "activity": breakdown.activity_modifier,
                        "protective": breakdown.protective_factors.total,
                        "concentration": breakdown.maintainer_concentration,
                        "commits": breakdown.commits_last_year,
                        "frustration": breakdown.protective_factors.frustration_score > 0,
                        "keywords": breakdown.protective_factors.frustration_evidence[:3] if breakdown.protective_factors.frustration_evidence else [],
                        "explanation": breakdown.explanation,
                        "recommendations": breakdown.recommendations,
                    }
                    st.session_state.result_package = package_name

                    if result.warnings:
                        for warning in result.warnings:
                            st.warning(warning)
                else:
                    st.error(f"Failed to score package: {result.error}")

    # Display results
    if "result" in st.session_state and st.session_state.get("result_package"):
        result = st.session_state.result
        pkg_name = st.session_state.result_package

        st.divider()

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

        st.subheader("Details")
        col1, col2, col3 = st.columns(3)

        with col1:
            level_colors = {
                "CRITICAL": "🔴", "HIGH": "🟠", "MODERATE": "🟡",
                "LOW": "🟢", "VERY_LOW": "🟢"
            }
            st.metric("Risk Level", f"{level_colors.get(result['level'], '')} {result['level']}")
            st.metric("Concentration", f"{result['concentration']:.1f}%")

        with col2:
            st.metric("Commits/Year", result["commits"])

        with col3:
            if result["frustration"]:
                st.error("⚠️ Frustration Signals")
                for kw in result["keywords"]:
                    st.write(f"• \"{kw}\"")
            else:
                st.success("✅ No Frustration Signals")

        # Explanation and recommendations
        if "explanation" in result:
            st.markdown(f"**Explanation:** {result['explanation']}")

        if "recommendations" in result and result["recommendations"]:
            st.markdown("**Recommendations:**")
            for rec in result["recommendations"]:
                st.markdown(f"- {rec}")

with tab2:
    st.header("📈 Historical Score Evolution")

    st.markdown("""
    This view shows how risk scores evolved over time, going **backward from the most recent commit**.
    Enter any package or GitHub repo to see its 24-month score history.
    """)

    # Package input
    col1, col2 = st.columns([3, 1])
    with col1:
        hist_package = st.text_input(
            "Package Name",
            value="",
            placeholder="e.g., lodash (npm), requests (pypi), or owner/repo (github)",
            key="hist_package_input"
        )
    with col2:
        hist_ecosystem = st.selectbox(
            "Ecosystem",
            ["npm", "pypi", "cargo", "rubygems", "packagist", "nuget", "go", "github"],
            key="hist_ecosystem"
        )

    if st.button("📈 Load Historical Data", type="primary", use_container_width=True):
        if not hist_package:
            st.error("Please enter a package name")
        else:
            # Progress tracking
            progress_bar = st.progress(0)
            status_text = st.empty()

            def update_progress(current, total):
                progress_bar.progress(current / total)
                status_text.text(f"Calculating month {current}/{total}...")

            with st.spinner(f"Calculating 24-month history for {hist_package}..."):
                historical_scores, warnings = run_async(
                    get_historical_scores(
                        hist_package,
                        hist_ecosystem,
                        months=24,
                        progress_callback=update_progress,
                    )
                )

                progress_bar.empty()
                status_text.empty()

                if warnings:
                    for warning in warnings:
                        st.warning(warning)

                if historical_scores:
                    st.session_state.historical_data = historical_scores
                    st.session_state.historical_package = hist_package
                else:
                    st.error("Failed to calculate historical scores")

    # Display historical data if available
    if "historical_data" in st.session_state and st.session_state.get("historical_package"):
        historical_scores = st.session_state.historical_data
        pkg_name = st.session_state.historical_package

        # Convert to DataFrame
        df = pd.DataFrame([
            {"date": hs.date, "score": hs.score, "level": hs.risk_level,
             "concentration": hs.concentration, "commits": hs.commits_year,
             "contributors": hs.contributors}
            for hs in historical_scores
        ])

        # Display chart
        st.plotly_chart(
            create_historical_chart(df, pkg_name),
            use_container_width=True,
        )

        # Stats row
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            st.metric("Current Score", f"{df['score'].iloc[-1]}")
        with col2:
            st.metric("Peak Score", f"{df['score'].max()}")
        with col3:
            # Months above threshold
            above_threshold = (df["score"] >= 60).sum()
            st.metric("Months at Risk (60+)", f"{above_threshold}")
        with col4:
            # Current risk level
            current_level = df['level'].iloc[-1]
            level_colors = {"CRITICAL": "🔴", "HIGH": "🟠", "MODERATE": "🟡", "LOW": "🟢", "VERY_LOW": "🟢"}
            st.metric("Current Level", f"{level_colors.get(current_level, '')} {current_level}")

        # Data table
        with st.expander("📋 View Raw Data"):
            st.dataframe(
                df[["date", "score", "level", "concentration", "commits", "contributors"]],
                use_container_width=True,
                hide_index=True,
            )

    st.divider()

    # Keep the validation summary for reference
    st.subheader("📊 Historical Analysis Summary (Validation Set)")

    st.markdown("""
    Our validation on known incidents shows that governance risks were detectable
    **months before** incidents occurred:
    """)

    summary_data = {
        "Package": ["event-stream", "colors", "coa", "express"],
        "Incident": ["Sept 2018", "Jan 2022", "Nov 2021", "None"],
        "First CRITICAL": ["Oct 2017", "Jan 2021", "Dec 2020", "Never"],
        "Warning Time": ["11 months", "12 months", "11 months", "N/A"],
        "Attack Type": ["Takeover", "Sabotage", "Takeover", "N/A"],
    }

    st.dataframe(summary_data, use_container_width=True, hide_index=True)

with tab_watch:
    st.header("⚠️ Risk Watchlist")

    st.markdown("""
    Packages with known governance risk signals. Scores are calculated live
    and cached for 7 days. High-download packages with high scores are prime
    targets for supply chain attacks.
    """)

    # Default watchlist - packages we've identified as risky
    DEFAULT_WATCHLIST = [
        ("inherits", "npm", "250M+ dl/wk, abandoned 12+ months"),
        ("minimist", "npm", "45M+ dl/wk, single maintainer, barely active"),
        ("atomicwrites", "pypi", "pytest dep chain, abandoned since 2021"),
        ("node-ipc", "npm", "5M+ dl/wk, post-protestware abandonment"),
        ("rc", "npm", "20M+ dl/wk, abandoned + frustration signals"),
        ("left-pad", "npm", "historic incident, abandoned"),
        ("coa", "npm", "compromised 2021, abandoned"),
        ("event-stream", "npm", "compromised 2018, abandoned"),
        ("colors", "npm", "sabotaged 2022, abandoned"),
        ("ua-parser-js", "npm", "compromised 2021, 95% concentration"),
    ]

    # Custom package input
    with st.expander("Add package to watchlist"):
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            custom_pkg = st.text_input("Package", placeholder="e.g., some-pkg or owner/repo", key="watch_pkg")
        with col2:
            custom_eco = st.selectbox("Ecosystem", ["npm", "pypi", "cargo", "rubygems", "packagist", "nuget", "go", "github"], key="watch_eco")
        with col3:
            custom_note = st.text_input("Note", placeholder="why watching?", key="watch_note")

        if st.button("Add to watchlist"):
            if custom_pkg:
                if "custom_watchlist" not in st.session_state:
                    st.session_state.custom_watchlist = []
                st.session_state.custom_watchlist.append((custom_pkg, custom_eco, custom_note or "custom"))

    # Combine default + custom
    watchlist = DEFAULT_WATCHLIST[:]
    if "custom_watchlist" in st.session_state:
        watchlist.extend(st.session_state.custom_watchlist)

    if st.button("🔄 Scan Watchlist", type="primary", use_container_width=True):
        progress_bar = st.progress(0)
        status_text = st.empty()

        results = []
        for i, (pkg, eco, note) in enumerate(watchlist):
            progress_bar.progress((i + 1) / len(watchlist))
            status_text.text(f"Scoring {pkg} ({eco})... ({i+1}/{len(watchlist)})")

            result = run_async(score_package(pkg, eco))
            if result.success:
                b = result.breakdown
                results.append({
                    "score": b.final_score,
                    "level": b.risk_level.value,
                    "package": pkg,
                    "ecosystem": eco,
                    "concentration": b.maintainer_concentration,
                    "commits_yr": b.commits_last_year,
                    "contributors": b.unique_contributors,
                    "frustration": b.protective_factors.frustration_score > 0,
                    "note": note,
                })
            else:
                results.append({
                    "score": -1,
                    "level": "ERROR",
                    "package": pkg,
                    "ecosystem": eco,
                    "concentration": 0,
                    "commits_yr": 0,
                    "contributors": 0,
                    "frustration": False,
                    "note": f"Error: {result.error[:40] if result.error else 'unknown'}",
                })

        progress_bar.empty()
        status_text.empty()

        # Sort by score descending
        results.sort(key=lambda r: r["score"], reverse=True)
        st.session_state.watchlist_results = results

    # Display results
    if "watchlist_results" in st.session_state:
        results = st.session_state.watchlist_results

        # Summary metrics
        valid = [r for r in results if r["score"] >= 0]
        critical = [r for r in valid if r["score"] >= 80]
        high = [r for r in valid if 60 <= r["score"] < 80]
        moderate = [r for r in valid if 40 <= r["score"] < 60]

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Packages Scanned", len(valid))
        col2.metric("🔴 Critical", len(critical))
        col3.metric("🟠 High", len(high))
        col4.metric("🟡 Moderate", len(moderate))

        st.divider()

        # Results table
        for r in results:
            score = r["score"]
            if score >= 80:
                icon = "🔴"
                color = "red"
            elif score >= 60:
                icon = "🟠"
                color = "orange"
            elif score >= 40:
                icon = "🟡"
                color = "yellow"
            elif score >= 0:
                icon = "🟢"
                color = "green"
            else:
                icon = "❌"
                color = "gray"

            frustration_flag = " 🗯️" if r["frustration"] else ""

            col1, col2, col3, col4 = st.columns([3, 1, 2, 4])
            with col1:
                st.markdown(f"**{r['package']}** ({r['ecosystem']})")
            with col2:
                if score >= 0:
                    st.markdown(f"{icon} **{score}**{frustration_flag}")
                else:
                    st.markdown(f"{icon} ERR")
            with col3:
                if score >= 0:
                    st.caption(f"{r['concentration']:.0f}% conc | {r['commits_yr']} cmts | {r['contributors']} ctrb")
            with col4:
                st.caption(r["note"])

        # Downloadable data
        with st.expander("📋 Export Data"):
            df = pd.DataFrame([r for r in results if r["score"] >= 0])
            if not df.empty:
                st.dataframe(
                    df[["package", "ecosystem", "score", "level", "concentration", "commits_yr", "contributors", "note"]],
                    use_container_width=True,
                    hide_index=True,
                )

with tab3:
    st.header("Validation Results")

    # Try to load latest validation results from JSON
    import glob as globmod
    import json

    results_files = sorted(globmod.glob(os.path.join(os.path.dirname(__file__), "validation_results*.json")))
    validation_data = None

    if results_files:
        latest_file = results_files[-1]
        try:
            with open(latest_file) as f:
                validation_data = json.load(f)
        except Exception:
            pass

    if validation_data:
        st.caption(f"From: {os.path.basename(latest_file)} ({validation_data.get('timestamp', 'unknown')[:10]})")

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Performance Metrics")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Accuracy", f"{validation_data.get('accuracy', 0)*100:.1f}%")
            m2.metric("Precision", f"{validation_data.get('precision', 0)*100:.0f}%")
            m3.metric("Recall", f"{validation_data.get('recall', 0)*100:.0f}%")
            m4.metric("F1", f"{validation_data.get('f1_score', 0):.2f}")

            # Dynamic confusion matrix from loaded data
            cm = validation_data.get("confusion_matrix", {})
            tn = cm.get("TN", 0)
            fp = cm.get("FP", 0)
            fn = cm.get("FN", 0)
            tp = cm.get("TP", 0)
            total = tn + fp + fn + tp

            fig_cm = go.Figure(
                data=go.Heatmap(
                    z=[[tn, fp], [fn, tp]],
                    x=["Predicted Safe", "Predicted Risky"],
                    y=["Actually Safe", "Actually Risky"],
                    text=[[f"TN: {tn}", f"FP: {fp}"], [f"FN: {fn}", f"TP: {tp}"]],
                    texttemplate="%{text}",
                    colorscale=[[0, "#d4edda"], [0.5, "#fff3cd"], [1, "#f8d7da"]],
                    showscale=False,
                )
            )
            fig_cm.update_layout(
                title=f"Confusion Matrix (n={total})",
                height=300,
                margin=dict(l=20, r=20, t=50, b=20),
            )
            st.plotly_chart(fig_cm, use_container_width=True)

        with col2:
            # By attack type
            st.subheader("By Attack Type")
            by_attack = validation_data.get("by_attack_type", {})
            if by_attack:
                attack_rows = []
                for atype, stats in by_attack.items():
                    pct = stats["correct"] / stats["total"] * 100 if stats["total"] > 0 else 0
                    attack_rows.append({
                        "Type": atype,
                        "Correct": f"{stats['correct']}/{stats['total']}",
                        "Rate": f"{pct:.0f}%",
                    })
                st.dataframe(pd.DataFrame(attack_rows), use_container_width=True, hide_index=True)

            # By ecosystem
            by_eco = validation_data.get("by_ecosystem", {})
            if by_eco:
                st.subheader("By Ecosystem")
                eco_names = []
                eco_correct = []
                eco_total = []
                for eco, stats in sorted(by_eco.items()):
                    eco_names.append(eco)
                    eco_correct.append(stats["correct"])
                    eco_total.append(stats["total"])

                fig_eco = go.Figure()
                fig_eco.add_trace(go.Bar(
                    name="Correct",
                    x=eco_names,
                    y=eco_correct,
                    marker_color="#28a745",
                ))
                fig_eco.add_trace(go.Bar(
                    name="Total",
                    x=eco_names,
                    y=eco_total,
                    marker_color="#dee2e6",
                ))
                fig_eco.update_layout(
                    barmode="overlay",
                    height=250,
                    margin=dict(l=20, r=20, t=20, b=20),
                    legend=dict(orientation="h"),
                )
                st.plotly_chart(fig_eco, use_container_width=True)

        # Individual results
        st.divider()
        results = validation_data.get("results", [])
        if results:
            st.subheader("All Results")

            # Filter controls
            show_filter = st.radio(
                "Show:", ["All", "Incidents Only", "Controls Only", "Errors/FN Only"],
                horizontal=True, key="val_filter",
            )

            result_rows = []
            for r in results:
                case = r.get("case", {})
                error = r.get("error")
                if show_filter == "Incidents Only" and case.get("expected_outcome") != "incident":
                    continue
                if show_filter == "Controls Only" and case.get("expected_outcome") != "safe":
                    continue
                if show_filter == "Errors/FN Only" and r.get("classification") != "FN" and not error:
                    continue

                level = r.get("risk_level", "")
                semaphore = {"CRITICAL": "🔴", "HIGH": "🟠", "MODERATE": "🟡", "LOW": "🟢", "VERY_LOW": "🟢"}.get(level, "")

                result_rows.append({
                    "Package": case.get("name", ""),
                    "Ecosystem": case.get("ecosystem", ""),
                    "Expected": case.get("expected_outcome", ""),
                    "Score": r.get("score", "ERR") if not error else "ERR",
                    "Level": f"{semaphore} {level}" if not error else error[:30],
                    "Class": r.get("classification", ""),
                    "Correct": "Y" if r.get("correct") else ("ERR" if error else "N"),
                })

            if result_rows:
                st.dataframe(pd.DataFrame(result_rows), use_container_width=True, hide_index=True)
    else:
        # Fallback: hardcoded summary when no JSON available
        st.info("No validation results JSON found. Run `python scripts/validate.py -o validation_results.json` to generate.")

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

with tab4:
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
