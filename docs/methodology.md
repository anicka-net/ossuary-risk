# Ossuary Scoring Methodology

This document describes the methodology used by Ossuary to assess governance-based supply chain risk in open source packages.

## Executive Summary

Ossuary calculates a risk score (0-100) based on observable governance signals in public package metadata. The methodology focuses on detecting **governance failures** - conditions that historically precede supply chain attacks like maintainer abandonment, frustration-driven sabotage, or social engineering takeovers.

**Key Finding**: In validation testing, the methodology achieved **91.4% accuracy** on 93 packages, with **92.9% precision**, detecting governance-related risks before incidents occur.

**Version**: 2.0 (February 2026)
**Validation Dataset**: 93 packages across npm and PyPI ecosystems

---

## 1. Problem Statement

### 1.1 The Governance Gap

Modern software relies heavily on open source dependencies. A typical application may have hundreds of transitive dependencies, many maintained by individuals or small teams. When these maintainers become unavailable, frustrated, or compromised, the entire dependency chain is at risk.

### 1.2 Historical Incidents

| Incident | Year | Attack Vector | Governance Failure |
|----------|------|---------------|-------------------|
| event-stream | 2018 | Malicious maintainer takeover | Abandoned package handed to stranger |
| colors/faker | 2022 | Intentional sabotage | Frustrated single maintainer |
| xz-utils | 2024 | Social engineering | Sole maintainer, 2-year grooming |
| ua-parser-js | 2021 | Account compromise | Single point of failure |

### 1.3 Research Question

> Can publicly observable metadata predict which packages are vulnerable to governance-based attacks **before** incidents occur?

---

## 2. Detection Scope

### 2.1 What Ossuary Detects

| Signal | Description | Example |
|--------|-------------|---------|
| **Maintainer Abandonment** | Single maintainer with declining activity | event-stream pre-2018 |
| **High Concentration Risk** | >90% commits from one person | minimist, rimraf |
| **Economic Frustration** | Burnout/resentment signals in communications | colors pre-2022 |
| **Governance Centralization** | No succession plan, single point of failure | husky |

### 2.2 What Ossuary Cannot Detect

| Attack Type | Why Undetectable | Example |
|-------------|------------------|---------|
| **Account Compromise** | Active project, healthy governance metrics | ua-parser-js |
| **Insider Sabotage** | Trusted maintainer with good signals | node-ipc |
| **Typosquatting** | New package, no governance to analyze | crossenv |
| **Dependency Confusion** | Build system attack, not governance | PyTorch-nightly |

These are classified as **expected false negatives** - the methodology explicitly does not attempt to detect them.

---

## 3. Scoring Formula

```
Final Score = Base Risk + Activity Modifier + Protective Factors
              (20-100)     (-30 to +20)        (-100 to +20)

Score Range: 0-100 (clamped)
```

### 3.1 Base Risk (Maintainer Concentration)

The primary risk signal is **bus factor** - how many people control the codebase.

| Concentration | Base Score | Interpretation |
|---------------|------------|----------------|
| <30% | 20 | Distributed - healthy |
| 30-49% | 40 | Moderate concentration |
| 50-69% | 60 | Elevated concentration |
| 70-89% | 80 | High concentration |
| ≥90% | 100 | Critical - single maintainer |

**Calculation**: Concentration = (commits by top contributor / total commits) × 100

Only commits from the last 3 years are considered to reflect current governance state.

### 3.2 Activity Modifier

Activity level indicates whether maintainers are engaged and responsive.

| Commits/Year | Modifier | Interpretation |
|--------------|----------|----------------|
| >50 | -30 | Actively maintained |
| 12-50 | -15 | Moderately active |
| 4-11 | 0 | Low activity |
| <4 | +20 | Appears abandoned |

**Rationale**: Abandoned packages are prime targets for takeover attacks (event-stream pattern).

### 3.3 Protective Factors

Protective factors can reduce (or increase) risk based on governance quality signals.

#### Risk Reducers (Negative Points)

| Factor | Points | Threshold | Rationale |
|--------|--------|-----------|-----------|
| **Tier-1 Reputation** | -25 | Score ≥60 | Established maintainers have more to lose |
| **Tier-2 Reputation** | -10 | Score ≥30 | Some track record |
| **GitHub Sponsors** | -15 | Has sponsors | Economic sustainability reduces frustration |
| **Organization (3+ admins)** | -15 | Org with succession | Reduces bus factor |
| **Massive Visibility** | -20 | >50M weekly downloads | High scrutiny |
| **High Visibility** | -10 | >10M weekly downloads | Moderate scrutiny |
| **Distributed Governance** | -10 | <40% concentration | Already healthy |
| **Active Community** | -10 | >20 contributors | Community resilience |
| **CII Best Practices** | -10 | Badge present | Security maturity |
| **Positive Sentiment** | -5 | Score >0.3 | Healthy maintainer mood |

#### Risk Increasers (Positive Points)

| Factor | Points | Condition | Rationale |
|--------|--------|-----------|-----------|
| **Frustration Detected** | +20 | Keywords found | colors/faker pattern |
| **Negative Sentiment** | +10 | Score <-0.3 | Pre-sabotage warning |

---

## 4. Maintainer Reputation System

Reputation provides a composite assessment of maintainer trustworthiness and investment in the ecosystem.

### 4.1 Reputation Signals

| Signal | Points | Threshold |
|--------|--------|-----------|
| **Account Tenure** | +15 | >5 years on GitHub |
| **Portfolio Quality** | +15 | ≥50 original repos with ≥10 stars each |
| **Total Stars** | +15 | ≥50,000 stars across repos |
| **Sponsor Support** | +15 | ≥10 GitHub sponsors |
| **Packages Published** | +10 | ≥20 packages maintained |
| **Top Package Maintainer** | +15 | Maintains top-1000 ecosystem package |
| **Recognized Org** | +15 | Member of nodejs, python, apache, etc. |

### 4.2 Reputation Tiers

| Tier | Score Range | Risk Reduction |
|------|-------------|----------------|
| TIER_1 | ≥60 | -25 points |
| TIER_2 | 30-59 | -10 points |
| UNKNOWN | <30 | 0 points |

### 4.3 Recognized Organizations

Membership in these organizations confers institutional backing:

- **JavaScript/Node**: nodejs, openjs-foundation, npm, expressjs, eslint, webpack, babel
- **Python**: python, psf, pypa, pallets, django, tiangolo
- **General**: apache, cncf, linux-foundation, mozilla, rust-lang, golang
- **Cloud/Infra**: kubernetes, docker, hashicorp

---

## 5. Sentiment Analysis

### 5.1 Approach

Ossuary analyzes commit messages and issue discussions for:

1. **General Sentiment**: Using VADER sentiment analysis
2. **Frustration Keywords**: Pattern matching for burnout/exploitation signals

### 5.2 Frustration Keywords

High-signal keywords that historically preceded sabotage:

```
"not getting paid", "unpaid work", "free labor", "corporate exploitation",
"burned out", "burnout", "stepping down", "abandoning this project",
"fortune 500", "pay developers", "companies make millions",
"protest", "on strike", "boycott", "resentment", "exploitation"
```

### 5.3 Sentiment Scoring

| Compound Score | Effect |
|----------------|--------|
| < -0.3 | +10 risk points |
| > 0.3 | -5 risk points |
| Otherwise | Neutral |

---

## 6. Risk Levels

| Score | Level | Semaphore | Recommended Action |
|-------|-------|-----------|-------------------|
| 0-19 | VERY_LOW | Green | Routine monitoring |
| 20-39 | LOW | Green | Quarterly review |
| 40-59 | MODERATE | Yellow | Monthly review |
| 60-79 | HIGH | Orange | Weekly review, contingency plan |
| 80-100 | CRITICAL | Red | Immediate action required |

---

## 7. Validation Methodology

### 7.1 Dataset Construction

The validation dataset includes:

1. **Known Incidents** (9 packages): Packages with documented supply chain incidents
2. **Governance Risk** (11 packages): Packages with elevated risk signals but no incident (yet)
3. **Control Group** (73 packages): Popular packages with healthy governance

Total: 100 packages (93 successfully analyzed, 7 had missing repository URLs)

### 7.2 Classification Rules

| Expected | Predicted Score | Classification |
|----------|-----------------|----------------|
| Incident/Risk | ≥60 | True Positive (TP) |
| Incident/Risk | <60 | False Negative (FN) |
| Safe | <60 | True Negative (TN) |
| Safe | ≥60 | False Positive (FP) |

### 7.3 Results (n=93)

```
Accuracy:   91.4%
Precision:  92.9%
Recall:     65.0%
F1 Score:   0.76

Confusion Matrix:
  TP: 13  |  FN: 7
  FP: 1   |  TN: 72
```

### 7.4 Performance by Category

| Category | Detection Rate | Notes |
|----------|---------------|-------|
| Governance Failure | 50% (1/2) | event-stream detected |
| Governance Risk | 82% (9/11) | Primary target category |
| Account Compromise | 50% (2/4) | Expected low - outside scope |
| Maintainer Sabotage | 33% (1/3) | Expected low - insider threat |
| Control (Safe) | 99% (72/73) | Very low false positive rate |

### 7.5 False Negative Analysis

Expected false negatives (outside detection scope):

| Package | Attack Type | Why Not Detected |
|---------|-------------|------------------|
| ua-parser-js | Account compromise | Active project with healthy metrics |
| node-ipc | Insider sabotage | Trusted maintainer, good signals |
| eslint-scope | Account compromise | Org-owned, protective factors apply |
| faker | Maintainer sabotage | Community fork now has good governance |

### 7.6 T-1 Validation (Predictive Power)

To validate **predictive** capability, we scored packages at a cutoff date *before* their incidents occurred:

| Package | Incident Date | Cutoff Date | T-1 Score | Current Score | Detected? |
|---------|---------------|-------------|-----------|---------------|-----------|
| event-stream | 2018-09-16 | 2018-09-01 | 80 | 80 | Yes |
| colors | 2022-01-08 | 2022-01-01 | 100 | 100 | Yes |
| coa | 2021-11-04 | 2021-11-01 | 100 | 100 | Yes |
| rc | 2021-11-04 | 2021-11-01 | 100 | 100 | Yes |

**Result**: 100% detection rate for governance-detectable incidents at T-1.

This demonstrates that the methodology could have flagged these packages **before** their incidents occurred, validating the predictive value of governance metrics.

---

## 8. Limitations

### 8.1 Methodological Limitations

1. **GitHub-centric**: Relies on GitHub metadata; other forges have limited support
2. **Historical data**: Git history can be rewritten; metrics reflect current state
3. **English bias**: Sentiment analysis optimized for English text
4. **API rate limits**: Full analysis requires authenticated GitHub API access

### 8.2 Detection Limitations

1. **Cannot detect insider threats** from trusted maintainers with good signals
2. **Cannot detect account compromise** on active, well-governed projects
3. **Cannot detect typosquatting** (new packages have no governance history)
4. **May flag healthy "done" packages** as risks (false positives on stable utilities)

### 8.3 Temporal Limitations

1. **Reputation data is current-state**: Stars, sponsors, repos reflect present, not historical
2. **Organization membership is current**: Historical org membership not tracked
3. **Download counts are current**: Cannot assess historical visibility

---

## 9. Recommendations for Use

### 9.1 Integration Patterns

1. **CI/CD Pipeline**: Score dependencies on PR, fail on CRITICAL
2. **Scheduled Audits**: Weekly scans of dependency tree
3. **Acquisition Diligence**: Score target's OSS dependencies
4. **Vendor Assessment**: Evaluate third-party software stacks

### 9.2 Score Interpretation

| Score Range | Action |
|-------------|--------|
| 0-39 | Standard dependency management |
| 40-59 | Add to watchlist, review quarterly |
| 60-79 | Investigate alternatives, prepare fork |
| 80-100 | **Immediate review**, consider removal |

### 9.3 Combining with Other Tools

Ossuary complements but does not replace:

| Tool Type | Purpose | Ossuary Relationship |
|-----------|---------|---------------------|
| **SBOM tools** | Inventory what you have | Provides risk context |
| **Vulnerability scanners** | Known CVEs | Different risk dimension |
| **License scanners** | Compliance risk | Orthogonal concern |
| **Ossuary** | Governance/abandonment risk | Predictive, not reactive |

---

## 10. Future Work

1. **Expand ecosystem support**: RubyGems, Cargo, Go modules
2. **Historical snapshots**: Archive reputation/org data for better T-1 analysis
3. **ML enhancement**: Train classifier on larger incident corpus
4. **Dependency graph analysis**: Transitive risk aggregation
5. **Maintainer network analysis**: Identify shared maintainer risks across packages

---

## References

1. Backstabber's Knife Collection - https://dasfreak.github.io/Backstabbers-Knife-Collection/
2. Sonatype State of the Software Supply Chain - https://www.sonatype.com/state-of-the-software-supply-chain
3. OpenSSF Scorecard - https://securityscorecards.dev/
4. SLSA Framework - https://slsa.dev/
5. Socket.dev Security Reports - https://socket.dev/blog

---

*Document version: 2.0*
*Last updated: February 2026*
*Validation dataset: 93 packages (91.4% accuracy, 92.9% precision)*
