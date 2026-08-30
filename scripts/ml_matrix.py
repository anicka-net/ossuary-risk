#!/usr/bin/env python3
"""Experiment 5 step 1 (v2): build the public diagnostic ML matrix.

Redesigned after pre-fit review. Two disjoint diagnostics:

  H — historical matched diagnostic: T1/T2/T3 incidents at their T-1 cutoffs,
      each matched to up to 4 controls from the same ecosystem whose repos
      already existed at that date (deterministic predeclared rule).
      Cutoff-safe features only (git history + stable org ownership +
      bound account tenure). NO current merge/issue/protective context.

  C — current-state diagnostic: 13 T_risk + 120 controls at the common
      August 2026 checkpoint. Full feature set. Exploratory (T_risk is
      hand-selected, n_pos=13).

T4/T5 rows are emitted for the boundary check only; they must be scored by
group-held-out models (no related row in training).

Grouping: connected components over leakage edges (same campaign / shared
credential, same repo lineage, same resolved GitHub maintainer login);
proxy-email edges only when no login resolves.

Anti-leakage: no Ossuary score/band/prediction/PF-total/reputation-composite/
tier-as-feature/notes. Explicit per-analysis feature whitelists below.
"""
# ruff: noqa: E402

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "benchmarks" / "ml_diagnostic_2026_08_29"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from validate import (  # noqa: E402
    VALIDATION_CASES,
    load_evidence_fixture,
    parse_utc_date_end,
)

from ossuary.collectors.git import GitCollector  # noqa: E402
from ossuary.sentiment.analyzer import SentimentAnalyzer  # noqa: E402
from ossuary.services.scorer import (  # noqa: E402
    _filter_issues_for_cutoff,
    _normalize_email,
    cached_collect,
)

REPLAY_INSTANT = datetime.fromisoformat("2026-08-15T15:49:38.364446+00:00")
SNAPSHOT_BEFORE = datetime.fromisoformat("2026-08-15T18:48:00+00:00")
IN_SCOPE = {"T1", "T2", "T3", "T_risk"}
HADES = {"gpsea", "ensmallen", "embiggen", "pyphetools",
         "ppkt2synergy", "phenopacket-store-toolkit"}
NPM_2021 = {"coa", "rc", "ua-parser-js"}
TEAMPCP = {"trivy-action", "litellm", "telnyx", "xinference", "@tanstack/router"}
MIASMA = {"@redhat-cloud-services/frontend-components"}

H_EXCLUSIONS = {
    ("github", "polyfillpolyfill/polyfill-library", "2024-02-01"): (
        "The retained repository lineage has no commits observable at the "
        "2024-02-01 cutoff, so it cannot support a valid historical feature row."
    ),
}

# ---- explicit feature whitelists (nothing outside these may enter a model) ----
# H: genuinely cutoff-reconstructable git-history observables only.
#    Removed after review: is_org_owned (current ownership not historically
#    stable), frustration_commit_count (zero variance in H), account_age_years
#    (+missing) — tenure available for only 14/144 rows; preserved in the CSV
#    for an explicitly labelled sensitivity run, but excluded from the
#    primary whitelist below.
H_FEATURES = [
    "top_contributor_concentration", "code_bus_factor", "unique_contributors",
    "commits_last_year", "zero_activity_flag", "repo_age_years",
    "lifetime_commit_count", "lifetime_contributors", "is_mature",
    "inactive_contributor_ratio",
    "takeover_shift_pp", "takeover_suspect_tenure_years",
    "frustration_commit_texts",
]
# Sensitivity-only columns (present in CSV, NOT in the primary H whitelist):
H_SENSITIVITY_FEATURES = ["account_age_years", "account_age_years_missing"]
C_FEATURES = H_FEATURES + H_SENSITIVITY_FEATURES + [
    "merge_bus_factor", "merge_bus_factor_missing", "merge_concentration",
    "frustration_commit_count",
    "frustration_issue_count", "frustration_issue_texts",
    "maintainer_public_repos", "maintainer_total_stars",
    "sponsor_count", "has_github_sponsors",
    "weekly_downloads", "cii_badge_present", "repo_stargazers",
    "org_admin_count", "is_org_owned",
]

ID_COLS = ["row_id", "analysis", "ecosystem", "package", "cutoff_date",
           "match_group", "is_historical", "tier_for_interpretation_only"]
LABEL_COLS = ["label"]
GROUP_COLS = ["case_group", "group_ambiguous"]
ALL_FEATURE_COLS = list(dict.fromkeys(H_FEATURES + C_FEATURES))


def campaign_of(name: str):
    base = name.split("/")[-1]
    if base in HADES:
        return "campaign:hades"
    if name in NPM_2021:
        return "campaign:npm-2021-credential-theft"
    if base in TEAMPCP or name in TEAMPCP:
        return "campaign:teampcp"
    if name in MIASMA:
        return "campaign:miasma"
    return None


class UnionFind:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def commits_observable_at(commits, cutoff):
    """Return commits whose author and committer timestamps were observable."""
    return [
        commit for commit in commits
        if commit.authored_date <= cutoff and commit.committed_date <= cutoff
    ]


def calculate_metrics_at_cutoff(git, commits, cutoff):
    return git.calculate_metrics(commits_observable_at(commits, cutoff), cutoff)


def h_exclusion(case):
    return H_EXCLUSIONS.get((case.ecosystem, case.name, case.cutoff_date))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", type=Path, default=ARTIFACTS)
    args = ap.parse_args()
    import asyncio
    asyncio.run(build(args.output_dir))


async def build(output_dir: Path):
    git = GitCollector()
    senti = SentimentAnalyzer()

    # ---------- pass 1: collect + per-case feature computation ----------
    cases = list(VALIDATION_CASES)
    errors = []

    async def collect(case):
        cutoff_for_collect = parse_utc_date_end(case.cutoff_date) if case.cutoff_date else None
        if case.evidence_fixture:
            return load_evidence_fixture(case), cutoff_for_collect
        collected, warnings = await cached_collect(
            case.name, case.ecosystem, case.repo_url,
            cutoff_date=cutoff_for_collect or REPLAY_INSTANT.replace(tzinfo=None),
            cache_only=True,
            snapshot_collected_before=SNAPSHOT_BEFORE.replace(tzinfo=None),
        )
        if collected is None:
            raise RuntimeError(warnings[0] if warnings else "no snapshot")
        return collected, cutoff_for_collect

    def compute(case, collected, is_hist):
        cutoff = (parse_utc_date_end(case.cutoff_date)
                  if case.cutoff_date else REPLAY_INSTANT.replace(tzinfo=None))
        m = calculate_metrics_at_cutoff(git, collected.all_commits, cutoff)
        gd = collected.github_data

        # frustration, frozen attribution discipline
        commit_maintainers = ({m.top_contributor_email}
                              if m.top_contributor_email else set())
        cs = senti.analyze_commits(
            [c.message for c in m.commits],
            author_ids=[_normalize_email(c.author_email) for c in m.commits],
            maintainer_ids=commit_maintainers,
        )
        maintainer_logins = {gd.maintainer_username} if gd.maintainer_username else None
        if is_hist:
            isent = senti.analyze_issues([])
        else:
            isent = senti.analyze_issues(
                _filter_issues_for_cutoff(gd.issues, cutoff),
                maintainer_logins=maintainer_logins,
            )

        # cutoff-safe tenure: only when the current profile is bound to the
        # cutoff top contributor (engine's maintainer_identity_matches rule)
        bound = bool(gd.maintainer_username) and bool(
            gd.maintainer_source_email
            and gd.maintainer_source_email == m.top_contributor_email
        )
        acc_age, acc_missing = "", 1
        if bound and collected.maintainer_account_created:
            ac = collected.maintainer_account_created
            if ac.tzinfo is None:
                ac = ac.replace(tzinfo=UTC)
            cz = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)
            acc_age = round((cz - ac).days / 365.25, 2)
            acc_missing = 0

        cii = (not is_hist) and gd.cii_badge_level in ("gold", "silver", "passing")

        row = {
            "top_contributor_concentration": round(m.maintainer_concentration, 2),
            "code_bus_factor": m.bus_factor,
            "unique_contributors": m.unique_contributors,
            "commits_last_year": m.commits_last_year,
            "zero_activity_flag": int(m.commits_last_year == 0),
            "repo_age_years": round(m.repo_age_years, 2),
            "lifetime_commit_count": m.total_commits,
            "lifetime_contributors": m.lifetime_contributors,
            "is_mature": int(m.is_mature),
            "inactive_contributor_ratio": round(m.inactive_contributor_ratio, 3),
            "takeover_shift_pp": round(m.takeover_shift, 2),
            "takeover_suspect_tenure_years": round(m.takeover_suspect_tenure_years, 2),
            "frustration_commit_count": cs.frustration_count,
            "frustration_commit_texts": cs.total_analyzed,
            "frustration_issue_count": ("" if is_hist else isent.frustration_count),
            "frustration_issue_texts": ("" if is_hist else isent.total_analyzed),
            "is_org_owned": int(gd.is_org_owned),
            "account_age_years": acc_age,
            "account_age_years_missing": acc_missing,
            # current-only features: blank for historical rows
            "merge_bus_factor": (
                "" if is_hist or gd.merge_bus_factor in (None, 0)
                else gd.merge_bus_factor
            ),
            "merge_bus_factor_missing": ("" if is_hist else int(gd.merge_bus_factor in (None, 0))),
            # missing semantics: '' when merge data unavailable, never a
            # silent 0 (which would read as "perfectly distributed")
            "merge_concentration": (
                "" if is_hist or gd.merge_bus_factor in (None, 0)
                or gd.merge_concentration is None
                else round(gd.merge_concentration, 2)
            ),
            "maintainer_public_repos": "" if is_hist or not bound else gd.maintainer_public_repos,
            "maintainer_total_stars": "" if is_hist or not bound else gd.maintainer_total_stars,
            "sponsor_count": "" if is_hist or not bound else gd.maintainer_sponsor_count,
            # consistent missing semantics: when the maintainer profile is
            # not bound, sponsorship is unknown (''), not an observed zero
            "has_github_sponsors": "" if is_hist or not bound else int(gd.has_github_sponsors),
            "weekly_downloads": "" if is_hist else (collected.weekly_downloads or 0),
            "cii_badge_present": "" if is_hist else int(cii),
            "repo_stargazers": "" if is_hist else (collected.repo_stargazers or 0),
            "org_admin_count": "" if is_hist else gd.org_admin_count,
        }
        identity = {
            "proxy": (m.top_contributor_email or "").strip().lower(),
            "gh": (gd.maintainer_username or "").lower(),
        }
        return row, identity

    # ---------- pass 2: row plan ----------
    controls = [c for c in cases if c.expected_outcome == "safe"]
    h_candidates = [
        c for c in cases
        if c.expected_outcome == "incident" and c.tier in {"T1", "T2", "T3"}
    ]
    excluded_h_pos = [c for c in h_candidates if h_exclusion(c)]
    h_pos = [c for c in h_candidates if not h_exclusion(c)]
    t_risk = [c for c in cases if c.tier == "T_risk"]
    boundary = [c for c in cases if c.tier in {"T4", "T5"}]

    # deterministic predeclared matching: per incident, controls in same
    # ecosystem whose repo existed at the incident cutoff (first_commit_date
    # <= cutoff), sorted by (name) and taking the FIRST FOUR in alphabetical
    # order of (ecosystem, name). Declared before seeing any feature values.
    # NOTE: existence needs first_commit_date, which is cutoff-safe metadata.
    # compute existence dates first (needed for matching); one collection per case
    collected_map = {}
    metrics_map = {}
    for i, case in enumerate(cases, 1):
        key = (case.ecosystem, case.name, case.cutoff_date or "current")
        try:
            collected, _ = await collect(case)
            # H rows for matched controls are re-scored at the incident cutoff:
            # features must be computed AT THAT cutoff, not at the control's
            # own current-state cutoff. Defer feature computation to emit().
            collected_map[key] = collected
            cutoff = parse_utc_date_end(case.cutoff_date) if case.cutoff_date else None
            metric_cutoff = cutoff or REPLAY_INSTANT.replace(tzinfo=None)
            m = calculate_metrics_at_cutoff(
                git, collected.all_commits, metric_cutoff
            )
            metrics_map[key] = m
        except Exception as e:  # noqa: BLE001
            errors.append((key, str(e)))
        if i % 40 == 0:
            print(f"collected {i}/{len(cases)}")

    for k, e in errors:
        print("ERROR", k, e)

    # control existence date = first commit date of its (current) collection
    ctrl_first = {}
    for c in controls:
        k = (c.ecosystem, c.name, "current")
        if k in metrics_map:
            m = metrics_map[k]
            ctrl_first[k] = m.first_commit_date

    # deterministic diversity-aware matching (predeclared): process
    # incidents chronologically (package-name tie-break); at each step pick
    # up to four eligible controls with the FEWEST previous selections;
    # equal-use ties broken alphabetically. No feature values, labels beyond
    # control eligibility, scores or model results are used.
    use_count = defaultdict(int)
    ordered_incidents = sorted(h_pos, key=lambda c: (c.cutoff_date, c.name.lower()))
    match_groups = {}
    for c in ordered_incidents:
        cutoff = parse_utc_date_end(c.cutoff_date)
        mg = f"mg:{c.ecosystem}:{c.name}:{c.cutoff_date}"
        eligible = []
        for ctrl in controls:
            k = (ctrl.ecosystem, ctrl.name, "current")
            if ctrl.ecosystem != c.ecosystem or k not in collected_map:
                continue
            fcd = ctrl_first.get(k)
            if fcd is None:
                continue
            if fcd.tzinfo is not None:
                fcd = fcd.replace(tzinfo=None)
            if fcd <= cutoff:
                eligible.append(ctrl)
        eligible.sort(key=lambda x: (use_count[x.name], x.name.lower()))
        chosen = eligible[:4]
        for ctrl in chosen:
            use_count[ctrl.name] += 1
        match_groups[mg] = [c] + chosen

    # ---------- pass 3: emit rows ----------
    rows, uf = [], UnionFind()
    ident_nodes = defaultdict(set)  # identity -> row keys (for components)

    def emit(row_id, analysis, case, label, mg, feature_cutoff=None):
        key = (case.ecosystem, case.name, case.cutoff_date or "current")
        if key not in collected_map:
            return
        collected = collected_map[key]
        if feature_cutoff is not None:
            # matched-control H row: cutoff-safe features at the incident date
            import dataclasses
            proxy_case = dataclasses.replace(case, cutoff_date=feature_cutoff)
            feats, ident = compute(proxy_case, collected, True)
        else:
            feats, ident = compute(case, collected, case.cutoff_date is not None)
        node = row_id
        camp = campaign_of(case.name)
        if camp:
            ident_nodes[camp].add(node)
        ident_nodes[f"repo:{case.ecosystem}:{case.name}"].add(node)
        if ident["gh"]:
            ident_nodes[f"gh:{ident['gh']}"].add(node)
        elif ident["proxy"]:
            ident_nodes[f"proxy:{ident['proxy']}"].add(node)
        row = {
            "row_id": row_id, "analysis": analysis,
            "ecosystem": case.ecosystem, "package": case.name,
            "cutoff_date": feature_cutoff or case.cutoff_date or "",
            "match_group": mg,
            "is_historical": int(feature_cutoff is not None or case.cutoff_date is not None),
            "tier_for_interpretation_only": case.tier or "",
            "label": label,
        }
        row.update({k: feats.get(k, "") for k in ALL_FEATURE_COLS})
        row["maintainer_proxy_email"] = ident["proxy"]
        row["maintainer_github_login"] = ident["gh"]
        rows.append((row, node))

    for mg, members in match_groups.items():
        pos = members[0]
        emit(f"H:{pos.ecosystem}:{pos.name}:{pos.cutoff_date}", "H", pos, 1, mg)
        for j, ctrl in enumerate(members[1:], 1):
            emit(f"H:{ctrl.ecosystem}:{ctrl.name}:{pos.cutoff_date}#m{j}",
                 "H", ctrl, 0, mg, feature_cutoff=pos.cutoff_date)
    print(f"match groups: {len(match_groups)}")

    # deduplicate negative H observations by (ecosystem, package, cutoff):
    # the four old-enough github controls were selected for all six Hades
    # incidents at the same 2026-06-07 cutoff, producing sixfold identical
    # negative rows. Grouping prevents fold leakage but not sixfold sample
    # weighting. Keep first occurrence; merge match-group provenance.
    seen_neg = {}
    deduped = []
    for row, node in rows:
        if row["analysis"] == "H" and row["label"] == 0:
            k = (row["ecosystem"], row["package"], row["cutoff_date"])
            if k in seen_neg:
                prev = seen_neg[k]
                prev["match_group"] = prev["match_group"] + "|" + row["match_group"]
                continue
            seen_neg[k] = row
        deduped.append((row, node))
    rows = deduped

    # Keep invalid historical reconstructions visible for qualitative review,
    # but exclude them before matching so they cannot leave orphaned controls.
    for case in excluded_h_pos:
        mg = f"mg:{case.ecosystem}:{case.name}:{case.cutoff_date}"
        emit(f"Q:{case.ecosystem}:{case.name}:{case.cutoff_date}",
             "Q", case, "", mg)

    # globally unique row_ids after dedup
    for n, (row, node) in enumerate(rows, 1):
        row["row_id"] = f"r{n:03d}"
    for c in t_risk:
        emit(f"C:{c.ecosystem}:{c.name}", "C", c, 1, "")
    for c in controls:
        emit(f"C:{c.ecosystem}:{c.name}", "C", c, 0, "")
    for c in boundary:
        emit(f"X:{c.ecosystem}:{c.name}:{c.cutoff_date}", "X", c, "", "")

    # ---------- pass 4: connected components ----------
    for node_set in ident_nodes.values():
        nodes = sorted(node_set)
        for other in nodes[1:]:
            uf.union(nodes[0], other)
    comp_of = {node: uf.find(node) for _, node in rows}
    # stable readable ids
    comp_ids = {}
    for node in comp_of.values():
        comp_ids.setdefault(node, f"grp{len(comp_ids)+1:03d}")
    out_rows = []
    for row, node in rows:
        row["case_group"] = comp_ids[comp_of[node]]
        row["group_ambiguous"] = int(
            not row["maintainer_github_login"] and not row["maintainer_proxy_email"])
        out_rows.append(row)

    out_rows.sort(key=lambda r: (r["analysis"], r["match_group"], r["row_id"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    cols = ID_COLS + LABEL_COLS + ALL_FEATURE_COLS + GROUP_COLS
    with open(output_dir / "ml_feature_matrix.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in out_rows:
            w.writerow({k: r[k] for k in cols})
    with open(output_dir / "ml_group_assignments.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["row_id", "analysis", "ecosystem",
                           "package", "cutoff_date", "match_group", "case_group",
                           "group_ambiguous"])
        w.writeheader()
        for r in out_rows:
            w.writerow({k: r[k] for k in w.fieldnames})
    # provenance sidecar
    json.dump({
        "built_utc": datetime.now(UTC).isoformat(),
        "source_commit": "715b36cf549d998bf1e2e928cc5a1b9f4e346a00",
        "methodology": "6.4.3", "collector_version": 5,
        "replay_instant": "2026-08-15T15:49:38.364446Z",
        "snapshot_collected_before": "2026-08-15T18:48:00Z",
        "H_features": H_FEATURES, "C_features": C_FEATURES,
        "H_sensitivity_features": H_SENSITIVITY_FEATURES,
        "matching_rule": (
            "chronological incidents (name tie-break); same ecosystem; control "
            "first_commit_date <= incident cutoff; fewest previous selections "
            "first; equal-use ties alphabetical; up to four"
        ),
        "H_population": {
            "canonical_positive_cases": len(h_candidates),
            "fitted_positive_cases": len(h_pos),
            "excluded_cases": [
                {
                    "ecosystem": case.ecosystem,
                    "package": case.name,
                    "cutoff_date": case.cutoff_date,
                    "reason": h_exclusion(case),
                }
                for case in excluded_h_pos
            ],
        },
        "historical_commit_filter": (
            "author timestamp <= cutoff and committer timestamp <= cutoff"
        ),
    }, open(output_dir / "ml_matrix_provenance.json", "w"), indent=1)
    print(f"wrote {len(out_rows)} rows; {len(errors)} errors")


if __name__ == "__main__":
    main()
