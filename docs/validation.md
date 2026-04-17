# Validation Report

**Version**: 3.1 (March 2026)
**Dataset**: 164 packages across 8 ecosystems
**Scoring**: Tapered concentration window (v3.1)

---

## Summary

Ossuary detects governance risk — structural conditions in a project's maintenance that make it vulnerable to supply chain attack. It does not detect all supply chain attacks, only those where governance signals were observable before the incident.

The validation uses a **scoped evaluation framework** (Scope B) that counts only in-scope incidents toward recall. Out-of-scope incidents (credential theft on healthy projects, CI/CD exploits) are included in the dataset to validate the detection boundary, but are not penalized as false negatives.

| Metric | All incidents | In-scope (Scope B) |
|--------|-------------|-------------------|
| **Accuracy** | 87.2% | 94.7% |
| **Precision** | 96.2% | 96.0% |
| **Recall** | 55.6% | 77.4% |
| **F1 Score** | 0.70 | 0.857 |
| **False Positives** | 1 (rxjs) | 1 (rxjs) |

---

## Scoped Validation Framework

### Why scoped metrics

Reporting a single recall number across all incident types conflates fundamentally different attack classes. A tool that detected every credential theft would need to flag every package (since any maintainer can be phished), producing unacceptable false positive rates. Scoped metrics separate what the tool *claims to detect* from what it *acknowledges it cannot detect*.

### Tier definitions

An incident is **in-scope** if governance weakness was observable from public data before the attack:

| Tier | Label | In-scope? | Rule |
|------|-------|-----------|------|
| T1 | Governance decay → compromise | Yes | Governance weakness was the enabling condition |
| T2 | Protestware / sabotage | Yes | Bus factor 1 enabled unilateral action |
| T3 | Account compromise + weak governance | Yes | Credential attack, but governance weakness also present |
| T4 | Account compromise + strong governance | No | Credential attack on healthy project |
| T5 | CI/CD pipeline exploit | No | Different attack surface entirely |
| T_risk | Governance risk, no incident (yet) | Yes | Validates risk identification before incidents occur |

### Decision procedure for borderline cases

For each incident: (1) Would Ossuary's signals have shown elevated risk before the attack? (2) Was the governance weakness the enabling condition or merely coincidental? If both yes → in-scope.

---

## Per-Tier Detection Rates

| Tier | Detected | Rate | Notes |
|------|----------|------|-------|
| T1: Governance decay | 8/9 | **89%** | 1 miss: polyfill.io (ownership transfer) |
| T2: Protestware / sabotage | 2/6 | **33%** | 4 misses: all reputation-protected maintainers |
| T3: Weak-gov compromise | 4/4 | **100%** | All detected |
| T_risk: Governance risk | 10/12 | **83%** | 2 misses: core-js (very active), devise (borderline drift) |
| T4: Strong-gov compromise (OOS) | 1/8 | 12% | Expected — out of scope |
| T5: CI/CD exploits (OOS) | 0/6 | 0% | Expected — out of scope |

**Combined in-scope (Scope B)**: 24/31 = 77.4% recall.

### Key finding: reputation-protected single-maintainer projects

T2 (protestware) is the weakest in-scope tier at 33%. This is because protestware maintainers tend to have strong reputations (medikoo, ForbesLindesay, Marak). The model correctly identifies that reputation reduces risk, but this means it cannot detect unilateral action by reputable maintainers. This is a genuine trade-off, not a bug — reputation DOES reduce the probability of malicious action.

---

## Dataset Composition

### By Ecosystem

| Ecosystem | Incidents | Controls | Total |
|-----------|-----------|----------|-------|
| npm | 23 | 43 | 66 |
| PyPI | 4 | 42 | 46 |
| Cargo | 0 | 8 | 8 |
| RubyGems | 3 | 8 | 11 |
| Packagist | 0 | 5 | 5 |
| NuGet | 0 | 4 | 4 |
| Go | 1 | 4 | 5 |
| GitHub | 13 | 5 | 18 |
| **Total** | **45** | **119** | **164** |

### By Tier

| Category | Count |
|----------|-------|
| T1: Governance decay | 9 |
| T2: Protestware / sabotage | 6 |
| T3: Weak-gov compromise | 4 |
| T4: Strong-gov compromise | 8 |
| T5: CI/CD exploits | 6 |
| T_risk: Governance risk | 12 |
| Controls | 119 |

---

## Confusion Matrix (Scope B)

```
                    Predicted Risky    Predicted Safe
Actually Risky         24 (TP)             7 (FN)
Actually Safe           1 (FP)           118 (TN)
```

Out-of-scope incidents (14 packages) excluded from this matrix. They appear in the full-dataset metrics but not the scoped metrics.

---

## False Positive Analysis

### rxjs (Score: 75 HIGH)

Persistent false positive. rxjs scores 75 HIGH due to:
- 100% maintainer concentration
- 0 commits in the last year
- No community activity signal

Despite organizational backing and 80M weekly npm downloads, the governance signals are genuinely concerning. This may warrant reclassification as `governance_risk` — the absence of an incident does not mean the absence of risk.

### sidekiq — eliminated by tapered concentration

In the previous validation run (March 6), sidekiq scored 60 HIGH (FP). One week later, with no governance change, it had shifted from 40→60 due to a 2.3% concentration swing when commits crossed the 12-month boundary. The tapered concentration window (v3.1) smooths this to 40 (TN). See Score Stability below.

---

## In-Scope False Negative Analysis

All 7 in-scope false negatives are explainable:

| Package | Score | Tier | Why missed |
|---------|-------|------|-----------|
| faker | 0 | T2 | Evaluating community fork (faker-js/faker); original repo deleted |
| node-ipc | 50 | T2 | Active development masks bus-factor-1 risk |
| polyfill.io | 40 | T1 | Ownership transfer to malicious CDN is an untracked signal |
| devise | 50 | T_risk | Borderline; concentration drift from minor changes |
| core-js | 40 | T_risk | High activity gives discount despite 92% concentration |
| es5-ext | 40 | T2 | 100% concentration but maintainer (medikoo) has strong reputation |
| is-promise | 45 | T2 | Reputation correctly reconstructed at 2020 cutoff |

**faker**: The original Marak/faker.js repo was deleted. We evaluate the community fork (faker-js/faker), which has healthy governance — score 0 is correct for the current project state.

**node-ipc**: RIAEvangelist maintained active development right up until injecting protestware. Active maintenance is a positive governance signal; the model correctly weights it as such.

**polyfill.io**: The project was sold to Funnull (a Chinese CDN company) who injected malicious JavaScript into 100K+ websites. Ownership transfers are not currently tracked as a signal. Acknowledged limitation.

**devise**: José Valim (Elixir creator) maintains devise with 83% concentration. Borderline case — the single-maintainer risk is real but Valim's standing in the community is a mitigating factor.

**core-js**: Denis Pushkarev (zloirock) was imprisoned Jan-Oct 2020, leaving the project unmaintained. Score 40 reflects the current state — he's back and actively committing. Bus factor 1 with 92% concentration is a real risk, but high activity correctly moderates the score.

**es5-ext**: 100% concentration but maintainer medikoo has strong reputation. Demonstrates the trade-off: bus factor 1 enables unilateral action, but reputable maintainers ARE genuinely lower risk.

**is-promise**: ForbesLindesay's portfolio and tenure are correctly reconstructed at the 2020 cutoff date, reducing protective factors. Score 45 (was 70 when historical reputation was incorrectly stripped). The accurate historical scoring gives the honest result — this maintainer was reputable in 2020.

---

## Out-of-Scope Incident Analysis

14 out-of-scope incidents are included in the dataset to validate detection boundaries. All score below 60 as expected:

### T4: Account compromise on healthy projects (8 cases)

| Package | Score | Attack Vector |
|---------|-------|---------------|
| ua-parser-js | 90 | Email hijacking (bonus detection — above threshold) |
| eslint-scope | 35 | Account compromise, OpenJS Foundation |
| LottieFiles/lottie-player | 45 | Account compromise, org-backed |
| chalk (2025) | 35 | Qix phished, Sindre Sorhus project |
| cline | 0 | npm account compromise, 256 contributors |
| solana-web3.js | 0 | Spear-phished via fake npm domain |
| eslint-config-prettier | 55 | JounQin phished via typosquatted domain |
| num2words | 0 | Phished via fake PyPI domain, org-backed (savoirfairelinux) |

ua-parser-js is a bonus detection — above threshold despite being T4, because the project had governance concentration signals at the 2021 cutoff.

### T5: CI/CD pipeline exploits (6 cases)

| Package | Score | Attack Vector |
|---------|-------|---------------|
| reviewdog/action-setup | 0 | CI/CD contributor access exploit |
| codecov/codecov-action | 0 | Docker HMAC extraction |
| web-infra-dev/rspack | 0 | GitHub Actions pwn request |
| ultralytics | 0 | GitHub Actions cache poisoning |
| tj-actions/changed-files | 50 | Cascading CI/CD exploit (SpotBugs → reviewdog → tj-actions) |
| nrwl/nx | 0 | pull_request_target exploit |

All correctly score below threshold. CI/CD exploits target build infrastructure, not package governance.

---

## Score Stability

### The problem (pre-v3.1)

Comparing two validation runs one week apart (March 6 vs March 13, hard 12-month cutoff):

- 95% of scores stable (149/157 common packages)
- 8 packages changed by ±10-20 points
- sidekiq crossed the threshold (40→60), creating a phantom false positive

Root cause: the hard 12-month cutoff for concentration. A single commit crossing the 365-day boundary shifts concentration by 2-3%, which amplifies into ±20 point score changes.

### The fix (v3.1): tapered concentration

Activity count keeps the hard 12-month cutoff (the activity modifier uses coarse buckets >50/≥12/≥4/<4 that are insensitive to boundary noise). Concentration uses a tapered window:

- 0-10 months: weight 1.0 (fully recent)
- 10-14 months: weight fades linearly from 1.0 → 0.0
- 14+ months: weight 0.0 (excluded)

A commit at month 11 doesn't vanish when it ages to month 13 — it gradually fades. This eliminates the cliff edge that caused phantom threshold crossings.

### Result

Compared to the hard-cutoff run on the same data:
- 6 scores changed (vs 8 unstable in the hard-cutoff comparison)
- sidekiq: 60→40 (FP eliminated)
- No TPs lost
- Precision: 92.3%→96.0%

The taper is not artificial smoothing — a commit from 11 months ago genuinely shouldn't have the same weight as one from last month. The hard cutoff was the artifact.

### Bus factor (v3.2): CHAOSS-aligned contributor diversity

Top-1 concentration can be misleading. A project like trivy (18% top-1) looks
distributed, but only 3 people account for 50% of commits (bus factor = 3).
v3.2 adds the CHAOSS bus factor metric alongside concentration, using the
worse signal as base risk floor:

| Bus factor | Base risk floor | Interpretation |
|-----------|----------------|----------------|
| 1 | 60 | Single person dominates (concentration already captures this) |
| 2 | 40 | Two people control the project |
| 3–5 | 40 | Small group, moderate risk |
| 6+ | 20 | Well-distributed |

Bus factor only raises the floor — it never lowers base risk below what
concentration gives. A/B testing confirmed **zero impact on Scope B
classifications** (all affected packages are controls, not incidents).

### Historical reputation reconstruction (v3.2)

Historical scoring (T-1 analysis) previously stripped all current-state
metadata. v3.2 reconstructs what is verifiable at the cutoff date:

| Signal | Reconstruction method |
|--------|----------------------|
| Tenure | Compute account age at cutoff (immutable) |
| Portfolio | Filter repos by `created_at` ≤ cutoff |
| Stars | Sum from repos that existed at cutoff (conservative) |
| Org membership | Pass through (stable for recognized foundations) |
| Sponsors | Cannot reconstruct — set to 0 |

This narrows the gap between current and historical scores. The cost is
1 lost TP: is-promise (70→45) because ForbesLindesay's portfolio is now
correctly recognized at the 2020 cutoff. A/B testing confirmed this is the
**sole source** of the recall change (80.6%→77.4%).

---

## The xz-utils Case

The xz-utils backdoor (CVE-2024-3094) is detected — ossuary scores it **80 CRITICAL** with takeover pattern detection identifying the Jia Tan proportion shift (+49.5pp: 0.8% historical → 50% recent).

This detection relies on the proportion shift being visible in commit data. A more subtle attacker maintaining a lower profile might evade detection. See [methodology §4.4](methodology.md) for technical details.

---

## Comparison Across Versions

| Metric | v1 (Feb 2026) | v2.1 (Feb 2026) | v3.0 (Mar 2026) | v3.1 (Mar 2026) |
|--------|---------------|-----------------|-----------------|-----------------|
| Packages | 92 | 143 | 158 | 164 |
| Ecosystems | 2 | 8 | 8 | 8 |
| Scope | All | All | All | Scope B |
| Accuracy | 92.4% | 91.6% | 89.2% | 94.7% |
| Precision | 100% | 100% | 95.8% | 96.0% |
| Recall | 65.0% | 58.6% | 59.0% | 77.4% |
| F1 | 0.79 | 0.74 | 0.73 | 0.857 |
| False Positives | 0 | 0 | 1 | 1 |

The v3.1 recall improvement (59%→77%) reflects the scoped framework, not a model improvement. The model correctly stopped penalizing for undetectable attack types. The modest recall (77.4% vs earlier 80.6%) is the cost of honest historical scoring — reconstructing verifiable reputation at cutoff dates rather than stripping it entirely. Historical mode is also intentionally conservative for unstable GitHub-only factors: present-day stars are not backfilled into the past, and issue/comment sentiment is disabled for T-1 scoring because the GitHub issue snapshot is current and incomplete.

---

## Reproducibility

Run the validation:

```bash
cd ossuary
source .env  # GITHUB_TOKEN required
.venv/bin/python scripts/validate.py --output validation_results.json
```

Run scope analysis:

```bash
python thesis/analyze_scopes.py
```

Filter by ecosystem:

```bash
.venv/bin/python scripts/validate.py --ecosystem cargo
```

---

## Data Completeness

### Incident population, not sample

The validation dataset is not a sample from a larger population — it is
effectively a **census** of known governance-relevant supply chain incidents
in the 8 supported ecosystems.

Three major incident catalogs were cross-referenced:

| Catalog | Total incidents | Relevant to Ossuary |
|---------|----------------|-------------------|
| CNCF TAG-Security | ~89 | ~15 (rest are CI/CD, firmware, mobile) |
| IQT Labs / Atlantic Council | ~182 | ~20 (rest are typosquatting, mobile, proprietary) |
| Ladisa et al. SoK (IEEE S&P 2023) | 94 | ~15 (overlap with above) |

Additionally consulted: Socket.dev blog, Snyk advisories, Sonatype timeline,
Backstabber's Knife Collection (Ohm et al., 174 packages), Datadog malicious
packages dataset (19K+ packages), open-source-peace protestware list.

The catalogs overlap heavily. The total unique population of documented supply
chain compromises of **legitimate projects** (not typosquatting or malware
uploads) in npm, PyPI, Cargo, RubyGems, Packagist, NuGet, Go, and GitHub is
approximately **50 incidents**. Our dataset contains **45 of these**. The
remainder have deleted GitHub repositories and cannot be scored:

| Excluded | Why |
|----------|-----|
| phpass (hautelook/phpass) | GitHub org deleted, repo 404 |
| electron-native-notify | Repo deleted, npm placeholder |
| @ledgerhq/connect-kit | Repo 404 |
| getcookies | Repo 404 |
| crossenv | Typosquatting — no legitimate repo to score |

### Implications for statistical analysis

This is a **rare-event population**, not a sampling problem. Governance-
detectable supply chain attacks on major packages have occurred approximately
50 times in the observable history of package ecosystems (2003–2026). No
amount of additional data collection will substantially increase n.

This explains why:
- **ML cannot beat hand-tuning**: n≈45 incidents is too few to learn nonlinear
  feature interactions (XGBoost achieves F1 0.787 vs hand-tuned 0.857)
- **Bootstrap confidence intervals are wide**: recall 62–91% at 95% CI
  reflects the genuine uncertainty from a small population
- **Per-tier rates have limited statistical power**: T3 at 4/4 (100%) is
  encouraging but the CI includes 40–100%

The appropriate statistical framing is **exact binomial confidence intervals**
on a near-complete population, not inference from a sample to a larger
population.

---

*Report generated from validation run on March 20, 2026*
*Dataset: 164 packages (45 incidents, 119 controls) across 8 ecosystems*
