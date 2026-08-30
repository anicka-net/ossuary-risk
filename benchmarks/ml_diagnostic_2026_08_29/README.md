# Diagnostic ML experiment

This directory contains the code-facing artifacts needed to reproduce the
post-freeze machine-learning comparison reported in the thesis. It was
published on 29 August 2026. Its evidence remains pinned to the final public
validation checkpoint of 15 August 2026 and methodology v6.4.3.

The experiment asks whether fixed learned models can discriminate cases from
the same raw governance observables used by Ossuary, and how their operating
points compare with the frozen rule system. It did not choose or tune Ossuary
features, weights, or thresholds. Most incident families were already known
during methodology development, so these results are diagnostic and
calibration evidence, not independent evidence of prediction on unseen
incidents.

## Populations

- H: 123 historical rows, comprising 28 T1/T2/T3 incident observations at
  their pre-incident cutoffs and 95 matched control observations. Connected
  leakage groups are evaluated with repeated five-fold grouped
  cross-validation.
- C: 133 current-state rows, comprising 13 purposively selected `T_risk`
  cases and 120 controls at the August checkpoint. Connected leakage groups
  are evaluated with repeated four-fold grouped cross-validation.
- X: T4/T5 boundary cases. These are interpreted separately and are never
  fitted as positive training cases.

The models are fixed L2 logistic regression, shallow histogram gradient
boosting, and shallow random forest. There is no hyperparameter search.

## Reproduction

The frozen matrix and out-of-fold predictions are committed here. The result
repair can therefore be reproduced without the private thesis repository or
the retained evidence database:

```bash
python -m pip install -r benchmarks/ml_diagnostic_2026_08_29/requirements.txt
python scripts/ml_eval_repair.py
python -m pytest -q tests/test_ml_diagnostic_artifacts.py
```

To refit the three models from the frozen matrix, run:

```bash
python scripts/ml_fit.py
```

`ml_fit.py` is deterministic under the recorded package versions and random
seeds and finishes by reconstructing the reviewed same-population operating
points. `ml_eval_repair.py` exposes that final step separately so the published
OOF probabilities can be checked without refitting a model.

Regenerating `ml_feature_matrix.csv` itself requires the retained
collector-version-5 snapshots from the August validation checkpoint:

```bash
python scripts/ml_matrix.py
python scripts/ml_ossuary_h_scores.py
python scripts/ml_fit.py
```

Those approximately 716 MB of raw snapshots are deliberately not committed.
The public frozen matrix is therefore the reproducibility boundary for model
fitting. `ml_matrix_provenance.json` records its cutoff, methodology, feature
whitelists, and matching rule.

## Review repair

An independent review found that an earlier `ml_model_summary.json` retained
stale true-negative cells even though the OOF prediction CSVs were correct.
The reviewed repair recomputes every confusion matrix from `label` and the
corresponding probability column, uses Ossuary's FP budget and recall on the
same population, and keeps H and C disagreement tables separate. It does not
change any fitted probability or any headline Ossuary validation metric.

The public matrix omits `maintainer_proxy_email` and
`maintainer_github_login`. Neither field is a model feature; the frozen
`case_group` values retain the leakage-control structure without publishing a
consolidated identity table.

## Files

- `ml_feature_matrix.csv`: sanitized frozen model matrix and grouping IDs.
- `ml_matrix_provenance.json`: feature whitelists and construction metadata.
- `ml_cv_predictions_H.csv`, `ml_cv_predictions_C.csv`: preserved OOF
  probabilities.
- `ossuary_h_scores.csv`: frozen Ossuary scores for the H population.
- `ossuary_c_scores.csv`: frozen Ossuary scores for the C population.
- `ml_model_summary.json`: reviewed metrics and same-population comparators.
- `ml_feature_stability.csv`: coefficient and permutation summaries.
- `ml_boundary_predictions.csv`: separately held-out T4/T5 boundary output.
- `ml_disagreements_H.csv`, `ml_disagreements_C.csv`: regime-specific
  row-level comparisons.
- `SHA256SUMS`: integrity hashes for the frozen data and result artifacts.
- `requirements.txt`: exact numerical-library versions used for the clean
  byte-for-byte reproduction.
