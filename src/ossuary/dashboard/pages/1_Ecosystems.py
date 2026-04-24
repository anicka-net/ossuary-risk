"""Ecosystem overview — browse tracked packages by ecosystem."""

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import streamlit as st

from ossuary.db.session import init_db
from ossuary.dashboard.utils import (
    apply_style, get_packages_by_ecosystem, get_ecosystem_summary,
    get_unscored_packages, rescore_packages, retry_packages,
    risk_color, risk_badge, COLORS, VERSION,
)

st.set_page_config(page_title="Ossuary — Ecosystems", layout="wide", initial_sidebar_state="collapsed")
apply_style()

@st.cache_resource
def _init_db():
    init_db()
    return True

_init_db()

st.markdown(
    '<h1 style="margin-bottom:0;color:#173f4f;">Ecosystems</h1>'
    '<p style="color:#6c757d;margin-top:0;">Browse tracked packages by ecosystem</p>',
    unsafe_allow_html=True,
)
st.divider()

# -- Ecosystem selector --

eco_summary = get_ecosystem_summary()
ecosystems = sorted(eco_summary.keys()) if eco_summary else []

if not ecosystems:
    st.info("No packages tracked yet. Score some packages first.")
    st.divider()
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.page_link("app.py", label="Home")
    with col2:
        st.page_link("pages/3_Score.py", label="Score a package")
    with col3:
        st.page_link("pages/2_Package.py", label="Package detail")
    with col4:
        st.page_link("pages/4_Methodology.py", label="Methodology")
    st.caption(f"Ossuary v{VERSION} · [source](https://github.com/anicka-net/ossuary-risk)")
    st.stop()

qp_eco = st.query_params.get("eco", "")
default_idx = ecosystems.index(qp_eco) if qp_eco in ecosystems else 0

selected = st.selectbox("Ecosystem", ecosystems, index=default_idx, label_visibility="collapsed")

if not selected:
    st.stop()

# -- Summary stats --

data = eco_summary[selected]
col1, col2, col3, col4 = st.columns(4)
col1.metric("Packages", data["count"])
col2.metric("Avg score", f'{data["avg_score"]:.0f}')
col3.metric("Max score", data["max_score"])
col4.metric("At risk (60+)", data["critical"] + data["high"])

# -- Refresh / retry orphans --
# Surface packages that were registered but never successfully scored
# (last_analyzed=None), with a one-click retry. Also offer a re-score-all
# button for the ecosystem so a user can refresh stale scores from the UI.

unscored = get_unscored_packages(selected)
all_in_eco = get_packages_by_ecosystem(selected)

cta1, cta2, _ = st.columns([2, 2, 4])


def _show_result(result: dict, verb: str) -> None:
    if result["errors"]:
        st.warning(
            f"{result['success']} succeeded, {len(result['errors'])} "
            f"failed: " + ", ".join(n for n, _ in result["errors"][:5])
        )
    else:
        st.success(f"{verb} {result['success']} packages.")


with cta1:
    if unscored and st.button(
        f"Retry {len(unscored)} unscored",
        key=f"retry_unscored_{selected}",
        help="Bypass all caches (score, snapshot, negative) and re-attempt "
             "collection from scratch. Use this for packages stuck on a "
             "stale failure cache. Will not fix genuinely bad source data "
             "(e.g. a typo in the registry's repository URL).",
    ):
        with st.spinner(f"Retrying {len(unscored)} {selected} packages…"):
            result = retry_packages(unscored)
        _show_result(result, "Retried")
        st.rerun()

with cta2:
    if all_in_eco and st.button(
        f"Re-score all {len(all_in_eco)}",
        key=f"rescore_all_{selected}",
        help="Bypass the score cache so every package recomputes a fresh "
             "breakdown. Snapshot cache is still used where the SLA is "
             "good (so this is cheap on cache hits). Negative-cached "
             "failures are still respected — use the Retry button above "
             "to bypass those.",
    ):
        targets = [
            {"name": p["name"], "ecosystem": p["ecosystem"],
             "repo_url": p.get("repo_url") or None}
            for p in all_in_eco
        ]
        with st.spinner(f"Re-scoring {len(targets)} {selected} packages…"):
            result = rescore_packages(targets)
        _show_result(result, "Re-scored")
        st.rerun()

if unscored:
    st.caption(
        f"⚠ {len(unscored)} package{'s' if len(unscored) != 1 else ''} "
        f"registered but never scored — likely transient failures or "
        "stuck on a negative-cache entry. Click Retry to bypass caches "
        "and re-attempt."
    )

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

    # Render as clickable list instead of dataframe for navigation
    for _, row in df.iterrows():
        pkg_name = row["name"]
        eco = row["ecosystem"]
        score_val = int(row["score"])
        level = row["risk_level"] or ""
        conc = f'{row["concentration"]:.0f}%' if pd.notna(row["concentration"]) else "—"
        commits = str(int(row["commits_year"])) if pd.notna(row["commits_year"]) else "—"
        color = risk_color(level)

        # Tags for maturity/takeover
        tags = ""
        if row.get("has_takeover_risk"):
            tags += (
                f' <span style="background:{COLORS["bg_critical"]};color:{COLORS["critical"]};'
                f'padding:1px 6px;border-radius:3px;font-size:0.75em;font-weight:600;">'
                f'TAKEOVER</span>'
            )
        if row.get("is_mature"):
            tags += (
                f' <span style="background:{COLORS["bg_low"]};color:{COLORS["low"]};'
                f'padding:1px 6px;border-radius:3px;font-size:0.75em;">mature</span>'
            )

        c1, c2, c3, c4 = st.columns([3, 1, 2, 2])
        with c1:
            st.markdown(
                f'<a href="/Package?name={pkg_name}&eco={eco}" target="_self" '
                f'style="color:inherit;text-decoration:none;font-weight:600;">{pkg_name}</a>{tags}',
                unsafe_allow_html=True,
            )
        with c2:
            st.markdown(
                f'<span style="color:{color};font-family:monospace;font-weight:600;">'
                f'{score_val}</span> <span style="color:#6c757d;font-size:0.85em;">{level}</span>',
                unsafe_allow_html=True,
            )
        with c3:
            st.caption(f"concentration {conc}")
        with c4:
            st.caption(f"{commits} commits/yr")

    # -- Score distribution --

    st.divider()
    st.markdown("#### Score distribution")

    import plotly.graph_objects as go

    scores = df["score_display"].tolist()
    fig = go.Figure(go.Histogram(
        x=scores,
        xbins=dict(start=0, end=100, size=10),
        marker_color="#6c757d",
        marker_line_color="#173f4f",
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
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.page_link("app.py", label="Home")
with col2:
    st.page_link("pages/3_Score.py", label="Score a package")
with col3:
    st.page_link("pages/2_Package.py", label="Package detail")
with col4:
    st.page_link("pages/4_Methodology.py", label="Methodology")

st.caption(f"Ossuary v{VERSION} · [source](https://github.com/anicka-net/ossuary-risk)")
