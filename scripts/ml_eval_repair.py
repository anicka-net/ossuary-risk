#!/usr/bin/env python3
"""Regenerate evaluation artefacts after the review repair. No refitting:
reads preserved OOF probabilities from ml_cv_predictions_{H,C}.csv and the
same-population frozen-Ossuary comparators (ossuary_h_scores.csv and
ossuary_c_scores.csv).

Fixes: correct TN in all confusion matrices; recall at each population's
Ossuary FP budget = max-recall threshold within budget (precision tie-break);
same-population operating-point comparisons; H/C disagreement kept separate.
"""
# ruff: noqa: N806

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "benchmarks" / "ml_diagnostic_2026_08_29"


def metrics_at_threshold(y, p, t):
    pred = p >= t
    TP = int(((pred == 1) & (y == 1)).sum())
    FP = int(((pred == 1) & (y == 0)).sum())
    TN = int(((pred == 0) & (y == 0)).sum())
    FN = int(((pred == 0) & (y == 1)).sum())
    prec = TP / (TP + FP) if TP + FP else 0.0
    rec = TP / (TP + FN) if TP + FN else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"threshold": round(float(t), 4), "TP": TP, "FP": FP, "TN": TN,
            "FN": FN, "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(f1, 4)}


def recall_at_fp_budget(y, p, budget):
    best = None
    for t in np.unique(p):
        m = metrics_at_threshold(y, p, t)
        if m["FP"] > budget:
            continue
        if (best is None or m["recall"] > best["recall"]
                or (m["recall"] == best["recall"] and m["precision"] > best["precision"])):
            best = m
    return best if best is not None else metrics_at_threshold(y, p, np.inf)


def precision_at_recall(y, p, target):
    best = None
    for t in np.unique(p):
        m = metrics_at_threshold(y, p, t)
        if m["recall"] >= target and (best is None or m["precision"] > best["precision"]):
            best = m
    return best


def main(artifacts):
    prov = json.load(open(artifacts / "ml_matrix_provenance.json"))
    summary = {"protocol": {
        "H": "repeated 5-fold StratifiedGroupKFold, 5 repeats (fitted earlier; OOF preserved)",
        "C": "repeated 4-fold StratifiedGroupKFold, 5 repeats (fitted earlier; OOF preserved)",
        "models": "L2 logistic (C=1.0), HistGBM (depth 3, lr 0.05, L2 1.0, "
                  "min_samples_leaf 10), RF (depth 4, 300 trees, leaf 5)",
        "preprocessing": "fold-local median imputation; standardisation for "
                         "logreg; RF without scaling; GBM native NaN",
        "no_hyperparameter_search": True,
        "frustration_interaction": "not evaluated — zero events in H; declared unevaluable pre-fit",
        "repair_note": "confusion matrices recomputed from y_true/y_pred; "
                       "recall@FP-budget = max-recall threshold within budget "
                       "(precision tie-break); same-population Ossuary comparators",
    }}

    # ---- Ossuary same-population comparators ----
    oh = pd.read_csv(artifacts / "ossuary_h_scores.csv")
    oh["label"] = oh["label"].astype(int)
    oy = oh["label"].to_numpy()
    om = metrics_at_threshold(oh["label"].to_numpy(), oh["score"].to_numpy(), 60)

    oc = pd.read_csv(artifacts / "ossuary_c_scores.csv")
    cy = oc["label"].to_numpy()
    cs = oc["score"].to_numpy()
    cm = metrics_at_threshold(cy, cs, 60)
    summary["ossuary_comparators"] = {
        "H_population": {"n": len(oh), "n_pos": int(oy.sum()), **{k: om[k] for k in
                         ("TP", "FP", "TN", "FN", "precision", "recall", "f1")}},
        "C_population": {"n": len(oc), "n_pos": int(cy.sum()), **{k: cm[k] for k in
                         ("TP", "FP", "TN", "FN", "precision", "recall", "f1")}},
        "note": "H: frozen v6.4.3 scored at the exact matrix cutoffs "
                "(ossuary_h_scores.csv). C: frozen v6.4.3 on the exact "
                "13 T_risk + 120 controls (ossuary_c_scores.csv). The "
                "global Scope B result (TP32/FN10/FP2/TN118) remains the "
                "primary validation result and is not an ML comparator.",
    }

    matrix = pd.read_csv(artifacts / "ml_feature_matrix.csv", dtype=str).fillna("")
    cutoff_of = {r["row_id"]: r["cutoff_date"] for _, r in matrix.iterrows()}
    pkg_of = {r["row_id"]: r["package"] for _, r in matrix.iterrows()}
    for reg, fp_budget, rec_target in [("H", 14, om["recall"]),
                                       ("C", 2, cm["recall"])]:
        df = pd.read_csv(artifacts / f"ml_cv_predictions_{reg}.csv")
        y = df["label"].to_numpy()
        n_groups = df["case_group"].nunique()
        models = {}
        for m in ["logreg", "gbm", "rf"]:
            p = df[f"{m}_oof"].to_numpy()
            models[m] = {
                "roc_auc": round(roc_auc_score(y, p), 4),
                "pr_auc": round(average_precision_score(y, p), 4),
                "at_0.5": metrics_at_threshold(y, p, 0.5),
                f"recall_at_fp<={fp_budget}": recall_at_fp_budget(y, p, fp_budget),
                f"precision_at_recall>={rec_target}":
                    precision_at_recall(y, p, rec_target),
            }
            assert sum(models[m]["at_0.5"][k]
                       for k in ("TP", "FP", "TN", "FN")) == len(y)
        summary[reg] = {"regime": reg, "n": len(df), "n_pos": int(y.sum()),
                        "n_groups": int(n_groups),
                        "features": prov["H_features"] if reg == "H" else prov["C_features"],
                        "ossuary_fp_budget": fp_budget,
                        "ossuary_recall_target": round(float(rec_target), 4),
                        "models": models}
        # regenerate disagreement rows (H and C separately)
        if reg == "H":
            oss_rows = {(r["package"], r["cutoff_date"]): (r["score"], r["predicted"])
                        for _, r in oh.iterrows()}
        else:
            oss_rows = {(r["package"], ""): (r["score"], r["predicted"])
                        for _, r in oc.iterrows()}
        dis = []
        for _, row in df.iterrows():
            co = cutoff_of.get(row["row_id"], "") if reg == "H" else ""
            pkg = pkg_of.get(row["row_id"], row["package"])
            k = (pkg, co)
            if k not in oss_rows:
                continue
            score, pred = oss_rows[k]
            rec = {"row_id": row["row_id"], "analysis": reg, "package": pkg,
                   "label": int(row["label"]),
                   "ossuary_score": score, "ossuary_pred": pred}
            for m in ["logreg", "gbm", "rf"]:
                rec[f"{m}_oof"] = row[f"{m}_oof"]
            dis.append(rec)
        pd.DataFrame(dis).to_csv(artifacts / f"ml_disagreements_{reg}.csv", index=False)

    json.dump(summary, open(artifacts / "ml_model_summary.json", "w"), indent=1)
    print(json.dumps({r: {m: summary[r]["models"][m]["recall_at_fp<=" +
                          str(summary[r]["ossuary_fp_budget"])]["recall"]
                          for m in ["logreg", "gbm", "rf"]} for r in ["H", "C"]}, indent=1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACTS)
    main(parser.parse_args().artifact_dir)
