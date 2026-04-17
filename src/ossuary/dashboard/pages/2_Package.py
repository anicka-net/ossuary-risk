"""Package detail — deep dive on a single package."""

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ossuary.db.session import init_db
from ossuary.services.scorer import score_package, get_historical_scores
from ossuary.dashboard.utils import (
    apply_style, run_async, get_all_tracked_packages,
    get_comparison_packages, get_score_history,
    risk_color, risk_badge, risk_level_str, COLORS, VERSION,
)

st.set_page_config(page_title="Ossuary — Package", layout="wide", initial_sidebar_state="collapsed")
apply_style()

@st.cache_resource
def _init_db():
    init_db()
    return True

_init_db()

st.markdown(
    '<h1 style="margin-bottom:0;color:#173f4f;">Package</h1>'
    '<p style="color:#6c757d;margin-top:0;">Detailed risk analysis</p>',
    unsafe_allow_html=True,
)
st.divider()


# -- Package selection --

# Check query params (from Score page link)
qp_name = st.query_params.get("name", "")
qp_eco = st.query_params.get("eco", "")

all_pkgs = get_all_tracked_packages()
pkg_names = sorted(set(f"{p['name']} ({p['ecosystem']})" for p in all_pkgs if p["score"] is not None))

# Pre-select from query params if available
default_idx = 0
if qp_name and qp_eco:
    target = f"{qp_name} ({qp_eco})"
    if target in pkg_names:
        default_idx = pkg_names.index(target) + 1  # +1 for "" at index 0

# Tracked-package selector. The label already shows "name (ecosystem)" so
# the ecosystem dropdown is shown only when a *new* (untracked) name is
# being entered below — otherwise it's redundant and confusing (you can
# pick "pyyaml (pypi)" while the dropdown shows "npm").
selected = st.selectbox(
    "Select a tracked package, or type a new name below",
    [""] + pkg_names,
    index=default_idx,
    label_visibility="collapsed",
    placeholder="Select tracked package...",
)

ecosystems = ["npm", "pypi", "cargo", "rubygems", "packagist", "nuget", "go", "github"]
default_eco = ecosystems.index(qp_eco) if qp_eco in ecosystems else 0

new_name = st.text_input(
    "Or enter package name",
    value=qp_name if qp_name and default_idx == 0 else "",
    placeholder="e.g., lodash, owner/repo",
    label_visibility="collapsed",
)

# Show the ecosystem picker only when the user typed a fresh name —
# tracked-package picks already carry their ecosystem in the label.
new_eco = default_eco_value = ecosystems[default_eco]
if new_name and not selected:
    new_eco = st.selectbox(
        "Ecosystem",
        ecosystems,
        index=default_eco,
        key="pkg_eco",
        label_visibility="visible",
    )

# Determine which package to show
pkg_name = None
pkg_eco = None

if new_name:
    pkg_name = new_name.strip()
    pkg_eco = new_eco
elif selected:
    # Parse "name (ecosystem)" format
    parts = selected.rsplit(" (", 1)
    pkg_name = parts[0]
    pkg_eco = parts[1].rstrip(")") if len(parts) > 1 else "npm"


if not pkg_name:
    st.caption("Select or enter a package to analyze.")
    st.divider()
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.page_link("app.py", label="Home")
    with col2:
        st.page_link("pages/1_Ecosystems.py", label="Browse ecosystems")
    with col3:
        st.page_link("pages/3_Score.py", label="Score a package")
    with col4:
        st.page_link("pages/4_Methodology.py", label="Methodology")
    st.caption(f"Ossuary v{VERSION} · [source](https://github.com/anicka-net/ossuary-risk)")
    st.stop()


# -- Score the package --

@st.cache_data(ttl=3600, show_spinner=False)
def _score(name, eco):
    return run_async(score_package(name, eco))

with st.status(f"Analyzing {pkg_name}...", expanded=False) as status:
    result = _score(pkg_name, pkg_eco)
    if result.success:
        status.update(label=f"{pkg_name} scored", state="complete")
    else:
        status.update(label=f"Error: {result.error}", state="error")

if not result.success or not result.breakdown:
    st.error(f"Could not score package: {result.error}")
    st.divider()
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.page_link("app.py", label="Home")
    with col2:
        st.page_link("pages/1_Ecosystems.py", label="Browse ecosystems")
    with col3:
        st.page_link("pages/3_Score.py", label="Score a package")
    with col4:
        st.page_link("pages/4_Methodology.py", label="Methodology")
    st.caption(f"Ossuary v{VERSION} · [source](https://github.com/anicka-net/ossuary-risk)")
    st.stop()

if result.warnings:
    for w in result.warnings:
        st.caption(f"Note: {w}")

b = result.breakdown

# INSUFFICIENT_DATA short-circuit: don't render the per-component panels
# from a breakdown that has no real numbers to show. Surface the failing
# inputs and the recovery path instead.
#
# `risk_level_str` deliberately avoids ``RiskLevel.INSUFFICIENT_DATA`` —
# see that helper for the Streamlit module-cache reasoning.
risk_value = risk_level_str(b.risk_level)

if risk_value == "INSUFFICIENT_DATA":
    st.divider()
    st.markdown(
        f'<div style="display:flex;align-items:baseline;gap:16px;">'
        f'<span style="font-size:2.2em;font-family:monospace;font-weight:700;color:#6c757d;">⚪</span>'
        f'<span style="font-size:1.4em;color:#6c757d;font-weight:600;">INSUFFICIENT DATA</span>'
        f'<span style="color:#6c757d;font-size:0.95em;">{pkg_name} · {pkg_eco}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.write("")
    st.warning(
        "No score was computed for this package. The methodology requires "
        "complete input data; one or more upstream fetches failed."
    )
    if b.incomplete_reasons:
        st.markdown("**Failed inputs:**")
        for reason in b.incomplete_reasons:
            st.markdown(f"- `{reason}`")
    st.info(
        "**To recover:** retry from the CLI later, or run "
        "`ossuary rescore-invalid` to retry every package currently in this state."
    )
    st.stop()

score = b.final_score
level = risk_value
color = risk_color(level)

st.divider()

# -- Score header --

st.markdown(
    f'<div style="display:flex;align-items:baseline;gap:16px;">'
    f'<span style="font-size:2.5em;font-family:monospace;font-weight:700;color:{color};">{score}</span>'
    f'<span style="font-size:1.2em;color:{color};">{level}</span>'
    f'<span style="color:#6c757d;font-size:0.95em;">{pkg_name} · {pkg_eco}</span>'
    f'</div>',
    unsafe_allow_html=True,
)

st.markdown("")

# -- Breakdown metrics --

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Base risk", b.base_risk)
col2.metric("Activity", f"{b.activity_modifier:+d}")
col3.metric("Protective", f"{b.protective_factors.total:+d}")
col4.metric("Concentration", f"{b.maintainer_concentration:.0f}%")
col5.metric("Commits/yr", b.commits_last_year)

# -- Waterfall chart --

categories = ["Base Risk", "Activity", "Protective", "Final"]
final = max(0, min(100, b.base_risk + b.activity_modifier + b.protective_factors.total))

fig_waterfall = go.Figure(go.Waterfall(
    orientation="v",
    measure=["relative", "relative", "relative", "total"],
    x=categories,
    y=[b.base_risk, b.activity_modifier, b.protective_factors.total, 0],
    text=[f"+{b.base_risk}", f"{b.activity_modifier:+d}", f"{b.protective_factors.total:+d}", str(final)],
    textposition="outside",
    connector={"line": {"color": "#bdc3c7"}},
    decreasing={"marker": {"color": COLORS["low"]}},
    increasing={"marker": {"color": COLORS["critical"]}},
    totals={"marker": {"color": color}},
))
fig_waterfall.update_layout(
    showlegend=False,
    height=280,
    margin=dict(l=40, r=20, t=20, b=40),
    plot_bgcolor="white",
    yaxis_title="Points",
)

st.plotly_chart(fig_waterfall, use_container_width=True)

# -- Explanation --

if b.explanation:
    st.markdown(f"**Analysis:** {b.explanation}")

if b.recommendations:
    with st.expander("Recommendations"):
        for rec in b.recommendations:
            st.markdown(f"- {rec}")

# Takeover risk alert
if b.protective_factors.takeover_risk_score > 0:
    st.markdown(
        f'<div style="padding:8px 12px;background:{COLORS["bg_critical"]};'
        f'border-left:3px solid {COLORS["critical"]};border-radius:2px;margin:8px 0;">'
        f'<strong>Takeover risk detected</strong> (+{b.protective_factors.takeover_risk_score} points)'
        f'</div>',
        unsafe_allow_html=True,
    )
    if b.protective_factors.takeover_risk_evidence:
        st.caption(f'  {b.protective_factors.takeover_risk_evidence}')

# Frustration signals
if b.protective_factors.frustration_score > 0:
    st.markdown(
        f'<div style="padding:8px 12px;background:{COLORS["bg_critical"]};'
        f'border-left:3px solid {COLORS["critical"]};border-radius:2px;margin:8px 0;">'
        f'<strong>Frustration signals detected</strong> (+{b.protective_factors.frustration_score} points)'
        f'</div>',
        unsafe_allow_html=True,
    )
    if b.protective_factors.frustration_evidence:
        for kw in b.protective_factors.frustration_evidence[:5]:
            st.caption(f'  "{kw}"')

# Maturity info
if b.protective_factors.maturity_evidence:
    st.markdown(
        f'<div style="padding:8px 12px;background:{COLORS["bg_low"]};'
        f'border-left:3px solid {COLORS["low"]};border-radius:2px;margin:8px 0;">'
        f'<strong>Mature project</strong>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.caption(f'  {b.protective_factors.maturity_evidence}')

# CHAOSS governance signals
if hasattr(b, "bus_factor") and (b.bus_factor > 0 or getattr(b, "elephant_factor", 0) > 0):
    with st.expander("Governance Signals (CHAOSS-aligned)"):
        gs_cols = st.columns(3)
        if b.bus_factor > 0:
            bf_color = "🔴" if b.bus_factor <= 2 else "🟡" if b.bus_factor <= 5 else "🟢"
            gs_cols[0].metric("Bus Factor", f"{bf_color} {b.bus_factor}", help="Minimum contributors for 50% of commits")
        ef = getattr(b, "elephant_factor", 0)
        if ef > 0:
            ef_color = "🔴" if ef <= 1 else "🟡" if ef <= 2 else "🟢"
            gs_cols[1].metric("Elephant Factor", f"{ef_color} {ef}", help="Minimum organizations for 50% of commits")
        ir = getattr(b, "inactive_contributor_ratio", 0)
        if ir > 0.1:
            ir_color = "🔴" if ir > 0.7 else "🟡" if ir > 0.4 else "🟢"
            gs_cols[2].metric("Contributor Attrition", f"{ir_color} {ir:.0%}", help="Fraction of lifetime contributors inactive in last year")

st.divider()

# -- Comparison --

st.markdown("#### Comparison")

comp = get_comparison_packages(pkg_name, pkg_eco, score)

cols = st.columns(3)
with cols[0]:
    if comp["safe"]:
        s = comp["safe"]
        sc = s["score"]
        sl = s["risk_level"] or "LOW"
        st.markdown(
            f'<div style="padding:12px;border:1px solid #ecf0f1;border-radius:4px;'
            f'border-left:3px solid {COLORS["low"]};">'
            f'<span style="color:#6c757d;font-size:0.8em;">Nearest safe</span><br>'
            f'<strong>{s["name"]}</strong><br>'
            f'<span style="font-family:monospace;color:{risk_color(sl)};">{sc}</span> {sl}'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption("No safe comparison available")

with cols[1]:
    st.markdown(
        f'<div style="padding:12px;border:1px solid #ecf0f1;border-radius:4px;'
        f'border-left:3px solid {color};background:{COLORS.get("bg_" + level.lower().replace(" ","_"), "#f8f9fa")};">'
        f'<span style="color:#6c757d;font-size:0.8em;">This package</span><br>'
        f'<strong>{pkg_name}</strong><br>'
        f'<span style="font-family:monospace;color:{color};">{score}</span> {level}'
        f'</div>',
        unsafe_allow_html=True,
    )

with cols[2]:
    if comp["risky"]:
        r = comp["risky"]
        rc = r["score"]
        rl = r["risk_level"] or "HIGH"
        st.markdown(
            f'<div style="padding:12px;border:1px solid #ecf0f1;border-radius:4px;'
            f'border-left:3px solid {COLORS["critical"]};">'
            f'<span style="color:#6c757d;font-size:0.8em;">Nearest risky</span><br>'
            f'<strong>{r["name"]}</strong><br>'
            f'<span style="font-family:monospace;color:{risk_color(rl)};">{rc}</span> {rl}'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption("No risky comparison available")

st.divider()

# -- Historical chart --

st.markdown("#### Score history")

history = get_score_history(pkg_name, pkg_eco)

if history and len(history) > 1:
    df = pd.DataFrame(history)

    fig_hist = go.Figure()
    fig_hist.add_trace(go.Scatter(
        x=df["date"],
        y=df["score"],
        mode="lines+markers",
        line=dict(color=COLORS["text"], width=2),
        marker=dict(size=5, color=color),
        hovertemplate="<b>%{x}</b><br>Score: %{y}<extra></extra>",
    ))

    fig_hist.add_hrect(y0=0, y1=40, fillcolor=COLORS["bg_low"], opacity=0.3, line_width=0)
    fig_hist.add_hrect(y0=40, y1=60, fillcolor=COLORS["bg_moderate"], opacity=0.3, line_width=0)
    fig_hist.add_hrect(y0=60, y1=80, fillcolor=COLORS["bg_high"], opacity=0.3, line_width=0)
    fig_hist.add_hrect(y0=80, y1=100, fillcolor=COLORS["bg_critical"], opacity=0.3, line_width=0)
    fig_hist.add_hline(y=60, line_dash="dot", line_color=COLORS["high"],
                       annotation_text="risk threshold", annotation_position="right")

    fig_hist.update_layout(
        yaxis=dict(range=[0, 105], title="Score"),
        xaxis_title="Date",
        height=350,
        margin=dict(l=40, r=20, t=20, b=40),
        hovermode="x unified",
        plot_bgcolor="white",
    )
    st.plotly_chart(fig_hist, use_container_width=True)

    with st.expander("Raw data"):
        st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.caption("No historical data yet. Run historical analysis to generate monthly snapshots.")

    if st.button("Calculate 24-month history", type="primary"):
        progress_bar = st.progress(0)
        status_text = st.empty()

        def update_progress(current, total):
            progress_bar.progress(current / total)
            status_text.text(f"Month {current}/{total}...")

        scores, warnings = run_async(
            get_historical_scores(pkg_name, pkg_eco, months=24, progress_callback=update_progress)
        )

        progress_bar.empty()
        status_text.empty()

        if scores:
            st.success(f"Calculated {len(scores)} monthly snapshots.")
            st.rerun()
        else:
            st.error("Failed to calculate history.")
            for w in warnings:
                st.caption(w)

st.divider()
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.page_link("app.py", label="Home")
with col2:
    st.page_link("pages/1_Ecosystems.py", label="Browse ecosystems")
with col3:
    st.page_link("pages/3_Score.py", label="Score a package")
with col4:
    st.page_link("pages/4_Methodology.py", label="Methodology")

st.caption(f"Ossuary v{VERSION} · [source](https://github.com/anicka-net/ossuary-risk)")
