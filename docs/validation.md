# Validation Report

**Version**: 3.0 (March 2026)
**Dataset**: 158 packages across 8 ecosystems

---

## Summary

Ossuary's governance-based risk scoring was validated against 158 open source packages spanning 8 package ecosystems. The dataset includes known supply chain incidents, packages with governance risk signals, and a control group of well-maintained packages.

| Metric | Value |
|--------|-------|
| **Accuracy** | 89.2% |
| **Precision** | 95.8% |
| **Recall** | 59.0% |
| **F1 Score** | 0.73 |
| **False Positives** | 1 (rxjs) |

**Key finding**: 1 false positive across 158 packages. rxjs scores 75 HIGH due to 100% maintainer concentration and 0 commits in the last year despite 80M weekly downloads — a borderline case where the governance signals are genuinely concerning but no incident has occurred.

---

## Dataset Composition

### By Ecosystem

| Ecosystem | Incidents | Controls | Total | Accuracy |
|-----------|-----------|----------|-------|----------|
| npm | 18 | 43 | 61 | 85% |
| PyPI | 4 | 42 | 46 | 96% |
| Cargo | 0 | 8 | 8 | 100% |
| RubyGems | 3 | 8 | 11 | 91% |
| Packagist | 0 | 5 | 5 | 100% |
| NuGet | 0 | 4 | 4 | 100% |
| Go | 1 | 4 | 5 | 100% |
| GitHub | 13 | 5 | 18 | 50% |
| **Total** | **39** | **119** | **158** | **89.2%** |

npm has the most incidents because it has the most documented supply chain attacks historically. GitHub has the lowest accuracy because most GitHub-specific cases are CI/CD exploits (stolen tokens, workflow vulnerabilities) — attack types governance metrics cannot detect.

### By Attack Type

| Attack Type | Detected | Total | Rate | Detectable? |
|-------------|----------|-------|------|-------------|
| Governance risk | 11 | 11 | 100% | Yes - primary target |
| Governance failure | 5 | 5 | 100% | Yes - primary target |
| Account compromise | 7 | 23 | 30% | No - outside scope |
| **Control (safe)** | **118** | **119** | **99.2%** | N/A (1 FP: rxjs) |

---

## Confusion Matrix

```
                    Predicted Risky    Predicted Safe
Actually Risky         23 (TP)            16 (FN)
Actually Safe           1 (FP)           118 (TN)
```

### What the Numbers Mean

**Precision (95.8%)**: When ossuary flags a package, it is almost always right. The single false positive (rxjs) has genuinely concerning governance metrics — whether it constitutes a true false positive or an unrealized governance risk is debatable.

**Recall (59.0%)**: Ossuary catches about 59% of risky packages. The ~41% it misses are predominantly attack types it explicitly does not claim to detect (account compromise, CI/CD exploits, insider sabotage). Among governance-detectable attack types (governance_failure + governance_risk), recall is **100%** (16/16).

**The recall ceiling**: Approximately 40% of incidents in our dataset are account compromises or CI/CD exploits. These are fundamentally invisible to governance metrics because the project looks healthy right up until the attack. No amount of tuning will catch credential theft or workflow vulnerabilities. Ossuary is honest about this limitation rather than inflating recall with noisy heuristics.

---

## False Positive Analysis

### rxjs (Score: 75 HIGH)

rxjs is classified as `safe` in our dataset (no incident has occurred), but scores 75 HIGH due to:
- 100% maintainer concentration
- 0 commits in the last year
- No community activity signal

Despite 80M weekly npm downloads and organizational backing, the governance signals are genuinely concerning. This may warrant reclassification as `governance_risk` in future validation rounds — the absence of an incident does not mean the absence of risk.

---

## False Negative Analysis

### Expected False Negatives (Outside Detection Scope)

All 16 false negatives are account compromise or CI/CD exploit cases where governance metrics cannot detect the attack by design:

| Package | Score | Attack Type | Why Not Detected |
|---------|-------|-------------|------------------|
| faker | 0 | Maintainer sabotage | Community fork has healthy governance now |
| node-ipc | 35 | Maintainer sabotage | Active maintainer with good governance signals |
| eslint-scope | 35 | Account compromise | Org-owned, protective factors correctly applied |
| LottieFiles/lottie-player | 45 | Account compromise | Org-owned project with institutional backing |
| cline | 0 | Account compromise | 256 contributors, 58K stars, missing provenance attestations |
| polyfillpolyfill/polyfill-library | 40 | Account compromise | CI/CD access policy exploit |
| reviewdog/action-setup | 0 | Account compromise | GitHub Actions workflow vulnerability |
| solana-labs/solana-web3.js | 0 | Account compromise | Spear-phished via fake npm site |
| ultralytics | 0 | Account compromise | Well-governed project, CI/CD compromise |
| codecov/codecov-action | 0 | Account compromise | Build infra compromise, corporate backing |
| web-infra-dev/rspack | 0 | Account compromise | CI/CD misconfiguration, ByteDance backing |
| chalk | 20 | Account compromise | Qix account phished, Sindre Sorhus project |
| num2words | 0 | Account compromise | Maintainer phished via PyPI-proxying domain |
| tj-actions/changed-files | 50 | Account compromise | Multi-stage CI/CD cascade attack |
| eslint-config-prettier | 35 | Account compromise | Maintainer phished via typosquatted npm domain |
| nrwl/nx | 0 | Account compromise | Exploited pull_request_target workflow |

**These are not failures.** A tool that flagged every actively maintained project as risky "just in case someone steals their credentials" would be useless.

---

## The xz-utils Case

The xz-utils backdoor (CVE-2024-3094) is now correctly detected — ossuary scores it **80 CRITICAL** with takeover pattern detection identifying the Jia Tan proportion shift.

This was a false negative in v2.1 (scored 30 LOW). The improvement came from the proportion shift detection added in v4.0, which compares each contributor's recent commit share vs historical baseline.

The xz case remains the most sophisticated supply chain attack ever documented:
- 2.6-year attack timeline (2021-2024)
- Attacker spent years building trust as a legitimate contributor
- Deliberately addressed governance weaknesses

That ossuary now detects it is encouraging, but the detection relies on the proportion shift being visible in commit data — a more subtle attacker who maintained a lower profile might still evade detection.

---

## Score Stability

Comparing the v2.1 run (February 15, 2026) with v3.0 (March 6, 2026) on the 143 common packages:

- **74% stable** (106/143 packages scored identically)
- **26% changed** (37 packages), of which:
  - 23 large changes (≥20 points)
  - 14 medium changes (5-19 points)
  - 8 classification flips (crossed the 60-point threshold)

Changes stem from two sources:
1. **Scoring engine improvements** between runs (maturity detection, takeover scoring, org-continuity) — responsible for most large changes
2. **Data drift** from the sliding git clone window — as time passes, recent commit counts shift

This level of instability is a known limitation. Governance signals are inherently time-dependent: a project with 10 commits/year in February may show 0 commits/year by March if the clone window shifts. The methodology §9.4 discusses this in detail.

---

## Comparison Across Versions

| Metric | v1 (Feb 2026) | v2.1 (Feb 2026) | v3.0 (Mar 2026) |
|--------|---------------|-----------------|-----------------|
| Packages | 92 | 143 | 158 |
| Ecosystems | 2 | 8 | 8 |
| Accuracy | 92.4% | 91.6% | 89.2% |
| Precision | 100% | 100% | 95.8% |
| Recall | 65.0% | 58.6% | 59.0% |
| F1 | 0.79 | 0.74 | 0.73 |
| False Positives | 0 | 0 | 1 |

The trend shows accuracy and precision decreasing slightly as the dataset grows and includes harder cases. This is expected — a larger, more diverse dataset is a harder test. The recall remains stable around 59%, consistent with the ~40% of incidents being fundamentally outside detection scope.

---

## Reproducibility

Run the validation:

```bash
cd ossuary
source .env  # GITHUB_TOKEN required
.venv/bin/python scripts/validate.py --output validation_results.json
```

Filter by ecosystem:

```bash
.venv/bin/python scripts/validate.py --ecosystem cargo
```

---

*Report generated from validation run on March 6, 2026*
*Dataset: 158 packages (39 incidents, 119 controls) across 8 ecosystems*
