#!/usr/bin/env python3
"""Score the exact H-matrix rows with frozen Ossuary v6.4.3 at their cutoffs.

Same-population comparator for the ML diagnostic: positives reuse the
canonical same-cutoff scores from validation_results.json; the 95 matched
historical control observations are scored through the frozen scorer using
the same cache/replay semantics as the committed validation run
(cache-only, snapshot-collected-before 2026-08-15T18:48Z).

Outputs in benchmarks/ml_diagnostic_2026_08_29:
  ossuary_h_scores.csv — exact H-population scores at historical cutoffs;
  ossuary_c_scores.csv — frozen T_risk + control rows from validation_results.json.
"""
# ruff: noqa: E402, N806

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "benchmarks" / "ml_diagnostic_2026_08_29"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from dotenv import load_dotenv

load_dotenv(REPO / ".env")

import pandas as pd  # noqa: E402
from validate import VALIDATION_CASES, load_evidence_fixture, parse_utc_date_end  # noqa: E402

from ossuary.services.scorer import cached_collect, calculate_score_for_date  # noqa: E402

SNAPSHOT_BEFORE = datetime(2026, 8, 15, 18, 48, 0)
REPLAY_INSTANT = datetime(2026, 8, 15, 15, 49, 38, 364446)

# case lookup for evidence fixtures / repo urls
CASES = {}
for c in VALIDATION_CASES:
    CASES.setdefault((c.ecosystem, c.name), c)


async def main(artifacts):
    df = pd.read_csv(artifacts / "ml_feature_matrix.csv", dtype=str).fillna("")
    H = df[df.analysis == "H"]
    vr = json.load(open(REPO / "validation_results.json"))
    canon = {(r["case"]["ecosystem"], r["case"]["name"], r["case"].get("cutoff_date") or ""): r
             for r in vr["results"]}

    out = []
    for _, row in H.iterrows():
        key = (row["ecosystem"], row["package"], row["cutoff_date"])
        rec = {"row_id": row["row_id"], "package": row["package"],
               "ecosystem": row["ecosystem"], "cutoff_date": row["cutoff_date"],
               "label": row["label"], "score": "", "predicted": "", "error": ""}
        if row["label"] == "1":
            r = canon.get(key)
            if r is None:
                rec["error"] = "no canonical score"
            else:
                rec["score"] = r["score"]
                rec["predicted"] = r["predicted_outcome"]
            out.append(rec)
            continue
        # matched historical control: score at the H cutoff
        case = CASES.get((row["ecosystem"], row["package"]))
        cutoff = parse_utc_date_end(row["cutoff_date"])
        try:
            if case is not None and case.evidence_fixture:
                collected = load_evidence_fixture(case)
            else:
                collected, warnings = await cached_collect(
                    row["package"], row["ecosystem"],
                    case.repo_url if case else None,
                    cutoff_date=cutoff,
                    cache_only=True,
                    snapshot_collected_before=SNAPSHOT_BEFORE,
                )
                if collected is None:
                    rec["error"] = warnings[0] if warnings else "no snapshot"
                    out.append(rec)
                    continue
            bd = calculate_score_for_date(
                row["package"], row["ecosystem"], collected, cutoff,
                is_historical=True)
            rec["score"] = bd.final_score
            rec["predicted"] = "risky" if bd.final_score >= 60 else "safe"
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)[:120]
        out.append(rec)
        if len(out) % 25 == 0:
            print(f"{len(out)}/{len(H)}")

    with open(artifacts / "ossuary_h_scores.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(out)

    c_out = []
    for result in vr["results"]:
        case = result["case"]
        if case.get("tier") != "T_risk" and case["expected_outcome"] != "safe":
            continue
        c_out.append({
            "package": case["name"],
            "ecosystem": case["ecosystem"],
            "label": 0 if case["expected_outcome"] == "safe" else 1,
            "score": result["score"],
            "predicted": result["predicted_outcome"],
        })
    with open(artifacts / "ossuary_c_scores.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(c_out[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(c_out)

    n_err = sum(1 for r in out if r["error"])
    print(f"wrote H={len(out)} rows ({n_err} errors), C={len(c_out)} rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACTS)
    asyncio.run(main(parser.parse_args().artifact_dir))
