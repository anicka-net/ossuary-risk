# Review Fix Log

## Round 1

### 1. Cache identity and refresh controls — FIXED

Explicit repository overrides bypass incompatible score/snapshot hits,
`refresh_data=True` bypasses raw and negative-cache reads without disabling
writes, and shared snapshots retain the donor collection timestamp.

### 2. Historical scoring — FIXED

Historical scores neutralize maintainer reputation when the top contributor at
the cutoff differs from the contributor whose GitHub profile was collected.
Legacy snapshots reconstruct the source identity from their commits and
collection timestamp. `INSUFFICIENT_DATA` months are omitted rather than
materialized as nullable historical scores.

### 3. Collector correctness — FIXED

Tapered concentration uses the largest weighted contributor total. A failed
merge-concentration GraphQL request records a provisional reason instead of
silently becoming a measured zero.

The GitHub ordering assumption was also reproduced against the live API:

```text
{"first":{"nodes":[{"number":6013,"updatedAt":"2026-05-17T00:27:57Z"},{"number":5812,"updatedAt":"2026-05-02T03:37:13Z"},{"number":5962,"updatedAt":"2026-04-08T00:18:11Z"}]},"last":{"nodes":[{"number":1723,"updatedAt":"2016-03-30T13:51:13Z"},{"number":517,"updatedAt":"2016-03-30T13:50:45Z"},{"number":493,"updatedAt":"2016-03-30T13:50:45Z"}]}}
```

### 4. CRA and SBOM outputs — FIXED

Support-period component identity includes ecosystem, skipped components count
as unscored, a fully unscored SBOM is indeterminate, and JSON mode emits only
JSON.

### 5. Public surfaces — FIXED

Dashboard aggregates exclude historical rows and expose
`INSUFFICIENT_DATA` packages for retry. API ecosystem casing and non-negative
`max_age` validation are consistent. Launcher failures propagate, movers omit
historical rows, and GitHub dependency-tree requests use the bearer token.

### 6. Repository-aware cache reuse — FIXED

Prefetched registry metadata can reuse a shared repository snapshot, including
registry-specific download data, without invoking a fresh full collection.

### 7. Duplication cleanup — FIXED

The duplicated custom/SUSE batch progress callback is consolidated into one
helper without changing command output.

### 8. Test CI — FIXED

`.github/workflows/tests.yml` installs the existing development dependencies
and runs the repository test suite on pushes and pull requests.

Workflow parsing evidence:

```text
All checks passed!
workflow YAML parsed
```

### 9. End-to-end verification — FIXED

Targeted public-surface suite:

```text
........................................................................ [ 70%]
..............................                                           [100%]
102 passed in 2.01s
```

Repository-aware cache suite:

```text
....................                                                     [100%]
20 passed in 0.37s
```

Historical-scoring suite:

```text
........................................................................ [ 94%]
....                                                                     [100%]
76 passed in 0.92s
```

Merge-fetch failure path:

```text
.                                                                        [100%]
1 passed in 0.42s
```

Complete suite:

```text
........................................................................ [ 12%]
........................................................................ [ 24%]
........................................................................ [ 36%]
........................................................................ [ 49%]
........................................................................ [ 61%]
........................................................................ [ 73%]
........................................................................ [ 86%]
........................................................................ [ 98%]
..........                                                               [100%]
586 passed in 13.09s
```

The canonical artifact was recomputed from the valid raw snapshots after the
live re-collection attempt encountered registry rate limits and
organization-restricted API responses. The completed run scored all 184 cases
with no artifact errors:

```text
Total packages tested: 184
Correct predictions:   151
Accuracy:              82.1%

Confusion Matrix:
  True Positives (TP):  33 - Predicted risky, was incident
  True Negatives (TN):  118 - Predicted safe, was safe
  False Positives (FP): 2 - Predicted risky, was safe
  False Negatives (FN): 31 - Predicted safe, was incident

Precision: 94.3% (of packages flagged risky, how many were incidents)
Recall:    51.6% (of actual incidents, how many were flagged)
F1 Score:  0.67

Scope B (in-scope tiers T1,T2,T3,T_risk): Prec=93.9% Rec=73.8% F1=0.827
```

### Final self-check

1. **Unverified review items:** None. Every implemented review item has an
   executed targeted test, complete-suite coverage, artifact recomputation, or
   the live GitHub ordering call shown above.
2. **Diff/evidence correspondence:** Every claim above corresponds to the
   working-tree diff or a pasted command-output block.
3. **Smoke-test coverage:** Cache controls, public surfaces, repository-aware
   reuse, historical scoring, the full test suite, workflow parsing, canonical
   validation, and live GraphQL ordering are each represented above.
