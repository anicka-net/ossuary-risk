"""Integrity checks for the frozen public diagnostic-ML artifacts."""

import csv
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "benchmarks" / "ml_diagnostic_2026_08_29"


def _rows(name):
    with (ARTIFACTS / name).open(newline="") as f:
        return list(csv.DictReader(f))


def _matrix(rows, probability, threshold):
    counts = {"TP": 0, "FP": 0, "TN": 0, "FN": 0}
    for row in rows:
        actual = int(row["label"])
        predicted = float(row[probability]) >= threshold
        key = ("T" if predicted == actual else "F") + ("P" if predicted else "N")
        counts[key] += 1
    return counts


def _metrics(rows, probability, threshold):
    counts = _matrix(rows, probability, threshold)
    precision = counts["TP"] / (counts["TP"] + counts["FP"])
    recall = counts["TP"] / (counts["TP"] + counts["FN"])
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    return {
        "threshold": round(threshold, 4),
        **counts,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def test_every_stored_confusion_matrix_reproduces_from_oof_probabilities():
    summary = json.loads((ARTIFACTS / "ml_model_summary.json").read_text())
    for regime in ("H", "C"):
        rows = _rows(f"ml_cv_predictions_{regime}.csv")
        for model, results in summary[regime]["models"].items():
            for result in results.values():
                if not isinstance(result, dict) or "threshold" not in result:
                    continue
                observed = _matrix(rows, f"{model}_oof", result["threshold"])
                assert observed == {key: result[key] for key in observed}


def test_stored_operating_points_are_optimal_within_declared_constraints():
    summary = json.loads((ARTIFACTS / "ml_model_summary.json").read_text())
    for regime in ("H", "C"):
        rows = _rows(f"ml_cv_predictions_{regime}.csv")
        budget = summary[regime]["ossuary_fp_budget"]
        recall_target = summary[regime]["ossuary_recall_target"]
        for model, results in summary[regime]["models"].items():
            probability = f"{model}_oof"
            candidates = sorted({float(row[probability]) for row in rows})

            within_budget = [
                _metrics(rows, probability, threshold)
                for threshold in candidates
                if _matrix(rows, probability, threshold)["FP"] <= budget
            ]
            best_recall = max(within_budget, key=lambda m: (m["recall"], m["precision"]))
            assert results[f"recall_at_fp<={budget}"] == best_recall

            above_recall = [
                _metrics(rows, probability, threshold)
                for threshold in candidates
                if _metrics(rows, probability, threshold)["recall"] >= recall_target
            ]
            best_precision = max(above_recall, key=lambda m: m["precision"])
            assert results[f"precision_at_recall>={recall_target}"] == best_precision


def test_same_population_ossuary_comparators_reproduce():
    summary = json.loads((ARTIFACTS / "ml_model_summary.json").read_text())
    h_rows = _rows("ossuary_h_scores.csv")
    h_matrix = _matrix(h_rows, "score", 60)
    assert h_matrix == {"TP": 23, "FP": 14, "TN": 81, "FN": 5}
    assert summary["ossuary_comparators"]["H_population"]["n"] == 123

    validation = json.loads((ROOT / "validation_results.json").read_text())
    validation_c_rows = [
        {
            "package": result["case"]["name"],
            "ecosystem": result["case"]["ecosystem"],
            "label": "0" if result["case"]["expected_outcome"] == "safe" else "1",
            "score": str(result["score"]),
            "predicted": result["predicted_outcome"],
        }
        for result in validation["results"]
        if result["case"].get("tier") == "T_risk"
        or result["case"]["expected_outcome"] == "safe"
    ]
    frozen_c_rows = _rows("ossuary_c_scores.csv")
    assert frozen_c_rows == validation_c_rows
    c_matrix = _matrix(frozen_c_rows, "score", 60)
    assert c_matrix == {"TP": 9, "FP": 2, "TN": 118, "FN": 4}
    c_summary = summary["ossuary_comparators"]["C_population"]
    assert c_summary["n"] == len(frozen_c_rows) == 133
    assert {key: c_summary[key] for key in c_matrix} == c_matrix


def test_public_matrix_omits_maintainer_identity_columns():
    for name in ("ml_feature_matrix.csv", "ml_group_assignments.csv"):
        fieldnames = set(_rows(name)[0])
        assert "maintainer_proxy_email" not in fieldnames
        assert "maintainer_github_login" not in fieldnames


def test_public_bundle_contains_no_email_addresses():
    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    for path in ARTIFACTS.iterdir():
        if path.is_file():
            assert not email.search(path.read_text())


def test_frozen_artifact_checksums_match():
    for line in (ARTIFACTS / "SHA256SUMS").read_text().splitlines():
        expected, name = line.split("  ", 1)
        observed = hashlib.sha256((ARTIFACTS / name).read_bytes()).hexdigest()
        assert observed == expected
