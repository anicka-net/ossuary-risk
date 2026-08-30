#!/usr/bin/env python3
"""Experiment 5 step 2: fit diagnostic models under the approved protocol.

- H: repeated 5-fold StratifiedGroupKFold (5 repeats); C: repeated 4-fold.
- Fold-local preprocessing (median imputation + scaling inside train folds).
- Fixed models: L2 logistic regression; HistGradientBoosting (shallow,
  conservative, native NaN handling); shallow RandomForest as robustness
  comparator. No hyperparameter search.
- Boundary: T4/T5 (X) predicted from the H regime; any X row sharing a
  connected leakage group with H rows is scored with that entire group held
  out of training.
- Business comparisons: recall at ~Ossuary's FP count (2, Scope B);
  precision at ~Ossuary's recall (0.762).
- Frustration interaction: NOT evaluated (zero events in H; declared
  unevaluable in the audit).

Inputs: benchmarks/ml_diagnostic_2026_08_29/ml_feature_matrix.csv and
ml_matrix_provenance.json,
validation_results.json (Ossuary scores for comparison only — never features).
Outputs: ml_cv_predictions_{H,C}.csv, ml_model_summary.json,
ml_feature_stability.csv, ml_disagreements_raw.csv, and
ml_boundary_predictions.csv in the same benchmark directory. Run
ml_ossuary_h_scores.py and ml_eval_repair.py afterwards to construct the
same-population comparison and final reviewed summary without refitting.
"""
# ruff: noqa: N803, N806

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "benchmarks" / "ml_diagnostic_2026_08_29"
N_REPEATS = 5
OSSUARY_FP = 2          # Scope B false positives at threshold 60
OSSUARY_RECALL = 0.762  # Scope B recall at threshold 60

RNG = np.random.default_rng(20260828)


def make_models():
    return {
        "logreg": Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("sc", StandardScaler()),
            ("clf", LogisticRegression(C=1.0, max_iter=5000)),
        ]),
        "gbm": HistGradientBoostingClassifier(
            max_depth=3, max_iter=200, learning_rate=0.05,
            l2_regularization=1.0, min_samples_leaf=10,
            early_stopping=False, random_state=0),
        "rf": Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("clf", RandomForestClassifier(
                n_estimators=300, max_depth=4, min_samples_leaf=5,
                random_state=0, n_jobs=4)),
        ]),
    }


def repeated_oof(X, y, groups, n_splits, model_name):
    """Out-of-fold probabilities across repeats. Returns list of fold records
    and per-row mean OOF probability."""
    oof_sum = np.zeros(len(y))
    oof_cnt = np.zeros(len(y))
    fold_records = []
    coefs = []
    for rep in range(N_REPEATS):
        cv = StratifiedGroupKFold(n_splits=n_splits, shuffle=True,
                                  random_state=1000 + rep)
        for fold, (tr, te) in enumerate(cv.split(X, y, groups)):
            model = make_models()[model_name]
            model.fit(X[tr], y[tr])
            if hasattr(model, "predict_proba"):
                p = model.predict_proba(X[te])[:, 1]
            else:
                p = model.decision_function(X[te])
            oof_sum[te] += p
            oof_cnt[te] += 1
            fold_records.append({"repeat": rep, "fold": fold,
                                 "n_train": len(tr), "n_test": len(te),
                                 "pos_test": int(y[te].sum())})
            if model_name == "logreg":
                coefs.append(model.named_steps["clf"].coef_[0])
    return oof_sum / np.maximum(oof_cnt, 1), fold_records, coefs


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


def roc_pr_auc(y, p):
    from sklearn.metrics import average_precision_score, roc_auc_score
    try:
        return round(roc_auc_score(y, p), 4), round(average_precision_score(y, p), 4)
    except ValueError:
        return None, None


def recall_at_fp_budget(y, p, fp_budget):
    """Among thresholds with FP <= budget, maximise recall; tie-break by
    precision, then by lower threshold."""
    best = None
    for t in np.unique(p):
        m = metrics_at_threshold(y, p, t)
        if m["FP"] > fp_budget:
            continue
        if (best is None or m["recall"] > best["recall"]
                or (m["recall"] == best["recall"] and m["precision"] > best["precision"])):
            best = m
    return best if best is not None else metrics_at_threshold(y, p, np.inf)


def precision_at_recall(y, p, target_recall):
    best = None
    for t in np.sort(np.unique(p))[::-1]:
        m = metrics_at_threshold(y, p, t)
        if m["recall"] >= target_recall:
            if best is None or m["precision"] > best["precision"]:
                best = m
    return best


def run_regime(name, df, features, n_splits):
    X = df[features].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    y = df["label"].to_numpy(int)
    groups = df["case_group"].to_numpy()
    out = {"regime": name, "n": len(df), "n_pos": int(y.sum()),
           "n_groups": df["case_group"].nunique(), "features": features,
           "models": {}}
    preds = {"row_id": df["row_id"].tolist()}
    stability_rows = []
    for model_name in ["logreg", "gbm", "rf"]:
        oof, folds, coefs = repeated_oof(X, y, groups, n_splits, model_name)
        preds[f"{model_name}_oof"] = np.round(oof, 4).tolist()
        roc, pr = roc_pr_auc(y, oof)
        m05 = metrics_at_threshold(y, oof, 0.5)
        m_fp = recall_at_fp_budget(y, oof, OSSUARY_FP)
        m_rec = precision_at_recall(y, oof, OSSUARY_RECALL)
        out["models"][model_name] = {
            "roc_auc": roc, "pr_auc": pr,
            "at_0.5": m05,
            f"recall_at_fp<={OSSUARY_FP}": m_fp,
            f"precision_at_recall>={OSSUARY_RECALL}": m_rec,
            "folds": folds,
        }
        if model_name == "logreg":
            coefs = np.array(coefs)
            for j, f in enumerate(features):
                c = coefs[:, j]
                stability_rows.append({
                    "regime": name, "feature": f,
                    "coef_mean": round(float(c.mean()), 4),
                    "coef_sd": round(float(c.std()), 4),
                    "sign_consistency": round(float(np.mean(np.sign(c) == np.sign(c.mean()))), 3),
                })
        if model_name == "gbm":
            # permutation importance on held-out folds (one repeat, fold-local)
            from sklearn.inspection import permutation_importance
            cv = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=7)
            imps = []
            for tr, te in cv.split(X, y, groups):
                m = make_models()["gbm"]
                m.fit(X[tr], y[tr])
                r = permutation_importance(m, X[te], y[te], n_repeats=10,
                                           random_state=0, scoring="f1")
                imps.append(r.importances_mean)
            imps = np.array(imps)
            for j, f in enumerate(features):
                stability_rows.append({
                    "regime": name, "feature": f,
                    "gbm_perm_importance_mean": round(float(imps[:, j].mean()), 4),
                    "gbm_perm_importance_sd": round(float(imps[:, j].std()), 4),
                })
    return out, preds, stability_rows


def main(artifacts):
    df = pd.read_csv(artifacts / "ml_feature_matrix.csv", dtype=str).fillna("")
    prov = json.load(open(artifacts / "ml_matrix_provenance.json"))
    H = df[df.analysis == "H"].copy()
    C = df[df.analysis == "C"].copy()
    X = df[df.analysis == "X"].copy()
    H["label"] = H["label"].astype(int)
    C["label"] = C["label"].astype(int)

    summary = {"protocol": {
        "H": "repeated 5-fold StratifiedGroupKFold, 5 repeats",
        "C": "repeated 4-fold StratifiedGroupKFold, 5 repeats",
        "models": "L2 logistic (C=1.0), HistGBM (depth 3, lr 0.05, L2 1.0, "
                  "min_samples_leaf 10), RF (depth 4, 300 trees, leaf 5)",
        "preprocessing": "fold-local median imputation; standardisation for "
                         "logreg; RF without scaling; GBM native NaN",
        "no_hyperparameter_search": True,
        "frustration_interaction": "not evaluated — zero events in H; "
                                   "declared unevaluable pre-fit",
    }}

    h_out, h_preds, h_stab = run_regime("H", H, prov["H_features"], 5)
    c_out, c_preds, c_stab = run_regime("C", C, prov["C_features"], 4)
    summary["H"] = h_out
    summary["C"] = c_out

    # ---- boundary: predict X from H regime, group-held-out ----
    h_groups = set(H["case_group"])
    X_feat = X[prov["H_features"]].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    XH = H[prov["H_features"]].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    yH = H["label"].to_numpy(int)
    gH = H["case_group"].to_numpy()
    boundary_rows = []
    for i, (_, xrow) in enumerate(X.iterrows()):
        g = xrow["case_group"]
        held_out = g in h_groups
        if held_out:
            mask = gH != g
        else:
            mask = np.ones(len(yH), bool)
        rec = {"row_id": xrow["row_id"], "package": xrow["package"],
               "tier": xrow["tier_for_interpretation_only"],
               "case_group": g, "h_group_held_out": held_out}
        for model_name in ["logreg", "gbm", "rf"]:
            m = make_models()[model_name]
            m.fit(XH[mask], yH[mask])
            if hasattr(m, "predict_proba"):
                p = m.predict_proba(X_feat[i:i + 1])[:, 1][0]
            else:
                p = m.decision_function(X_feat[i:i + 1])[0]
            rec[f"{model_name}_p"] = round(float(p), 4)
        boundary_rows.append(rec)
    pd.DataFrame(boundary_rows).to_csv(artifacts / "ml_boundary_predictions.csv",
                                       index=False)

    # ---- Ossuary comparison & disagreement audit ----
    vr = json.load(open(REPO / "validation_results.json"))
    oss = {}
    for r in vr["results"]:
        c = r["case"]
        oss[(c["ecosystem"], c["name"], c.get("cutoff_date") or "")] = (
            r["score"], r["predicted_outcome"], r["classification"])

    def ossuary_of(row):
        co = row["cutoff_date"]
        # matched-control H rows carry the incident cutoff; the Ossuary score
        # for a control is its current-state score (different cutoff) — flag.
        v = oss.get((row["ecosystem"], row["package"], co))
        if v:
            return v, "same-cutoff"
        v = oss.get((row["ecosystem"], row["package"], ""))
        return v, "current-state-ossuary-score"

    dis_rows = []
    for _, row in pd.concat([H, C, X]).iterrows():
        v, basis = ossuary_of(row)
        if not v:
            continue
        score, pred_out, cls = v
        dis_rows.append({
            "row_id": row["row_id"], "analysis": row["analysis"],
            "package": row["package"],
            "tier": row["tier_for_interpretation_only"],
            "ossuary_score": score, "ossuary_pred": pred_out,
            "ossuary_classification": cls, "ossuary_basis": basis,
            "label": row["label"],
            "logreg_oof": (h_preds["logreg_oof"][H.index.get_loc(row.name)]
                           if row["analysis"] == "H" else
                           c_preds["logreg_oof"][C.index.get_loc(row.name)]
                           if row["analysis"] == "C" else ""),
        })

    # save predictions
    for preds, sub, name in [(h_preds, H, "H"), (c_preds, C, "C")]:
        out = pd.DataFrame(preds)
        out.insert(1, "analysis", name)
        out["package"] = sub["package"].to_numpy()
        out["label"] = sub["label"].to_numpy()
        out["case_group"] = sub["case_group"].to_numpy()
        out.to_csv(artifacts / f"ml_cv_predictions_{name}.csv", index=False)
    pd.DataFrame(h_stab + c_stab).to_csv(artifacts / "ml_feature_stability.csv",
                                         index=False)
    pd.DataFrame(dis_rows).to_csv(artifacts / "ml_disagreements_raw.csv", index=False)
    json.dump(summary, open(artifacts / "ml_model_summary.json", "w"), indent=1)
    print(json.dumps({k: summary[k]["models"]["logreg"]["roc_auc"] for k in ["H", "C"]}))
    from ml_eval_repair import main as repair_evaluation

    repair_evaluation(artifacts)
    print("done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACTS)
    main(parser.parse_args().artifact_dir)
