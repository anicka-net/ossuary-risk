#!/usr/bin/env python3
"""Pin the xz-utils cutoff sweep through the complete frozen v6.4.3 scorer.

Thesis §3.1 claims, for tukaani-project/xz under methodology v6.4.3:
  2024-01-01: score 80, responsibility shift about +60 pp;
  2024-03-01: score 80, shift about +53 pp;
  first firing: 2023-04-01 (+33 pp); at 2023-03-01 the rule does not fire
  (score 50).
An independent offline replay suggested ~+30.3 pp at 2023-03-01, which
would be above the >30 pp rule threshold — so the March/April boundary
claim is only trustworthy if checked through the *complete* frozen
scorer (taper windows, maturity gate, <10% historical-share guard,
name-merged identity guard, mega-repo tenure guard, org-continuity
guard, bot exclusion, activity suppression).

This script scores each cutoff via the same frozen code path as the
validation runner (cached_collect cache-only replay of the August 2026
snapshot, then calculate_score_for_date with is_historical=True), and
additionally re-derives the takeover internals from the same
GitCollector.calculate_metrics call so the guard situation is recorded
per cutoff.

Output: thesis/xz_cutoff_sweep.json
No network: cache-only, snapshot_collected_before 2026-08-15T18:48:00Z.
"""
import asyncio
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from ossuary._compat import parse_utc_date_end  # noqa: E402
from ossuary.collectors.git import (  # noqa: E402
    GitCollector,
    _domain_org_key,
    _GENERIC_EMAIL_DOMAINS,
    _normalize_email,
)
from ossuary.scoring import METHODOLOGY_VERSION  # noqa: E402
from ossuary.scoring.engine import RiskScorer  # noqa: E402
from ossuary.services.repo_cache import COLLECTOR_VERSION  # noqa: E402
from ossuary.services.scorer import (  # noqa: E402
    cached_collect,
    calculate_score_for_date,
)

SNAPSHOT_BEFORE = datetime(2026, 8, 15, 18, 48, 0)
NAME = "tukaani-project/xz"
ECOSYSTEM = "github"
REPO_URL = "https://github.com/tukaani-project/xz"
CUTOFFS = [
    "2023-01-01", "2023-02-01", "2023-02-15", "2023-03-01",
    "2023-03-15", "2023-04-01", "2023-05-01", "2023-06-01",
    "2023-09-01", "2023-12-01", "2024-01-01", "2024-03-01",
]
# Daily probes across March 2023 to locate the exact first-firing day
# (the monthly grid shows no fire at 2023-03-01, +29.2 pp, and a fire at
# 2023-03-15, +30.7 pp; the crossing happens between them).
BOUNDARY_DAILY = [f"2023-03-{d:02d}" for d in range(2, 15)]


def _candidate_scan(commits, cutoff):
    """Replicate the takeover-candidate evaluation from
    GitCollector.calculate_metrics for every recent-window identity,
    recording each guard's verdict. The authoritative firing still comes
    from calculate_metrics; this scan exists to explain *why* a raw
    shift above 30 pp does or does not fire.
    """
    recent = [c for c in commits
              if cutoff - timedelta(days=365) <= c.authored_date <= cutoff]
    taper_start = cutoff - timedelta(days=425)
    hist = [c for c in commits if c.authored_date < taper_start]
    hist_counts = defaultdict(int)
    hist_names = {}
    for c in hist:
        e = _normalize_email(c.author_email)
        hist_counts[e] += 1
        hist_names[e] = c.author_name
    name_to_hist = defaultdict(int)
    for e, n in hist_counts.items():
        nm = hist_names.get(e, "").strip().lower()
        if nm:
            name_to_hist[nm] += n
    total_recent = len(recent)
    hist_total = len(hist)
    if total_recent == 0 or hist_total == 0:
        return []
    out = []
    for c in recent:
        pass  # identity enumeration below via recent counts
    recent_counts = defaultdict(int)
    recent_names = {}
    for c in recent:
        e = _normalize_email(c.author_email)
        recent_counts[e] += 1
        recent_names[e] = c.author_name
    for identity, rcount in recent_counts.items():
        name = recent_names.get(identity, "")
        guards = []
        if "[bot]" in identity.lower() or "[bot]" in name.lower():
            guards.append("bot-excluded")
        hist_pct = hist_counts.get(identity, 0) / hist_total * 100
        name_key = name.strip().lower()
        merged_pct = (name_to_hist.get(name_key, 0) / hist_total * 100
                      if name_key else 0)
        if hist_pct >= 10:
            guards.append(f"historical-share>={10:.0f}% ({hist_pct:.1f}%)")
        if merged_pct >= 10:
            guards.append(f"name-merged-share>=10% ({merged_pct:.1f}%)")
        hist_abs = hist_counts.get(identity, 0)
        tenure_years = None
        if hist_abs >= 100:
            own = sorted(
                [c.authored_date for c in hist
                 if _normalize_email(c.author_email) == identity])
            if own:
                tenure_years = (own[-1] - own[0]).days / 365.25
            if tenure_years is not None and tenure_years >= 4:
                guards.append(
                    f"mega-repo-tenure>=4y (hist={hist_abs}, {tenure_years:.1f}y)")
        if "@" in identity:
            dom = identity.split("@")[1]
            if dom not in _GENERIC_EMAIL_DOMAINS:
                org = _domain_org_key(dom)
                org_hist = sum(
                    n for e, n in hist_counts.items()
                    if "@" in e and e.split("@")[1] not in _GENERIC_EMAIL_DOMAINS
                    and _domain_org_key(e.split("@")[1]) == org)
                org_pct = org_hist / hist_total * 100
                if org_pct >= 30:
                    guards.append(f"org-continuity>=30% ({org_pct:.1f}%)")
        out.append({
            "identity": identity,
            "name": name,
            "recent_share_pct": rcount / total_recent * 100,
            "historical_share_pct": hist_pct,
            "raw_shift_pp": rcount / total_recent * 100 - hist_pct,
            "guards_triggered": guards,
            "passes_all_guards": not guards,
        })
    out.sort(key=lambda x: -x["raw_shift_pp"])
    return out


async def sweep_one(cs):
    """Score one cutoff through the frozen path; return the record."""
    cutoff = parse_utc_date_end(cs)
    collected, warnings = await cached_collect(
        NAME, ECOSYSTEM, REPO_URL,
        cutoff_date=cutoff,
        cache_only=True,
        snapshot_collected_before=SNAPSHOT_BEFORE,
    )
    if collected is None:
        return {"cutoff": cs, "error": warnings[0] if warnings else "no snapshot"}
    bd = calculate_score_for_date(
        NAME, ECOSYSTEM, collected, cutoff, is_historical=True)

    # Re-derive the takeover internals through the identical frozen
    # metrics path (both authored and committed timestamps <= cutoff).
    filtered = [
        c for c in collected.all_commits
        if c.authored_date <= cutoff and c.committed_date <= cutoff
    ]
    gm = GitCollector().calculate_metrics(filtered, cutoff)
    raw_activity = RiskScorer().calculate_activity_modifier(gm.commits_last_year)
    suppressed = (
        bd.protective_factors.takeover_risk_score > 0
        and raw_activity < 0
        and bd.activity_modifier == 0
    )
    rec = {
        "cutoff": cs,
        "cutoff_datetime": cutoff.isoformat(),
        "final_score": bd.final_score,
        "risk_level": bd.risk_level.value,
        "responsibility_shift_fired": bd.protective_factors.takeover_risk_score > 0,
        "takeover_evidence": bd.protective_factors.takeover_risk_evidence,
        "takeover_shift_pp_metrics": gm.takeover_shift,
        "takeover_suspect_identity": gm.takeover_suspect,
        "takeover_suspect_name": gm.takeover_suspect_name,
        "takeover_suspect_tenure_years": gm.takeover_suspect_tenure_years,
        "is_mature": gm.is_mature,
        "repo_age_years": round(gm.repo_age_years, 2),
        "commits_last_year_human": gm.commits_last_year,
        "top_contributor_concentration_pct": round(gm.maintainer_concentration, 2),
        "bus_factor": gm.bus_factor,
        "base_risk": bd.base_risk,
        "activity_modifier_raw": raw_activity,
        "activity_modifier_final": bd.activity_modifier,
        "activity_modifier_suppressed_by_shift": suppressed,
        "protective_factors_total": bd.protective_factors.total,
        "candidate_scan_top5": _candidate_scan(filtered, cutoff)[:5],
    }
    print(f"{cs}: score {bd.final_score} ({bd.risk_level.value}), "
          f"shift fired={rec['responsibility_shift_fired']}, "
          f"metrics shift={gm.takeover_shift:.1f}pp, "
          f"suspect={gm.takeover_suspect_name or gm.takeover_suspect}")
    return rec


async def main():
    try:
        git_commit = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        git_commit = None

    records = []
    for cs in CUTOFFS:
        records.append(await sweep_one(cs))

    boundary = []
    for cs in BOUNDARY_DAILY:
        boundary.append(await sweep_one(cs))

    payload = {
        "purpose": "xz-utils cutoff sweep through the complete frozen v6.4.3 scorer",
        "methodology_version": METHODOLOGY_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "git_commit": git_commit,
        "generated_utc": datetime.utcnow().isoformat() + "Z",
        "generation_command": (
            ".venv/bin/python scripts/xz_cutoff_sweep.py "
            "(output: thesis/xz_cutoff_sweep.json)"
        ),
        "source_provenance": {
            "package": NAME,
            "ecosystem": ECOSYSTEM,
            "repo_url": REPO_URL,
            "evidence": (
                "frozen snapshot cache, cache-only replay, no network; "
                "snapshot_collected_before 2026-08-15T18:48:00Z; the xz "
                "snapshot is the same collector-v5 evidence used by the "
                "canonical v6.4.3 validation run"
            ),
            "cutoff_semantics": "end of the named UTC day (parse_utc_date_end)",
            "historical_scoring": (
                "commits filtered to authored_date AND committed_date <= "
                "cutoff (v6.4.2 isolation); is_historical=True"
            ),
        },
        "cutoffs": records,
        "boundary_refinement_daily_march_2023": boundary,
    }
    out = REPO / "thesis" / "xz_cutoff_sweep.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
