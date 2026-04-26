# Ossuary Scoring Methodology

This document describes the methodology used by Ossuary to assess governance-based supply chain risk in open source packages.

## Executive Summary

Ossuary calculates a risk score (0-100) based on observable governance signals in public package metadata. The methodology focuses on detecting **governance failures** - conditions that historically precede supply chain attacks like maintainer abandonment, frustration-driven sabotage, or social engineering takeovers.

**Key Finding**: In validation testing against 170 packages across 8 ecosystems using the §5.5 per-tier scope framework (T1 governance decay, T2 protestware, T3 weak-gov compromise, T_risk governance risk are in-scope; T4 strong-gov compromise and T5 CI/CD exploits are out of scope), the v6.3 methodology achieves **96.0% precision** (1 false positive: rxjs) and **75.0% in-scope recall** (F1 0.842) on n = 152 in-scope cases. The dataset was extended in April 2026 with the TeamPCP campaign (`xinference`, `litellm` as T4 EXPECTED FN; `telnyx` as a T3 near-miss FN at score 55, five points below the 60-point threshold — see §5.7.1). Recall moved from 77.4 % to 75.0 % through composition alone (one new in-scope incident added, no offsetting TP); precision and FP count are unchanged. Out-of-scope incidents (credential theft on healthy projects, CI/CD exploits) are included in the dataset to validate detection boundaries but are not penalized as false negatives. v6.3 itself was driven by the §5.10.1 factor ablation: the frustration weight was lowered from +20 to +15 (rayon flipped FP→TN, no TPs lost) and the sentiment scoring branch was removed (0/170 fires on the validation set); see §6.3 and §6.4.1.

**Version**: 6.3 (April 2026)
**Validation Dataset**: 170 packages across npm, PyPI, Cargo, RubyGems, Packagist, NuGet, Go, and GitHub

---

## 1. Problem Statement

### 1.1 The Governance Gap

Modern software relies heavily on open source dependencies. A typical application may have hundreds of transitive dependencies, many maintained by individuals or small teams. When these maintainers become unavailable, frustrated, or compromised, the entire dependency chain is at risk.

### 1.2 Historical Incidents

| Incident | Year | Attack Vector | Governance Signal |
|----------|------|---------------|-------------------|
| event-stream | 2018 | Maintainer handoff to stranger | Abandoned, 75% concentration, "free work" frustration |
| colors/faker | 2022 | Intentional sabotage | 100% concentration, "protest", "exploitation" keywords |
| xz-utils | 2024 | 2.6-year social engineering | Sole maintainer, proportion shift detectable 12 months early |
| left-pad | 2016 | Registry removal | Single maintainer, mass breakage |
| ctx | 2022 | Expired domain takeover | Abandoned, bus_factor=1, years of inactivity |
| polyfill.io | 2024 | Project sold to malicious CDN | Ownership transfer without safeguards |
| ua-parser-js | 2021 | Account compromise | *Not governance-detectable* — active project, healthy metrics |
| chalk (2025) | 2025 | Maintainer phished | *Not governance-detectable* — strong governance, credential theft |
| tj-actions | 2025 | CI/CD cascade exploit | *Not governance-detectable* — CI trust chain, not governance |

### 1.3 Research Question

> Can publicly observable metadata predict which packages are vulnerable to governance-based attacks **before** incidents occur?

---

## 2. Related Work

This section reviews prior academic research relevant to software supply chain security and open source sustainability.

### 2.1 Software Supply Chain Security

The field has seen significant academic attention following high-profile incidents:

**Taxonomies and Systematization**

Ladisa et al. (2023) presented "SoK: Taxonomy of Attacks on Open-Source Software Supply Chains" at IEEE S&P, identifying **12 distinct attack categories** independent of specific languages or ecosystems. This taxonomy provides the foundation for understanding governance-based attacks as a distinct category.

Ohm et al. (2020) published "Backstabber's Knife Collection: A Review of Open Source Software Supply Chain Attacks" at DIMVA, cataloging real-world attacks and establishing patterns that inform detection approaches.

**Scale and Impact**

Research quantifies the growing threat: Sonatype recorded a **700% increase** in supply chain attacks (ICSE '23), while 97% of applications now use open source components with 78% of code originating from OSS (Synopsys 2022).

Torres-Arias et al. (2019) introduced in-toto, a framework for software supply chain integrity, and Lamb & Zacchiroli (2021) addressed "Reproducible Builds" in IEEE Software, focusing on build integrity rather than governance.

**Risk Assessment Frameworks**

The ACM TOSEM paper "Research Directions in Software Supply Chain Security" (2024) identifies three major attack vectors: vulnerabilities in dependencies, infiltration of build infrastructure, and **social engineering** - the last being most relevant to governance-based risks.

### 2.2 Open Source Sustainability and Maintainer Health

**Maintainer Burnout**

Raman et al. (2020) published "Stress and Burnout in Open Source" at ICSE-NIER, finding that toxic conversations demotivate and burn out developers. They developed an SVM classifier to detect toxic discussions - an approach that influenced Ossuary's sentiment analysis component.

Guo et al. (2024) in "Sustaining Maintenance Labor for Healthy Open Source Software Projects" (arXiv) argue that **depleted maintainer capacity** leads to unmaintained projects with security consequences. This directly supports Ossuary's activity modifier component.

**The Bus Factor Problem**

Eghbal's foundational work "Roads and Bridges: The Unseen Labor Behind Our Digital Infrastructure" (Ford Foundation, 2016) found that the **majority of open source projects are maintained by one or two people** - validating maintainer concentration as a key risk signal.

The CHAOSS project formalized this as the "Contributor Absence Factor" metric, which Ossuary operationalizes in its concentration scoring.

### 2.3 The xz-utils Case Study

The xz-utils backdoor (CVE-2024-3094) represents the most sophisticated governance attack documented. Academic analysis (arXiv 2504.17473) details:

- **2.6-year attack timeline** (2021-2024)
- **Phased social engineering**: trust-building → maintainer duties → infrastructure control → code injection
- **Exploitation of maintainer burnout**: The sole maintainer cited "long-term mental health issues" before ceding control

This incident validates Ossuary's approach: the attacker specifically targeted a project with high concentration and a burned-out maintainer - both signals Ossuary detects.

**Proportion Shift Detection (v4.0)**: Ossuary's takeover detection compares each contributor's historical commit share against their recent (12-month) share. Applied to xz-utils, this detects Jia Tan by **March 2023** — a full 12 months before the backdoor was discovered in March 2024. At the March 2023 cutoff Jia Tan's share was 0.6% historical → 31% recent = **+30.4 percentage point shift**, just over the 30pp detection threshold; by January 2024 the shift had grown to +46.5pp (3.5% → 50%). See Section 4.4 for the full time-series.

### 2.4 Existing Tools and Gap Analysis

| Tool | Focus | Gap Addressed by Ossuary |
|------|-------|-------------------------|
| **OpenSSF Scorecard** | Security best practices | Doesn't assess governance/abandonment risk |
| **CHAOSS/Augur** | Community health metrics | No actionable risk score or sentiment analysis |
| **Snyk/Dependabot** | Known vulnerabilities | Reactive to CVEs, not predictive |
| **Socket.dev** | Behavioral analysis | Detects malicious code, not governance risk |
| **deps.dev** | Dependency metadata | Informational, no risk scoring |
| **LFX Insights** | Project criticality ranking | Identifies important projects but not governance risk; complementary as prioritization input |
| **OSSInsight** | GitHub analytics at scale | Raw activity dashboards (stars, commits, PRs) but no risk scoring or governance assessment |

**The Gap**: No existing tool combines maintainer concentration, activity patterns, and frustration signals into a predictive governance risk score validated against historical incidents.

### 2.5 Academic Contribution

Ossuary contributes to this body of research by:

1. **Operationalizing** CHAOSS metrics into an actionable risk score
2. **Adding sentiment analysis** for frustration/burnout detection (extending Raman et al.)
3. **Validating predictively** against real incidents (T-1 analysis)
4. **Achieving 96.0% precision** with 1 false positive across 170 packages (v6.3)
5. **Detecting social engineering takeovers** via proportion shift analysis, validated against the xz-utils timeline (12-month early detection)
6. **Explicitly validating detection boundaries** — including out-of-scope attack types in the validation set to empirically demonstrate what governance scoring can and cannot detect

---

## 3. Detection Scope

### 3.1 What Ossuary Detects

| Signal | Description | Example |
|--------|-------------|---------|
| **Maintainer Abandonment** | Single maintainer with declining activity | event-stream pre-2018 |
| **High Concentration Risk** | >90% commits from one person | minimist, rimraf |
| **Economic Frustration** | Burnout/resentment signals in communications | colors pre-2022 |
| **Governance Centralization** | No succession plan, single point of failure | husky |
| **Newcomer Takeover** | Unknown contributor suddenly dominates a mature project | xz-utils/Jia Tan |

### 3.2 What Ossuary Cannot Detect

| Attack Type | Why Undetectable | Examples | Validation Cases |
|-------------|------------------|----------|-----------------|
| **Account Compromise** | Active project, healthy governance metrics | ua-parser-js, chalk (2025), solana-web3.js, num2words | 8 cases (T4), all expected FN |
| **CI/CD Pipeline Exploits** | Workflow misconfigurations, not governance | tj-actions, reviewdog, rspack, ultralytics, Nx | 6 cases (T5), all expected FN |
| **Protestware by reputable maintainer** | Reputation correctly reduces risk score | es5-ext, is-promise | Detected by scope but missed by threshold (T2 FN) |
| **Typosquatting** | New package, no governance to analyze | crossenv, boltdb-go/bolt | Not tested (no repo to score) |
| **Dependency Confusion** | Build system attack, not governance | PyTorch-nightly | Not tested |

These are classified as **expected false negatives** — the methodology explicitly does not attempt to detect them. The validation set includes 18 out-of-scope cases (T4+T5) to empirically confirm the detection boundary (see §8.6).

### 3.3 The Detection Boundary

The key insight from validation is that governance scoring and credential/CI/CD-based detection are **complementary, not competing** approaches:

- **Governance scoring detects**: Conditions that make attacks possible (abandonment, concentration, frustration)
- **Credential/CI/CD detection requires**: Runtime analysis, provenance attestation, workflow auditing

A well-governed project can still be compromised via phishing (chalk 2025, solana-web3.js) or CI/CD exploits (tj-actions, Nx). Conversely, a poorly-governed project might never be attacked. The two dimensions are orthogonal — organizations should assess both.

### 3.4 Case Study: The 2025 npm Phishing Wave

The 2025 npm supply chain attacks represent the most significant series of package ecosystem compromises to date, affecting over 2.6 billion weekly downloads across multiple interconnected attack waves. Three packages from our validation set — `chalk`, `eslint-config-prettier`, and `is` — were compromised in this campaign, and their contrasting Ossuary scores illustrate the detection boundary precisely.

#### Timeline

| Wave | Date | Vector | Scale |
|------|------|--------|-------|
| eslint-config-prettier | July 17–19 | Phishing via typosquatted `npnjs.com` | 5 packages, 78M weekly downloads |
| `is` package | July 19 | Phished inactive maintainer + social engineering | 1 package, 6 hours undetected |
| s1ngularity / Nx | August 21–31 | GitHub Actions injection → token theft | 500+ packages, 2,349 credentials harvested |
| chalk / "Great Heist" | September 8 | Phishing via `npmjs.help` | 18+ packages, 2.6B weekly downloads |
| Shai-Hulud 1.0 | September | Self-replicating worm using stolen tokens | 500+ packages |
| Shai-Hulud 2.0 | November 21–24 | Unrotated tokens from prior waves | 796 packages, 14,000 secrets exposed |

The waves were causally linked: credentials stolen in the s1ngularity attack (August) were never rotated, directly enabling the Shai-Hulud worm (September–November). CISA issued alert AA25-266A on September 23, and CERT/CC published VU#534320 identifying "longstanding design weaknesses in npm's architecture."

#### Three Packages, Three Outcomes

| Package | Ossuary | Scorecard | Classification | Why |
|---------|---------|-----------|----------------|-----|
| **`is`** | **100 CRITICAL** | 3.4 | **True Positive** | Single inactive maintainer (100% concentration, 0 commits/year), no protective factors |
| **eslint-config-prettier** | 35 LOW | 4.5 | Expected FN | Prettier org, active development, 32 commits/year, multiple contributors |
| **chalk** | 20 LOW | 3.8 | Expected FN | Sindre Sorhus (Tier 1 reputation), established project, strong protective factors (−60) |

All three were compromised by the same attack class (credential phishing), yet Ossuary correctly scored them on opposite sides of the risk threshold. This is not a bug — it reflects the fundamental distinction between **governance vulnerability** and **attack occurrence**:

**`is`** — The `is` package had a single maintainer (enricomarino) who had not committed in years, with 100% contributor concentration and no organizational backing. Ossuary scored it 100 CRITICAL. When the attacker phished this inactive maintainer's npm credentials and then social-engineered the current team into re-granting publish access, the governance weakness was the enabling condition: a dormant account with live publish rights on a package with no review process for ownership changes.

**chalk** — chalk is maintained by Sindre Sorhus, one of npm's most prolific contributors (Tier 1 reputation in Ossuary's system), with an active contributor base and organizational backing. Ossuary scored it 20 LOW. The attacker phished a co-maintainer (Qix-) via a fake `npmjs.help` domain and published malicious versions that intercepted cryptocurrency wallet transactions. The attack succeeded not because of governance weakness but because npm's authentication infrastructure allowed phishable TOTP-based MFA and long-lived publish tokens.

**eslint-config-prettier** — Part of the Prettier organization with active development (32 commits/year). Ossuary scored it 35 LOW. Maintainer JounQin was phished via the typosquatted `npnjs.com` domain, and malicious versions delivered a Windows RAT via disguised postinstall scripts. Again, the attack vector was credential theft against a well-governed project.

#### The Complementarity Argument

The 2025 npm wave demonstrates that governance scoring and credential/infrastructure hardening are **complementary layers**, not alternative approaches:

| Defense Layer | Would Prevent | 2025 Example |
|---------------|---------------|--------------|
| **Governance: Mandatory phishing-resistant MFA** | Credential phishing entirely | `npmjs.help` and `npnjs.com` attacks used phishable TOTP codes; FIDO/WebAuthn keys cannot be phished |
| **Governance: Short-lived scoped tokens** | Cross-wave credential reuse | s1ngularity tokens (August) enabled Shai-Hulud (September–November) because they were never rotated |
| **Governance: Publish review for dormant accounts** | `is`-style social engineering | Inactive maintainer regained publish access via email claim |
| **Governance scoring (Ossuary)** | Identifying high-risk targets | `is` scored 100 CRITICAL — the governance weakness that made it a soft target was detectable months before the attack |
| **Detection: Anomalous publish monitoring** | Rapid exploitation | Aikido detected chalk compromise in 5 minutes; 2.5M downloads still occurred in the 2-hour remediation window |
| **Detection: Credential scanning** | Token reuse chains | The 2,349 tokens leaked by s1ngularity were publicly visible on GitHub; automated scanning would have flagged them |

The critical insight for the thesis: **Ossuary cannot prevent the chalk attack** (score 20, correctly assessed as low governance risk), **but it can identify packages like `is` where governance weakness creates the preconditions for exactly this type of attack** (score 100). A security team using both Ossuary and credential monitoring would have:

1. **Pre-attack**: Flagged `is` as CRITICAL risk (governance scoring), prioritized it for publish-access audit
2. **During attack**: Detected anomalous publish from dormant account (credential monitoring)
3. **Post-attack**: Known that chalk's governance was healthy (score 20) and focused remediation on the credential vector, not governance restructuring

Neither tool alone provides this complete picture. The npm phishing wave is empirical evidence that the two dimensions — governance risk and credential/infrastructure security — require distinct measurement approaches used in concert.

#### References

- CISA Alert AA25-266A: "Widespread Supply Chain Compromise Impacting npm Ecosystem" (September 23, 2025)
- CERT/CC VU#534320: npm architecture design weaknesses
- Sonatype: "npm chalk and debug packages hit in software supply chain attack" (September 2025)
- Aikido Security: Detection timeline for the September 8 chalk compromise
- StepSecurity: "Another npm Supply Chain Attack — The 'is' Package Compromise" (July 2025)
- SafeDep: "eslint-config-prettier: Major npm Supply Chain Hack" (July 2025)

---

## 4. Scoring Formula

```
Final Score = Base Risk + Activity Modifier + Protective Factors
              (20-100)     (-30 to +20)        (-100 to +40)

Score Range: 0-100 (clamped)
```

### 4.-1 Data-completeness contract

Before any of the components below are computed, Ossuary applies a hard
rule: **a score is only produced from complete input data.** If any
upstream fetch (package registry, download stats, GitHub API, git
clone, sentiment analysis) returned a *known* failure — non-2xx HTTP
response, transport exception, malformed payload — the scoring engine
short-circuits and returns ``risk_level = INSUFFICIENT_DATA`` with the
failing inputs listed under ``incomplete_reasons``. No numeric score
is computed and no risk level is inferred.

The reason for this rule is empirical: silent fallbacks (e.g. treating
a rate-limited download-stats fetch as "zero downloads") produce
*different* scores from successful runs without any signal to the user
that the inputs differed. The score for `pyyaml` could swing from 35
LOW to 55 MODERATE between runs minutes apart depending only on
whether `pypistats.org` happened to rate-limit the second call. The
methodology refuses that outcome — better to surface "insufficient
data, retry" than to produce a misleading number.

A failure is *known* when the call returned a status the collector
recognises as a failure mode. Empty results (a package with zero
sponsors, a project with zero recent commits) are valid measurements
and do **not** trigger this contract.

Transient failures are retried before the contract fires:

| Failure class | Backoff | Max retries |
|---|---|---|
| HTTP 429 (rate limit) | `Retry-After` header if present (capped at 30 s); else 5 s | 2 |
| HTTP 5xx (server error) | 1.5 s × attempt | 2 |
| Transport timeout | 1 s × attempt | 2 |
| HTTP 4xx (other) | none — treated as permanent | 0 |
| Transport exception (DNS/TLS) | none — treated as permanent | 0 |

If retries do not recover the call, the score lands as
`INSUFFICIENT_DATA`. Use `ossuary rescore-invalid` to retry every such
package in one pass; the upstream failure is usually transient.

#### Provisional scores: when partial data is still useful

Every protective-factor input that fails silently makes the resulting
score *higher* than the true score, because the missing factor
contributes 0 instead of its negative bonus. So the user-facing
direction is the same in both classes of failure: the score is
conservative (overstated risk), not understated. The two classes
differ in **signal magnitude** and what the missing signal makes us
*blind to*, not in the sign of the bias.

The contract above (`INSUFFICIENT_DATA`) applies to failures that are
load-bearing for the popularity assessment that distinguishes
"well-known and well-watched" from "obscure":

- A failed registry-downloads fetch (PyPI, npm, cargo, RubyGems,
  Packagist, NuGet) means the engine cannot tell a 50M-downloads/week
  package from a 0-downloads/week one. The visibility factor is the
  single largest protective bonus (−10 for >10M, −20 for >50M
  weekly downloads), so its absence can move a score across two
  buckets (e.g. CRITICAL → MODERATE if the bonus had landed).
  Refusing to score is the right call: a number computed without
  this signal collapses the popularity dimension and is not directly
  comparable to a number computed with it.
- A failed GitHub *repo_info* fetch (when transient — 429/5xx/network)
  leaves the engine without an owner type to branch on; downstream
  org-vs-user logic cannot run. Refused.

The provisional class covers failures of *corroborating* protective
signals where the missing factor is smaller and the system can still
distinguish the things it needs to distinguish. The canonical case is
GitHub's auxiliary endpoints (Sponsors, maintainer profile, orgs,
issues, CII badge): each contributes −10 to −15 individually, none of
them are load-bearing for the popularity signal, and refusing to
score whenever any of them fails would render the system unusable
during normal GitHub rate-limit windows.

For this class the engine still computes a number but flags the
breakdown as ``is_provisional = True`` with the failing endpoints in
``provisional_reasons``. Surfaces (CLI, API, dashboard) display a
"⚠ PROVISIONAL" badge so the user knows the number is conservative
and worth retrying. ``rescore-invalid`` retries both INSUFFICIENT_DATA
and provisional rows by default; pass ``--only insufficient`` or
``--only provisional`` to restrict.

| Source | Failure → state | Why |
|---|---|---|
| Registry downloads (PyPI, npm, cargo, RubyGems, Packagist, NuGet) | `INSUFFICIENT_DATA` | Visibility is the largest single protective factor (−10 to −20); without it the engine cannot tell popular from obscure |
| GitHub `repo_info` (transient 429/5xx) | `INSUFFICIENT_DATA` | No owner type → can't run downstream branches |
| GitHub `repo_info` 404 | hard error ("repo not found") | Permanent, not transient |
| GitHub maintainer profile / repos / sponsors / orgs / issues / CII / contributors | `is_provisional` | Each is small (−10 to −15) and corroborating; missing one keeps popularity assessment intact |
| Go proxy `@latest` | logged only | Go has no download API; version display only, not scored |

Both classes produce a *higher* (more cautious) score than the
complete-data run would. The split decides whether the engine should
publish that number at all (`INSUFFICIENT_DATA`) or publish it with a
conservative-bias flag (`is_provisional`).

### 4.-0 Operational SLA — snapshot freshness

Ossuary scores reflect repository data **as of the most recent
snapshot** for the package's canonical repo. Governance signals (bus
factor, concentration, contributor attrition, organisational backing)
are structural and move on the timescale of weeks to quarters, not
hours — the freshness bands reflect that:

| Band | Snapshot age | Meaning |
|---|---|---|
| **Fresh** | ≤ 30 days | Suitable for routine audit-time evidence; aligned with typical release-boundary review cadence under CRA Art. 13(5). |
| **Stale** | 30–90 days | Still defensible for point-in-time judgments but warns the operator. Re-score recommended before formal sign-off. |
| **Expired** | > 90 days | Score is informational only; refresh required before relying on it for an attestation. |

Scores never silently use data older than 90 days for the "current"
view. Historical-cutoff scores (T-1 analyses, validation runs against
past incidents) are exempt from the freshness contract by definition
— their cutoff date sets the relevant horizon.

**First-fetch carve-out.** A package with no prior snapshot is always
fetched on demand — the SLA bands apply to *refresh latency*, not to
first-time scoring of an unfamiliar dependency. An operator pasting a
new package into the CLI gets a Fresh score immediately; the bands
govern the cache hit path.

**Why these bands and not tighter.** Two reasons:

1. **Governance signals are structural.** A bus factor of 1 doesn't
   become a bus factor of 4 overnight; an organisation doesn't
   appear or disappear in days. Scoring on data ≤ 30 days old
   captures every meaningful change in the underlying signal class.
2. **Tight bands invite flapping.** Day-to-day rescore noise
   (paginated API result ordering, transient signal failures
   degrading to provisional, sentiment analyser variance) becomes
   indistinguishable from real change at sub-week SLAs. A monthly
   floor enforces that any score change between two refreshes
   reflects a real shift in the project, not a measurement
   artefact. If two refreshes 30 days apart give different scores
   on the same underlying state, that's a *bug to investigate*, not
   noise the SLA should tolerate.

**Implementation note.** Each `Score` row carries
``data_snapshot_at`` (when the underlying CollectedData was fetched)
alongside ``calculated_at`` (when the formula ran on it). The
distinction matters because a methodology version bump produces a
new ``calculated_at`` against an older ``data_snapshot_at`` —
formula iteration does not invalidate the snapshot cache (see
`docs/data_reuse_design.md`). The freshness band shown to the user
is computed from ``data_snapshot_at``, not ``calculated_at``.

### 4.0 Two-Track Scoring (Mature vs. Non-Mature Projects)

A critical insight from validating against real-world package inventories: the original scoring model conflated "stable/finished" with "abandoned." A project like argon2 or dosfstools — quietly maintained for 15 years with occasional small edits — would score identically to a package whose maintainer disappeared. Both show high concentration and low recent activity, but the risk profiles are fundamentally different.

Ossuary uses a **two-track scoring model**:

- **Non-mature projects**: Standard scoring (described in 4.1–4.2 below)
- **Mature projects**: Modified scoring that uses lifetime contributor history and suppresses the abandonment penalty, while adding takeover detection

#### Maturity Heuristics

A project is classified as **mature** when ALL of these are true:

| Criterion | Threshold | Rationale |
|-----------|-----------|-----------|
| Repository age | ≥ 5 years | Long-lived projects have demonstrated stability |
| Total commits | ≥ 30 | Meaningful development history |
| Last commit | Within 5 years | Not truly dead or deleted |

This classification catches stable infrastructure (argon2, dosfstools, logrotate, cronie, fillup) while correctly leaving young abandoned projects scored as risky.

#### Mature Project Scoring Differences

| Component | Non-Mature | Mature |
|-----------|-----------|--------|
| Base risk | Recent (12-month) concentration | **Lifetime** concentration when <4 commits/year; recent otherwise |
| Activity modifier | -30 to +20 | **+20** when zero recent commits (truly abandoned); otherwise clamped to ≤0 |
| Takeover detection | N/A | **+20** if proportion shift detected |

For mature projects with *some* activity (even 1–3 commits per year) the real risk isn't abandonment — it's unexpected takeover (the xz-utils pattern). A project that sat quietly for 15 years with occasional small edits is safe by default.

The lifetime concentration fallback only applies when the project has fewer than 4 commits per year — the "abandoned" activity tier where concentration from 1-3 commits is unreliable. When a mature project has 4+ recent commits, the recent concentration is used as normal, preserving the governance signal.

### 4.1 Base Risk (Concentration + Bus Factor)

Base risk uses two complementary signals — top-1 **concentration** and CHAOSS **bus factor** (minimum contributors for 50% of commits). The worse signal sets the floor.

**Concentration** (top-1 contributor share):

| Concentration | Risk | Interpretation |
|---------------|------|----------------|
| <30% | 20 | Distributed - healthy |
| 30-49% | 40 | Moderate concentration |
| 50-69% | 60 | Elevated concentration |
| 70-89% | 80 | High concentration |
| ≥90% | 100 | Critical - single maintainer |

**Bus factor** (CHAOSS-aligned):

| Bus Factor | Risk Floor | Interpretation |
|-----------|-----------|----------------|
| 1 | 60 | Single person for 50%+ (concentration already captures) |
| 2 | 40 | Two people control the project |
| 3–5 | 40 | Small group, moderate risk |
| 6+ | 20 | Well-distributed |

**Base Risk = max(concentration_risk, bus_factor_risk)**

This catches cases concentration misses. Example: trivy has 18% top-1 concentration (looks distributed) but bus factor 3 — only 3 people account for 50% of commits. Concentration gives base 20; bus factor raises it to 40.

**Calculation**: Concentration = (commits by top contributor / total commits) × 100, using a tapered window (full weight 0-10 months, linear fade 10-14 months) to smooth week-to-week boundary noise. Bus factor computed from unweighted recent commits, excluding bots (`[bot]` in email/name).

For **non-mature** projects, only recent commits are used. For **mature** projects with <4 commits/year, lifetime concentration is used as fallback.

### 4.2 Activity Modifier

Activity level indicates whether maintainers are engaged and responsive.

| Commits/Year | Modifier | Interpretation |
|--------------|----------|----------------|
| >50 | -30 | Actively maintained |
| 12-50 | -15 | Moderately active |
| 4-11 | 0 | Low activity |
| <4 | +20 | Appears abandoned |

**Rationale**: Abandoned packages are prime targets for takeover attacks (event-stream pattern).

**Mature project exception**: For mature projects the activity modifier follows a three-way split:

| Recent commits | Modifier | Interpretation |
|---|---|---|
| 0 | **+20** | Truly abandoned (no maintainer present at all) — the abandonment penalty applies even on mature projects |
| 1–3 | clamped to ≤0 | Stable but quiet; the negative side of the modifier still gives credit if activity rises |
| ≥4 | clamped to ≤0 | Actively maintained; standard mature-project handling |

A 15-year-old tool with 2 commits/year is stable, not abandoned, and gets the clamp. The same tool with *zero* commits in the last 12 months is treated as abandoned: even mature projects need someone home, otherwise their long history just means a larger attack surface for an opportunistic takeover.

### 4.3 Protective Factors

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
| **Project Maturity** | 0 (informational) | Mature project (see §4.0) | Benefit is activity-penalty suppression + lifetime concentration fallback, not a score bonus |

The VADER sentiment magnitude was a `±10` factor through v6.2.1; it
contributes 0 in v6.3 (the §5.10 ablation found 0/167 packages
crossed the ±0.3 threshold on the validation set). The field is
retained on `ProtectiveFactors` as structurally 0 for cached-score
deserialisation; see §6.3.

#### Risk Increasers (Positive Points)

| Factor | Points | Condition | Rationale |
|--------|--------|-----------|-----------|
| **Frustration Detected** | +15 | Rule-based maintainer-authored frustration text (§6.2) | colors/faker pattern; lowered from +20 in v6.3 (rayon flipped FP→TN, no TPs lost; see §6.4.1) |
| **Takeover Risk** | +20 | Proportion shift >30pp on mature project | xz-utils/Jia Tan pattern (see §4.4) |

### 4.4 Proportion Shift Takeover Detection

For mature projects, Ossuary detects the **xz-utils attack pattern**: a minor or unknown contributor gradually taking over a quiet project.

#### Methodology

For each contributor who made commits in the last 12 months, compute:

```
proportion_shift = recent_share% - historical_share%
```

Where:
- **recent_share%** = contributor's commits in last 12 months / total recent commits × 100
- **historical_share%** = contributor's commits before last 12 months / total historical commits × 100

If any contributor's proportion shift exceeds **+30 percentage points** on a mature project with ≥5 recent commits, a **takeover risk** flag is raised (+20 points).

#### Guards Against False Positives

Two filters prevent false alarms from established maintainers and automated tooling:

1. **Bot filtering**: Contributors with `[bot]` in their email or name are excluded (e.g., dependabot, renovate). Bots can dominate recent commits on quiet projects without representing a takeover risk.

2. **Historical share threshold**: Only contributors with **<10% of historical commits** (measured against the full pre-recent window) are considered as takeover suspects. Established maintainers (e.g., a project creator at 20% historical share) naturally fluctuate in activity — that's not a takeover signal. The 10% threshold catches Jia Tan (~7.6% historical share against the full xz-utils history at the late-2024 cutoff used by the code's regression check) while filtering out long-time contributors like project founders whose share temporarily increases. A name-merged historical share is also computed to handle contributors who use multiple email identities (e.g., domain changes).

#### Design Rationale

This approach detects proportional change, not absolute newcomer status. Jia Tan made a few small patches in 2022 — enough to be "established" — before dominating the project in 2023. A binary newcomer check would miss this pattern. Proportion shift catches it because, even at the modest historical share Jia Tan held at each cutoff (see Section 4.4 table), the recent share grew large enough to exceed the +30pp threshold regardless of when the contributor first appeared.

#### Validation Against xz-utils Timeline

| Cutoff Date | Jia Tan Historical % | Jia Tan Recent % | Shift | Detected? |
|-------------|---------------------|-------------------|-------|-----------|
| 2023-01 | 0.2% | 15% | +14.8pp | No (below 30pp) |
| 2023-03 | 0.6% | 31% | +30.4pp | **Yes** |
| 2023-06 | 1.2% | 42% | +40.8pp | **Yes** |
| 2024-01 | 3.5% | 50% | +46.5pp | **Yes** |

**Result**: Ossuary detects the xz-utils takeover pattern by March 2023, **12 months before** the backdoor was discovered (March 2024).

---

## 5. Maintainer Reputation System

Reputation provides a composite assessment of maintainer trustworthiness and investment in the ecosystem.

### 5.1 Reputation Signals

| Signal | Points | Threshold |
|--------|--------|-----------|
| **Account Tenure** | +15 | >5 years on GitHub |
| **Portfolio Quality** | +15 | ≥50 original repos with ≥10 stars each |
| **Total Stars** | +15 | ≥50,000 stars across repos |
| **Sponsor Support** | +15 | ≥10 GitHub sponsors |
| **Packages Published** | +10 | ≥20 packages maintained |
| **Top Package Maintainer** | +15 | Maintains a flagship package on the relevant registry (curated top-~30 list per ecosystem; covers npm, PyPI, Cargo, RubyGems, Packagist, NuGet, Go, GitHub) |
| **Recognized Org** | +15 | Member of nodejs, python, apache, etc. |

### 5.2 Reputation Tiers

| Tier | Score Range | Risk Reduction |
|------|-------------|----------------|
| TIER_1 | ≥60 | -25 points |
| TIER_2 | 30-59 | -10 points |
| UNKNOWN | <30 | 0 points |

#### Top-package list curation

The flagship-package bonus uses a curated list per ecosystem
(`TOP_PACKAGES` in `src/ossuary/scoring/reputation.py`). Lists were
compiled from each registry's own download/installation count metric,
snapshot date 2026-04-17, capped at roughly 30 entries per ecosystem.
The intent is not to be exhaustive but to cover packages whose
presence in a maintainer's portfolio clearly signals ecosystem-wide
reach. The list is open to community refinement (see
[`CONTRIBUTING.md`](../CONTRIBUTING.md)); refinements are treated as
supportive contributions, not core methodology changes.

### 5.3 Recognized Organizations

Membership in these organizations confers institutional backing:

- **JavaScript/Node**: nodejs, openjs-foundation, npm, expressjs, eslint, webpack, babel
- **Python**: python, psf, pypa, pallets, django, tiangolo
- **General**: apache, cncf, linux-foundation, mozilla, rust-lang, golang
- **Cloud/Infra**: kubernetes, docker, hashicorp

---

## 6. Sentiment Analysis

### 6.1 Approach

Ossuary analyses commit messages and issue discussions through two
deterministic layers:

1. **General sentiment** — VADER compound score across every text
   (commits, issue bodies, comments). Through v6.2.1 this fed a
   ±10 protective factor; in v6.3 the scoring branch was removed
   (the §5.10 ablation found 0/167 packages crossed the ±0.3
   threshold on the validation set). The signal is still surfaced
   on `RiskBreakdown.protective_factors.sentiment_evidence` as a
   community-mood readout, but it does not change the score.
2. **Rule-based frustration detection** — a curated set of regex
   templates plus literal phrases targeting burnout / sabotage
   precursor language. Frustration evidence drives the +15 risk
   factor in §4 Factor 10 (lowered from +20 in v6.3; see §6.4.1).

VADER alone is not sufficient: it scored Marak Squires' Nov 2020
sabotage rant at +0.676 (positive!) because of words like "support"
and "opportunity". The rule layer is what carries the detectable
frustration signal in v6.3.

#### 6.1.1 Author attribution (v6.2)

Frustration is a signal about the *maintainer*, not random users
complaining about the project. Through v6.1, every issue text was
weighted equally, which made noisy issue trackers a major source of
spurious +20 frustration hits.

v6.2 restricts frustration scoring to text authored by the
maintainer login (`top_contributor_email` / `maintainer_username`
already on `CollectedData`). Bot accounts (`*[bot]`) are always
excluded. The general VADER pass continues to scan everything so the
community-mood signal is not lost. When the maintainer login cannot
be determined, the v6.1 behaviour (scan all texts) is preserved as a
conservative fallback — the analyzer doesn't silently drop the
signal.

Commits already imply maintainer authorship and so are not filtered.

### 6.2 Frustration rules (v6.2)

Through v6.1 frustration detection used a flat list of literal
keywords. It caught Marak's exact phrasing ("free work", "no longer
support") but missed paraphrases — "I'm done giving away my labor",
"tired of supporting Fortune 500s", "stop maintaining this" all
slipped through.

v6.2 replaces the flat list with a small set of regex *templates*
that capture verb stems and structural patterns, plus literal
fallbacks for multi-word phrases that don't generalise cleanly. Each
rule has a short label that surfaces as evidence in the breakdown.
The full set lives in `src/ossuary/sentiment/analyzer.py`
(`FRUSTRATION_RULES`); representative templates:

| Template | Catches |
|---|---|
| `tired of \w+(ing\|s)?` | "tired of fixing", "tired of bugs" |
| `no longer (going to )?(support\|maintain\|fix\|...)` | Marak's exact phrasing + variants |
| `stop(ping)? (support\|maintain\|fix\|...)` | "stop maintaining this" |
| `done (with\|maintaining\|...) (this\|the\|my)` | "done with this project" |
| `giv(e\|ing) up on (this\|the\|maintaining\|...)` | "giving up on this" |
| `(my )?free (work\|labor\|time)` | Marak's "free work" + paraphrases |
| `unpaid \w+` | "unpaid work", "unpaid labor", "unpaid hours" |
| `pay (me\|us\|developers\|maintainers)` | financial-protest pattern |
| `(corporate\|company\|companies\|fortune 500\|...) (exploit\|profit\|...)` | "company makes millions off my code" |

Literal fallbacks cover phrases like `burned out`, `stepping down`,
`mass resignation`, `protest`, `boycott`, `on strike`, `taken
advantage of`, `resentment`. All matching is case-insensitive and
deterministic — same input always produces the same labels in the
same order.

### 6.3 Sentiment Scoring (removed from formula in v6.3)

Through v6.2.1 the VADER compound score contributed protective-factor
points (+10 risk for compound < −0.3, −5 risk for compound > +0.3).
The §5.10.1 ablation found that 0 of 167 packages in the validation
set crossed the ±0.3 threshold, so the factor never participated in
a final score. v6.3 removes the scoring branch and documents
`sentiment_score` as structurally 0 on `ProtectiveFactors`. The
underlying VADER computation is still performed and its average is
exposed as evidence; only the score weight was removed.

The deferred layer-3 embedding work (§6.6) is what would make a
sentiment factor earn its place back. Until then, the rule-based
frustration layer (§6.2) carries the detectable emotional signal.

### 6.4 Validation: corpus-driven coverage

Rule changes are driven by a committed corpus at
`tests/fixtures/sentiment_corpus.jsonl` — currently 45 positive
examples (Marak Squires variants, event-stream-style handover
language, node-ipc-style protest, generic OSS-burnout discourse,
funding frustration, sabotage precursors) and 42 negatives
chosen to surface plausible false-positive traps:

- `dev_corpus` — ordinary dev-text patterns (`"Don't give up on
  the test suite"`, `"Boycott of legacy browser support"`,
  `"Pay-per-use API tier added"`).
- `lifecycle_corpus` — orderly handover and deprecation language
  (`"looking for a new maintainer for this package"`,
  `"this project is no longer actively maintained"`,
  `"this is the last release supporting Python 3.8"`).
- `process_corpus` — routine release / sprint communication
  (`"out of bandwidth for this milestone"`,
  `"please stop opening PRs against the release branch"`,
  `"we support Fortune 500 companies"`).

Each entry is tagged with a `source` bucket so a thesis defender
can trace why it is in the set. The lifecycle and process buckets
were added in the v6.2.1 fix-up after a GPT review (2026-04-19)
flagged that several first-pass rules had been overshooting onto
healthy governance text — see §6.4.1 for the principle.

The corpus drives three guard tests in `tests/test_sentiment.py`:
positive recall ≥ 95% (currently 100%), false-positive rate ≤ 5%
(currently 0%), and per-bucket recall ≥ 80% (so a future rule
change cannot silently gut one whole category while staying above
the global bar).

#### 6.4.1 Precision-over-recall on lifecycle text

The frustration factor (+15 in v6.3, lowered from +20 in v6.2.1
after the §5.10.1 ablation showed the +20 floor was leaking one
residual FP without earning recall) is one of the heaviest single
contributions in the protective-factors aggregation. A false-positive
on healthy lifecycle text (orderly maintainer succession, planned
deprecation, EOL announcements) materially overstates risk for a
project that is doing exactly what good OSS governance looks like.

Principle: **emotional / personal exit signals fire frustration;
clinical lifecycle / governance announcements do not.** Examples:

| Phrase | Fires? | Why |
|---|---|---|
| "I'm walking away from this project." | yes | first-person, abandonment connotation |
| "I quit." | yes | direct exit signal |
| "I am no longer going to support … with my free work." | yes | refusal + economic frustration |
| "looking for a new maintainer for this package" | no | passive, third-person, collaborative |
| "this is the last release supporting Python 3.8" | no | EOL announcement, scoped to a runtime |
| "transfer ownership to the new team" | no | orderly handover |

Genuinely-frustrated handover ("I quit, find another maintainer")
is still caught — through the emotional rules (`im_exhausted`,
`i_quit`, `cant_keep_doing`), not the lifecycle ones. A small
amount of recall is intentionally traded for precision on this
boundary.

### 6.5 Known signal gap: external surfaces

The analyzer can only see what GitHub returns through its issues /
PRs / commits APIs. Maintainer-frustration text frequently lives on
external surfaces the collector cannot reach:

- Personal blog posts and Substack newsletters.
- GitHub Gists (Marak Squires' Nov 2020 "No more free works" rant
  was published as a gist, not in any of his repo issue trackers).
- Twitter / X / Mastodon threads.
- Hacker News and Lobsters comments.
- README files outside the default branch (e.g. on a sabotage
  release branch where the only warning is in the new README).

This is observable in the `colors.js` real-data probe (April 2026):
without author attribution the analyzer found three frustration
hits in the issue tracker, all written by *users* about Marak's
behaviour after the sabotage. With v6.2 author attribution applied,
zero hits were attributed to the maintainer, because his actual
rant was never posted in the repo. The community-mood signal is
still computed (the same user comments scored -0.8 to -0.89 through
VADER) — but as of v6.3 it no longer contributes to the score (see
§6.3); the maintainer-side frustration signal is genuinely absent.

This gap is not solved by the deferred embedding layer (§6.6)
either: any classifier is bounded by the text it can see. The
honest mitigation is to widen collection (gist polling, blog
discovery via the maintainer's GitHub profile URL) rather than
making the classifier smarter — which is logged as future work, not
a v6.2 commitment.

### 6.6 Forward look — semantic embeddings (deferred)

A third layer using pinned sentence-embedding similarity against a
curated frustration corpus is designed but not implemented (see
`thesis/sentiment_v2_design.md`). It would ship behind an
opt-in environment flag, with the asymmetric guarantee that the
embedding layer can only *add* frustration evidence — the
deterministic v6.2 rule layers remain the floor regardless of
embedding-model availability. As noted in §6.5, embeddings do not
address the external-surfaces gap; they would only catch
paraphrases the rule layer missed *within* the text already
collected.

---

## 7. Risk Levels

| Score | Level | Semaphore | Recommended Action |
|-------|-------|-----------|-------------------|
| 0-19 | VERY_LOW | Green | Routine monitoring |
| 20-39 | LOW | Green | Quarterly review |
| 40-59 | MODERATE | Yellow | Monthly review |
| 60-79 | HIGH | Orange | Weekly review, contingency plan |
| 80-100 | CRITICAL | Red | Immediate action required |

---

## 8. Validation Methodology

### 8.1 Dataset Construction

The validation dataset (v6.3, n=170) includes:

1. **Known Incidents** (38 packages): Packages with documented supply chain incidents, spanning governance failures, protestware, account compromises, CI/CD exploits, and maintainer sabotage. Includes both in-scope and explicitly out-of-scope incidents.
2. **Governance Risk** (12 packages): Packages with elevated governance risk signals but no incident (yet) — abandoned, single-maintainer, or concentrated projects.
3. **Control Group** (120 packages): Popular packages with healthy governance across all 8 ecosystems.

Total: 170 packages across all 8 supported ecosystems. The v6.3 dataset extension (April 2026) added the TeamPCP campaign — `xinference`, `litellm`, `axios` as T4 EXPECTED FN; `eslint-config-prettier`, `aquasecurity/trivy-action` as additional T4/T5 cases; `telnyx` as a T3 near-miss FN at score 55 — to validate detection boundaries against contemporary credential-theft and CI-tag-manipulation attacks.

**Dataset construction principles**:
- Incidents drawn from documented supply chain attacks 2016–2026, cross-referenced against multiple sources (Socket.dev, Snyk, CISA advisories, incident write-ups)
- Controls selected as top packages per ecosystem by download count
- Each incident classified by detectability tier (T1-T5) based on whether governance weakness was observable before the attack
- Out-of-scope incidents explicitly included to validate detection boundaries
- Each case includes attack type, incident date, cutoff date (for T-1 analysis), and rationale

### 8.2 Scoped Validation Framework

Traditional recall penalizes the model for not detecting attacks it was never designed to detect (e.g., credential theft on healthy projects). The scoped framework addresses this:

**Tier classification**: Each incident is assigned to a detectability tier based on whether governance weakness was observable before the attack. In-scope tiers (T1, T2, T3, T_risk) contribute to recall; out-of-scope tiers (T4, T5) do not.

| Tier | Label | In-scope? | Count |
|------|-------|-----------|-------|
| T1 | Governance decay → compromise | Yes | 7 |
| T2 | Protestware / sabotage | Yes | 6 |
| T3 | Account compromise + weak governance | Yes | 7 |
| T_risk | Governance risk, no incident | Yes | 12 |
| T4 | Account compromise + strong governance | No | 11 |
| T5 | CI/CD pipeline exploit | No | 7 |

**Decision procedure**: For borderline cases, two questions determine scope: (1) Would Ossuary's signals have shown elevated risk before the attack? (2) Was governance weakness the enabling condition or merely coincidental? If both yes → in-scope.

### 8.3 Classification Rules

| Expected | Predicted Score | Classification |
|----------|-----------------|----------------|
| Incident/Risk (in-scope) | ≥60 | True Positive (TP) |
| Incident/Risk (in-scope) | <60 | False Negative (FN) |
| Safe | <60 | True Negative (TN) |
| Safe | ≥60 | False Positive (FP) |

Out-of-scope incidents (T4, T5) are tracked separately as "bonus detections" but do not count as TP or FN.

### 8.4 Results (n=170, Scope B)

```
In-scope incidents: 32 (T1=7, T2=6, T3=7, T_risk=12)
Out-of-scope incidents: 18 (T4=11, T5=7)
Controls: 120

Confusion Matrix (Scope B):
  TP: 24  |  FN: 8
  FP: 1   |  TN: 119

Accuracy:   94.1%
Precision:  96.0%
Recall:     75.0%
F1 Score:   0.842
```

**Key results**:

- **1 false positive** (rxjs) across 120 safe packages. rxjs scores 75 HIGH due to 100% maintainer concentration and 0 commits in the last year. The governance signals are genuinely concerning; it may warrant reclassification as `governance_risk`.
- **8 in-scope false negatives**, all explainable: faker (community fork), node-ipc (active development masks risk), polyfill.io (ownership transfer untracked), core-js (high activity offsets bus-factor risk), devise (borderline drift), es5-ext and is-promise (maintainer reputation correctly reduces score), telnyx (T3 near-miss at score 55, five points below the 60-point threshold — see §5.7.1).
- **75.0% in-scope recall** reflects genuine detection capability with honest historical scoring. Recall moved from 77.4 % at n=167 (v6.2.1) to 75.0 % at n=170 (v6.3) through dataset composition alone — one new in-scope incident (telnyx) added without an offsetting TP — not a model regression.

**Comparison with unscoped metrics**: Across all 50 incidents (including out-of-scope), overall recall is 50.0%. This lower number is expected — 18 out-of-scope incidents (T4 well-governed credential theft, T5 CI/CD exploits) are fundamentally undetectable from governance signals.

**Tuning history**: v4.0 initially used a -15 maturity bonus + lifetime concentration for all mature projects, achieving 91.6% accuracy on cached scores but only 81.8% on fresh validation. Parameter sweep across 16 configurations (bonus ∈ {0,-5,-10,-15} × lifetime threshold ∈ {1,4,8,12}) identified the optimal: bonus=0, lifetime fallback when <4 commits/year.

### 8.5 Per-Tier Detection Rates

| Tier | Detected | Rate | Notes |
|------|----------|------|-------|
| **T1: Governance decay** | 6/7 | **86%** | 1 miss: polyfill.io (ownership transfer) |
| **T2: Protestware / sabotage** | 2/6 | **33%** | 4 misses: reputation-protected maintainers |
| **T3: Weak-gov compromise** | 6/7 | **86%** | 1 miss: telnyx (org backing softens score below threshold) |
| **T_risk: Governance risk** | 10/12 | **83%** | 2 misses: core-js (very active), devise (borderline) |
| T4: Strong-gov compromise (OOS) | 1/11 | 9% | Expected — out of scope |
| T5: CI/CD exploits (OOS) | 0/7 | 0% | Expected — out of scope |

T1 (governance decay, 86%) and T3 (weak-governance compromise, 86%) are the primary targets. T2 (protestware, 33%) is weakest because protestware maintainers tend to have strong reputations that correctly reduce their risk scores. This is a genuine trade-off: reputation DOES reduce attack probability, but doesn't prevent unilateral action.

### 8.6 In-Scope False Negative Analysis

8 in-scope false negatives, all explainable:

| Package | Score | Tier | Why Missed |
|---------|-------|------|-----------|
| faker | 0 | T2 | Evaluating community fork (faker-js/faker); original repo deleted |
| node-ipc | 50 | T2 | Active development masks bus-factor-1 risk |
| polyfill.io | 40 | T1 | Ownership transfer to malicious CDN is an untracked signal |
| devise | 40 | T_risk | Borderline; concentration drift from minor changes |
| core-js | 40 | T_risk | High activity gives discount despite 92% concentration |
| es5-ext | 40 | T2 | 100% concentration but maintainer (medikoo) has strong reputation |
| is-promise | 35 | T2 | Reputation correctly reconstructed at 2020 cutoff |
| telnyx | 55 | T3 | T3 near-miss at score 55, five points below the 60-point threshold; org backing (-15) softens an otherwise risky bus-factor-1 / 97 % concentration profile |

Historical reputation reconstruction (v3.2) verifies portfolio and tenure at the cutoff date using repo `created_at` timestamps. This gives honest T-1 scores: is-promise (45) reflects ForbesLindesay's real 2020 reputation rather than stripping it to zero. The cost is 1 fewer TP compared to the stripped version, but the score is more accurate.

Historical scoring is intentionally conservative for unstable GitHub-only
signals. Present-day repository stars are **not** reused as a proxy for past
visibility, and issue/comment sentiment is disabled for T-1 scoring because
the GitHub issue API snapshot is current-state and incomplete. Historical
scores therefore prefer reconstructable signals over current-state proxies.

### 8.7 Out-of-Scope Incident Analysis

18 out-of-scope incidents are included to validate detection boundaries:

**T4: Account compromise on healthy projects (11 cases)** — ua-parser-js (bonus detection at 90), eslint-scope (35), LottieFiles (45), chalk (35), cline (0), solana-web3.js (0), eslint-config-prettier (55), num2words (0), axios (0), litellm (0), xinference (0).

**T5: CI/CD pipeline exploits (7 cases)** — reviewdog (0), codecov (0), rspack (0), ultralytics (0), tj-actions (50), nrwl/nx (0), aquasecurity/trivy-action (45).

All correctly score below threshold except ua-parser-js (bonus detection at 90). A tool that flagged all credential-based attacks would need to flag every package, producing unacceptable false positive rates.

### 8.8 The Scoped Validation Contribution

A key methodological contribution is the **tier-based scoped evaluation**:

1. **Define detectability tiers** a priori based on whether governance signals were observable
2. **Include out-of-scope incidents** in the dataset to validate boundaries
3. **Report both scoped and unscoped metrics** for transparency
4. **Document per-tier detection rates** to identify specific strengths and weaknesses

This is more honest than either (a) reporting only in-scope incidents (hides the limitations) or (b) counting all incidents equally (artificially depresses recall for a tool with a clear, stated scope).

### 8.7 T-1 Validation (Predictive Power)

To validate **predictive** capability, we scored packages at a cutoff date *before* their incidents occurred:

| Package | Incident Date | Cutoff Date | T-1 Score | Level | Key Signals Detected |
|---------|---------------|-------------|-----------|-------|---------------------|
| event-stream | 2018-09-16 | 2018-09-01 | 100 | CRITICAL | 75% concentration, "free work" frustration |
| colors | 2022-01-08 | 2022-01-01 | 100 | CRITICAL | 100% concentration, "protest", "exploitation" |
| coa | 2021-11-04 | 2021-11-01 | 100 | CRITICAL | 100% concentration, abandoned |

**Control comparison** (same timeframe):

| Package | Cutoff Date | Score | Level | Key Signals |
|---------|-------------|-------|-------|-------------|
| express | 2022-01-01 | 0 | VERY_LOW | Org-backed (30 admins), tier-1 maintainer, 64M downloads/wk |

**Result**: all three governance-decay worked examples scored CRITICAL at T-1, and the xz-utils takeover pattern scored HIGH (see the §4.4 timeline). This is an illustrative worked-example set, not a recall claim — the headline recall is the §8.4 Scope B figure (24/32 = 75.0 %); these T-1 cases are a subset of the in-scope incidents that already contribute to that recall, presented here at their cutoff dates to show the pre-incident signal in detail.

#### T-1 Analysis Details

The T-1 score breakdowns below were captured at v6.2.1 (frustration
weight +20). Under the active v6.3 weighting (frustration +15),
event-stream and colors retain CRITICAL; coa is unchanged because its
score floor comes from concentration and activity, not frustration.
The figures are reproduced verbatim so historical claims about the
detection signal at the time of the incident remain auditable; for
current scores, run `ossuary score <pkg> --cutoff <date>`.

**event-stream (before September 2018 compromise)**:
```
Score: 100 CRITICAL
- Base Risk: 75% concentration (+80)
- Activity: 4 commits/year (+0)
- Frustration: "free work" keyword detected (+20 at v6.2.1; +15 in v6.3)
```
The tool would have flagged this as a prime takeover target with frustration signals.

**colors (before January 2022 sabotage)**:
```
Score: 100 CRITICAL
- Base Risk: 100% concentration (+100)
- Activity: 0 commits/year (+20)
- Frustration: "protest", "exploitation" keywords detected (+20 at v6.2.1; +15 in v6.3)
- Protective: GitHub Sponsors (-15), downloads (-10)
```
Despite protective factors from visibility and sponsors, the frustration signals and extreme concentration produced a CRITICAL score (the v6.3 frustration weight change does not move colors out of CRITICAL — the score is clamped at 100 with substantial headroom).

**coa (before November 2021 compromise)**:
```
Score: 100 CRITICAL
- Base Risk: 100% concentration (+100)
- Activity: 0 commits/year (+20)
```
Classic abandonment pattern - single maintainer, no activity, prime target for malicious takeover.

This demonstrates that the methodology could have flagged these packages **before** their incidents occurred, validating the predictive value of governance metrics.

#### xz-utils Proportion Shift Detection (v4.0)

The xz-utils attack (CVE-2024-3094) was the most sophisticated governance attack documented, with a 2.6-year timeline. Using proportion shift takeover detection, Ossuary detects the anomaly by **March 2023**:

```
Cutoff: 2023-03 (12 months before disclosure)
Score: HIGH — Takeover risk detected
- Mature project (22 years, 80+ lifetime contributors)
- Jia Tan: +30.4pp shift in commit share
- Evidence: "Jia Tan: +30pp shift in commit share on mature project (xz-utils pattern)"
```

This validates that governance-based scoring can detect social engineering attacks during the grooming phase, before any malicious code is introduced.

---

## 9. Limitations

### 9.1 Methodological Limitations

1. **GitHub-centric**: Relies on GitHub metadata; other forges have limited support
2. **Historical data**: Git history can be rewritten; metrics reflect current state
3. **English bias**: Sentiment analysis optimized for English text
4. **API rate limits**: Full analysis requires authenticated GitHub API access

### 9.2 Detection Limitations

1. **Cannot detect insider threats** from trusted maintainers with good signals
2. **Cannot detect account compromise** on active, well-governed projects
3. **Cannot detect typosquatting** (new packages have no governance history)
4. ~~**May flag healthy "done" packages** as risks~~ — Resolved in v4.0 by mature project detection (see §4.0)

### 9.3 Temporal Limitations

1. **Reputation data is current-state**: Stars, sponsors, repos reflect present, not historical
2. **Organization membership is current**: Historical org membership not tracked
3. **Download counts are current**: Cannot assess historical visibility

### 9.4 Score Stability

#### The problem (pre-v3.1)

Comparing two validation runs one week apart on the same 157 packages:
- **95% stable** (149/157 scored identically)
- **8 packages changed** by ±10-20 points
- sidekiq crossed the 60-point threshold (40→60), creating a phantom false positive with no actual governance change

Root cause: the hard 12-month cutoff for concentration. A single commit crossing the 365-day boundary shifts the top contributor's commit percentage by 2-3%, which the scoring formula amplifies to ±20 point score changes. For example, sidekiq's concentration moved from 68.9%→71.2% solely because one commit aged past day 365.

#### The fix (v3.1): tapered concentration

Activity count keeps the hard 12-month cutoff (the activity modifier uses coarse buckets — >50, ≥12, ≥4, <4 commits — that are insensitive to ±3 commit boundary noise). Concentration uses a tapered window:

- 0-10 months: weight 1.0 (fully recent)
- 10-14 months: weight fades linearly from 1.0 → 0.0
- 14+ months: weight 0.0 (excluded)

A commit at month 11 gradually fades rather than vanishing when it ages past the boundary. This eliminates the cliff edge that caused phantom threshold crossings.

#### Result

Compared to the hard-cutoff run on the same data:
- sidekiq: 60→40 (false positive eliminated)
- No true positives lost
- Precision: 92.3%→96.0%
- Only 6 scores changed, all by ≤20 points, all classification changes were improvements

The taper is not artificial smoothing — a commit from 11 months ago genuinely shouldn't have the same weight as one from last month, and shouldn't vanish entirely at 12 months and 1 day. The hard cutoff was the artifact.

---

## 10. Threats to Validity

This section discusses potential threats to the validity of the research findings, following standard academic conventions for empirical software engineering research.

### 10.1 Internal Validity

Internal validity concerns whether the methodology correctly measures what it claims to measure.

| Threat | Description | Mitigation |
|--------|-------------|------------|
| **Threshold Selection** | Risk thresholds (60+ = risky) were chosen based on incident analysis, not derived empirically | Validated against 170 packages across 8 ecosystems; threshold sensitivity tested at 50, 55, 60, 65 — ≥60 is optimal (96.0% precision, 75.0% in-scope recall) |
| **Keyword Selection Bias** | Frustration keywords derived from known incidents may overfit to historical cases | Keywords based on general burnout/economic frustration patterns, not incident-specific |
| **Scoring Formula Weights** | Point values for factors are hand-tuned, not learned from data | Weights validated through iterative testing; future work could use ML optimization |
| **Maturity Classification** | 5-year/30-commit threshold is heuristic, not empirically derived | Validated against 94 SLE packages; eliminates false CRITICALs on known-stable infrastructure |
| **Confounding Variables** | High scores might correlate with other unmeasured factors (e.g., project age, domain) | Controlled for by including diverse package types in validation set |

### 10.2 External Validity

External validity concerns whether findings generalize beyond the study context.

| Threat | Description | Mitigation |
|--------|-------------|------------|
| **Ecosystem Bias** | Initial validation limited to npm and PyPI | v2+ validation covers 8 ecosystems (npm, PyPI, Cargo, RubyGems, Packagist, NuGet, Go, GitHub) with consistent results |
| **Survivorship Bias** | Can only analyze repositories that still exist; deleted repos (like Marak/Faker.js) are invisible | Acknowledged as limitation; 2 incidents excluded because repos deleted (phpass, electron-native-notify) |
| **Selection Bias in Incidents** | Known incidents may be biased toward governance-detectable cases | Deliberately included 18 out-of-scope incidents (T4: account compromise, T5: CI/CD) to validate detection boundaries |
| **Temporal Generalization** | Validated on 2016-2026 incidents; attack patterns may evolve | T-1 validation confirms historical effectiveness; 2025 incidents (chalk, tj-actions, Nx) confirm boundary holds for recent attacks |
| **Cultural/Language Bias** | English-language sentiment analysis; non-English projects may score differently | Acknowledged limitation; VADER optimized for English social media text |

### 10.3 Construct Validity

Construct validity concerns whether the theoretical constructs are correctly operationalized.

| Threat | Description | Mitigation |
|--------|-------------|------------|
| **"Governance Risk" Definition** | Governance risk is a latent construct; operationalization may not capture all dimensions | Definition grounded in incident analysis; validated by predictive accuracy |
| **Maintainer Concentration Proxy** | Commit count used as proxy for "control"; doesn't capture npm publish rights, code review authority | Git commits are observable and historically correlate with incidents |
| **Frustration Measurement** | Keyword matching is crude; may miss subtle frustration or produce false positives | Combined with VADER sentiment; keywords chosen for high precision |
| **Reputation Conflation** | GitHub stars/repos conflate popularity with trustworthiness | Reputation is one factor among many; not solely determinative |

### 10.4 Conclusion Validity

Conclusion validity concerns whether the statistical conclusions are justified.

| Threat | Description | Mitigation |
|--------|-------------|------------|
| **Small Incident Sample** | 50 incident/risk packages in validation set (32 in-scope) | This is a near-census, not a sample. Cross-referencing CNCF (89), IQT Labs (182), and Ladisa et al. (94) catalogs identified ~50 total scorable governance-relevant incidents across our 8 ecosystems; we include the contemporary cohort (TeamPCP campaign added in v6.3 — see §8.1). The population IS small — governance-detectable attacks are rare events. |
| **Class Imbalance** | 50 incidents vs 120 controls (1:2.4 ratio) | Reported precision and recall separately; F1 accounts for imbalance; metrics reported both scoped (Scope B) and unscoped |
| **No Cross-Validation** | Single train/test split, not k-fold | Dataset is the near-complete population, not a sample from a larger one; temporal holdout analysis performed (≤2022 dev / 2023+ holdout) but holdout has only 3 in-scope incidents |
| **Confidence Intervals** | Point estimates reported without confidence intervals | Bootstrap CIs reported: precision 86–100%, recall 62–91%, F1 74–94%. Wide recall CI reflects genuine uncertainty from small population |
| **ML Comparison** | Hand-tuned formula not validated against learned alternatives | Five ML models tested (LR, SVM, RF, Gradient Boosting, XGBoost) on the v6.2.1 baseline (n=167, F1 0.857). Best ML achieves F1 0.787 vs hand-tuned 0.857. ML validates feature selection and threshold (PR-optimal = 60) but cannot match precision (80% vs 96%) due to small n and nonlinear interactions. The v6.3 dataset extension (TeamPCP campaign) shifted in-scope F1 to 0.842 through composition; the hand-tuned vs ML gap is unchanged in direction. |

### 10.5 Mitigations Summary

Despite these threats, several factors support the validity of findings:

1. **96.0% Precision**: 1 false positive (rxjs) across 170 packages and 8 ecosystems
2. **75.0% In-Scope Recall**: Scoped framework with honest historical reputation reconstruction
3. **Per-Tier Transparency**: T1 86%, T2 33%, T3 86%, T_risk 83% — specific strengths and weaknesses documented
4. **Near-Census Coverage**: Dataset covers 50 incidents and 120 controls across 8 ecosystems, including the 2025-2026 TeamPCP campaign for contemporary boundary validation
5. **CHAOSS Bus Factor**: Contributor diversity metric catches patterns missed by top-1 concentration (e.g. trivy: 18% top-1 but bus factor 3)
4. **T-1 Detection on the worked examples**: event-stream, colors, coa scored CRITICAL and xz-utils scored HIGH at their pre-incident cutoffs (see §8.7); the small worked-example set is illustrative, not a separate recall claim
5. **Explicit Boundary Validation**: 18 out-of-scope incidents included and documented
6. **Cross-Ecosystem Generalization**: Consistent results across npm, PyPI, Cargo, RubyGems, Packagist, NuGet, Go, and GitHub
7. **Temporal Range**: Incidents spanning 2016–2026, including the 2025 npm phishing wave and CI/CD exploit wave
8. **Score Stability**: Tapered concentration window eliminates phantom threshold crossings from boundary noise
9. **Grounded in Real Incidents**: Methodology derived from analysis of actual supply chain attacks
10. **Transparent Limitations**: Explicitly documents what the tool cannot detect and proves this empirically

---

## 11. Recommendations for Use

### 11.1 Integration Patterns

1. **CI/CD Pipeline**: Score dependencies on PR, fail on CRITICAL
2. **Scheduled Audits**: Weekly scans of dependency tree
3. **Acquisition Diligence**: Score target's OSS dependencies
4. **Vendor Assessment**: Evaluate third-party software stacks

### 11.2 Score Interpretation

| Score Range | Action |
|-------------|--------|
| 0-39 | Standard dependency management |
| 40-59 | Add to watchlist, review quarterly |
| 60-79 | Investigate alternatives, prepare fork |
| 80-100 | **Immediate review**, consider removal |

### 11.3 Combining with Other Tools

Ossuary complements but does not replace:

| Tool Type | Purpose | Ossuary Relationship |
|-----------|---------|---------------------|
| **SBOM tools** | Inventory what you have | Provides risk context |
| **Vulnerability scanners** | Known CVEs | Different risk dimension |

### 11.4 Empirical Comparison with OpenSSF Scorecard

OpenSSF Scorecard is the most widely-used automated security assessment tool for open source projects. To test the hypothesis that governance scoring and security-practice scoring measure orthogonal dimensions, we ran Scorecard against the same 158 packages in our validation set and compared results systematically.

#### Methodology

| Aspect | OpenSSF Scorecard | Ossuary |
|--------|-------------------|---------|
| **Focus** | Security best practices | Governance risk |
| **Checks** | CI/CD, branch protection, fuzzing, SAST, signed releases | Concentration, activity, frustration signals, reputation |
| **Question answered** | "Does this project follow security hygiene?" | "Could this project be abandoned or compromised?" |
| **Scale** | 0–10 (higher = better practices) | 0–100 (higher = more risk) |

We queried the Scorecard public API (`api.securityscorecards.dev`) for all 158 packages, resolving each to its GitHub repository via ecosystem-specific registry APIs (npm, PyPI, RubyGems, Cargo, Packagist, NuGet, Go). **132 of 158 packages** returned valid Scorecard data (26 had no indexed data, typically because the repository was deleted or not yet scanned).

#### Correlation Analysis

| Metric | Value |
|--------|-------|
| Packages with both scores | 132 |
| **Pearson correlation** | **−0.678** |
| **Spearman rank correlation** | **−0.625** |

The moderate negative correlation is expected: high Ossuary scores (risky governance) tend to occur on packages with low Scorecard scores (poor security practices), and vice versa. However, the correlation is far from −1.0, confirming the tools measure substantially different dimensions. The unexplained variance (r² = 0.46) represents the orthogonal information each tool uniquely captures.

#### Quadrant Analysis

Using thresholds of Ossuary ≥ 60 (HIGH+ risk) and Scorecard ≥ 5.0/10:

```
                          Scorecard ≥ 5.0    Scorecard < 5.0
  Ossuary ≥ 60 (risky)         2                  19
  Ossuary < 60 (healthy)      84                  27
```

**Quadrant I — High Ossuary Risk + High Scorecard (2 packages)**

These are the most thesis-relevant cases: projects that follow good security practices but have governance vulnerabilities that only Ossuary detects.

| Package | Ossuary | Scorecard | Attack Type |
|---------|---------|-----------|-------------|
| **xz-utils** | 80 CRITICAL | 6.3 | governance_failure |
| **ua-parser-js** | 75 HIGH | 8.1 | account_compromise |

*xz-utils* is the canonical example: Jia Tan deliberately maintained excellent security practices (signed releases, code review, CI/CD) specifically to build trust before inserting the backdoor. Scorecard gave 6.3/10 — reasonable security hygiene. Ossuary scored 80 CRITICAL because it detected the underlying governance vulnerability: extreme contributor concentration in a single newcomer who had rapidly gained commit access.

*ua-parser-js* scored 8.1/10 on Scorecard — excellent security practices — yet was compromised via account takeover. Ossuary scored 75 HIGH due to single-maintainer concentration risk that made the account a high-value target.

**Quadrant II — High Ossuary Risk + Low Scorecard (19 packages)**

Both tools flag problems, though for different reasons. Examples:

| Package | Ossuary | Scorecard | Type |
|---------|---------|-----------|------|
| colors | 100 | 2.2 | maintainer_sabotage |
| event-stream | 90 | 2.1 | governance_failure |
| rest-client | 100 | 3.3 | account_compromise |
| moment | 85 | 2.8 | governance_risk |
| boltdb/bolt | 100 | 3.0 | governance_risk |

This is the "agreement" quadrant — both tools correctly identify these as problematic, though Scorecard flags poor practices while Ossuary flags governance risk.

**Quadrant III — Low Ossuary Risk + High Scorecard (84 packages)**

Both tools agree these are healthy. This is the largest quadrant, dominated by well-maintained popular packages:

| Package | Ossuary | Scorecard |
|---------|---------|-----------|
| express | 0 | 8.8 |
| requests | 0 | 8.6 |
| typescript | 0 | 8.1 |
| django | 0 | 6.8 |
| react | 0 | 6.5 |
| tokio | 0 | 7.2 |

**Quadrant IV — Low Ossuary Risk + Low Scorecard (27 packages)**

Poor security practices but acceptable governance — a dimension Ossuary doesn't penalize:

| Package | Ossuary | Scorecard |
|---------|---------|-----------|
| debug | 30 | 2.6 |
| inherits | 35 | 3.6 |
| nanoid | 5 | 3.8 |

Many small, stable npm packages fall here: single-purpose modules with no CI/CD or branch protection, but functional governance (responsive maintainer, no concentration risk).

#### Incident Detection Comparison

Of the 32 incident packages with valid Scorecard data:

| Detection Method | Flagged | Rate |
|------------------|---------|------|
| Ossuary ≥ 60 | 21/32 | 66% |
| Scorecard < 5.0 | 27/32 | 84% |
| **Either tool** | **30/32** | **94%** |

The two tools that missed detection overlap on only 2 packages — combining them catches 94% of incidents vs. 66% or 84% alone. This demonstrates their complementary value.

Critically, the 5 incidents where Scorecard ≥ 5.0 (indicating good security practices) were all detected by Ossuary:

| Package | Ossuary | Scorecard | Attack |
|---------|---------|-----------|--------|
| ua-parser-js | 75 HIGH | 8.1 | account_compromise |
| xz-utils | 80 CRITICAL | 6.3 | governance_failure |
| tj-actions/changed-files | 50 | 5.9 | account_compromise |
| codecov/codecov-action | 0 | 7.0 | account_compromise |
| nrwl/nx | 0 | 6.4 | account_compromise |

Of these 5, Ossuary flagged 2 (xz-utils and ua-parser-js) — precisely the governance-detectable ones. The remaining 3 (tj-actions, codecov, nx) are CI/CD or credential compromises that neither governance nor security-practice scoring can predict, confirming the detection boundary analysis from §8.6.

#### Key Findings

1. **Moderate negative correlation (r = −0.68)** confirms the tools measure related but distinct dimensions. They are neither redundant (r ≈ −1.0) nor fully independent (r ≈ 0).

2. **The "blind spot" quadrant** (High Ossuary + High Scorecard) contains 2 packages — both real incidents where good security practices masked governance vulnerability. This is precisely where Ossuary adds unique value.

3. **Combined detection** reaches 94% of incidents, substantially better than either tool alone.

4. **Different failure modes**: Scorecard misses well-practiced projects with governance risk (xz-utils, ua-parser-js). Ossuary misses credential/CI/CD compromises at well-governed projects (codecov, nx). The tools are complementary by design.

### 11.5 Comparison with CHAOSS Metrics (Augur/GrimoireLab)

The [CHAOSS project](https://chaoss.community/) (Community Health Analytics for Open Source Software) defines standardized metrics for open source health. Tools like [Augur](https://github.com/chaoss/augur) and [GrimoireLab](https://chaoss.github.io/grimoirelab/) implement these metrics.

#### Relevant CHAOSS Metrics

| CHAOSS Metric | Description | Ossuary Equivalent |
|---------------|-------------|-------------------|
| **Contributor Absence Factor** (Bus Factor) | Minimum contributors for 50% of commits | Maintainer Concentration |
| **Elephant Factor** | Minimum *organizations* for 50% of commits | Not measured (individual focus) |
| **Activity Dates and Times** | Commit frequency patterns | Activity Modifier |
| **Change Request Closure Ratio** | PR/issue responsiveness | Not directly measured |

#### Key Differences

| Aspect | CHAOSS/Augur/GrimoireLab | Ossuary |
|--------|--------------------------|---------|
| **Output** | Individual metrics and dashboards | Single risk score (0-100) |
| **Focus** | Community health broadly | Supply chain attack risk specifically |
| **Sentiment** | Not included | Frustration/burnout detection |
| **Actionability** | Requires interpretation | Direct risk level + recommendations |
| **Historical** | Strong time-series support | Cutoff-based T-1 analysis |

#### Complementary Relationship

CHAOSS metrics provide **deep diagnostic data** for understanding community dynamics. Ossuary provides **rapid risk triage** for supply chain security decisions.

Example workflow:
1. **Ossuary** flags package as HIGH risk (score 75)
2. **GrimoireLab** dashboard reveals *why*: declining contributor diversity, increasing response times
3. Security team makes informed decision with full context

#### Academic Foundation

Ossuary's concentration metric aligns with CHAOSS's [Contributor Absence Factor](https://chaoss.community/kb/metric-contributor-absence-factor/), providing academic grounding for the approach. The key innovation is combining this with:
- Sentiment analysis for frustration detection
- Reputation scoring for maintainer assessment
- Weighted scoring formula calibrated against known incidents

### 11.6 Real-World Dependency Scanning

To demonstrate practical applicability, we scanned the dependency trees of two major open source projects using `ossuary scan`.

#### Express.js (npm, 28 runtime dependencies)

Express is the most widely-used Node.js web framework. Scanning its `package.json` runtime dependencies:

| Risk Level | Count | Examples |
|------------|-------|---------|
| CRITICAL | 2 | escape-html (100% conc., 0 commits/yr), depd (100% conc., 0 commits/yr) |
| HIGH | 3 | once (100% conc., 1 commit/yr), merge-descriptors (100% conc., 0 commits/yr), cookie-signature (100% conc., 0 commits/yr) |
| MODERATE | 1 | etag (67% conc., 3 commits/yr) |
| LOW | 10 | debug, qs, statuses, proxy-addr, http-errors, ... |
| VERY_LOW | 12 | body-parser, accepts, router, mime-types, ... |

**18% of Express's runtime dependencies score HIGH or CRITICAL.** These are single-maintainer micro-packages with no recent activity — exactly the governance profile that preceded the event-stream compromise. A security team reviewing this output would prioritize `escape-html` and `depd` for governance review or vendoring.

#### Home Assistant (PyPI, 58 core dependencies)

Home Assistant is one of the largest Python open source projects, with over 2,000 integrations. Scanning its core `requirements.txt`:

| Risk Level | Count | Examples |
|------------|-------|---------|
| CRITICAL | 9 | standard-aifc, standard-telnetlib, atomicwrites-homeassistant, ifaddr, astral, cronsim, ... |
| HIGH | 6 | ciso8601, orjson, audioop-lts, psutil-home-assistant, ha-ffmpeg, ... |
| MODERATE | 3 | python-slugify, webrtc-models, lru-dict |
| LOW | 11 | PyYAML, aiohttp_cors, packaging, bcrypt, Jinja2, ... |
| VERY_LOW | 28 | aiohttp, requests, cryptography, Pillow, SQLAlchemy, ... |

**26% of Home Assistant's core dependencies score HIGH or CRITICAL.** Several CRITICAL packages are HA-specific forks (`atomicwrites-homeassistant`, `psutil-home-assistant`) or niche single-maintainer libraries (`ifaddr`, `astral`) — packages unlikely to appear in vulnerability scanners but carrying real governance risk.

#### Practical Implications

These scans reveal a pattern common across large projects: the core framework dependencies (aiohttp, requests, cryptography) are well-governed, but the **long tail of utility dependencies** carries significant governance risk. This aligns with the "Roads and Bridges" thesis (Eghbal, 2016) — critical infrastructure depends on small, undermaintained packages that receive no attention until they fail.

The `ossuary scan` command makes this risk visible in seconds, enabling:
1. **Dependency review** during security audits
2. **CI/CD integration** to flag new high-risk dependencies in pull requests
3. **Vendoring decisions** — CRITICAL-scored packages are candidates for vendoring or replacement
4. **Maintainer outreach** — identifying which upstream packages need community investment

---

## 12. CRA-Aligned Outputs

This section describes the v0.9 outputs designed to plug into a Cyber
Resilience Act (Regulation (EU) 2024/2847) compliance workflow. **None of
these outputs change the risk score** — they are derivations on top of it.

### 12.1 SBOM ingestion and enrichment

Article 13(24) and Annex I Part II point (1) make the software bill of
materials the canonical interchange format for component data. Ossuary
v0.9 accepts CycloneDX 1.4+ JSON and SPDX 2.3+ JSON via:

```bash
ossuary score-sbom product.cdx.json
ossuary score-sbom product.spdx.json --enrich enriched.cdx.json
```

Components are identified by Package URL (PURL) where present, mapped to
Ossuary ecosystems via the standard PURL types (`pkg:npm`, `pkg:pypi`,
`pkg:cargo`, `pkg:gem`, `pkg:composer`, `pkg:nuget`, `pkg:golang`,
`pkg:github`). Components without a parseable PURL can be scored if a
single ecosystem hint is supplied via `--ecosystem-default`.

The `--enrich` flag writes the SBOM back with governance scores attached:
- **CycloneDX**: as `components[].properties[]` entries under the
  `ossuary:governance:` name prefix.
- **SPDX 2.3**: as embedded `packages[].annotations[]` entries with
  `annotator: Tool: ossuary-<version>`. Both per-element embedding and
  document-root annotation arrays are valid in the SPDX 2.3 JSON schema;
  Ossuary uses the per-element form. The annotation comment is a JSON
  payload that also carries the package's SPDXID, so the link to the
  package survives if a downstream tool extracts annotations standalone.

  Conformance is enforced by tests: enriched output is validated against
  the official SPDX 2.3 JSON Schema (vendored at
  `tests/fixtures/spdx-schema-2.3.json` from the [`spdx/spdx-spec`
  `support/2.3` branch](https://github.com/spdx/spdx-spec/tree/support/2.3/schemas))
  on every commit. An optional interop test in
  `tests/test_sbom_spdx_interop.py` additionally round-trips enriched
  output through the `spdx-tools` Python library and confirms its
  `validate_full_spdx_document` reports zero issues; install with
  `pip install -e ".[dev,dev-spdx-interop]"` to run it locally.

The original SBOM structure is preserved; tools that do not understand the
ossuary additions still parse the file. Re-running enrichment on an
already-enriched SBOM replaces (not appends) the Ossuary entries, so the
operation is idempotent.

### 12.2 Implied maximum support period

CRA Article 13(8) requires the manufacturer's declared support period to
take into account "the support periods of integrated components that
provide core functions and are sourced from third parties." For OSS
dependencies there is no formally declared support period. Ossuary v0.9
derives a defensible upper bound from the governance score:

| Score range | Risk level | Implied horizon |
|---|---|---|
| 0–19 | VERY_LOW | ≥60 months — no constraint on a 5-year claim |
| 20–39 | LOW | 60 months (matches the CRA 5-year minimum) |
| 40–59 | MODERATE | 36 months — reassess before extending |
| 60–79 | HIGH | 18 months — only with compensating controls |
| 80–100 | CRITICAL | 6 months — consider replacing or forking |

The mapping is heuristic and clearly labelled as such in tool output. It
is not derived from incident data; a manufacturer may justify a different
horizon with compensating controls.

For an SBOM the product-level horizon is computed by:
1. Scoring every component.
2. Selecting the **critical subset**: top-N components by structural
   importance (the same fragility × irreplaceability × tree-impact formula
   used by `xkcd-tree --tower`'s "most structurally critical dependency"
   indicator) when the SBOM contains dependency relationships, otherwise
   top-N by raw governance score.
3. Taking the **minimum** horizon across that subset. The product cannot
   defensibly claim a longer support period than its weakest critical
   dependency.

The default critical-top-N is 5; override with `--critical-top-n`.

```bash
ossuary support-period lodash -e npm
ossuary support-period-sbom product.cdx.json --critical-top-n 10
```

The "top-N by structural importance" choice is deliberate: ranking *all*
components as critical would let a tiny utility with bus factor 1 cap a
product's support claim, even if that utility is structurally trivial.

### 12.3 Annex VII technical-documentation record

Article 13(4) requires the cybersecurity risk assessment to be included in
the technical documentation set out in Annex VII; Articles 13(12)–(13)
require that documentation to be retained ≥10 years or for the support
period (whichever is longer). A loose JSON dump from Ossuary is not an
audit-ready artefact for that purpose.

The `--annex-vii` flag on `score-sbom` produces a structured record
declaring:

- Tool name, Ossuary version, scoring methodology version (declared in
  this document's "Version" field).
- Generation timestamp (UTC).
- Source SBOM path, format, spec version, and SHA-256.
- Articles addressed: 13(2), 13(3), 13(4), 13(5), 13(8).
- Explicit scope statement: what the assessment covers and — equally
  important — what it does **not** cover (vulnerability scanning, licence
  compliance, Article 14 reporting, account-compromise on healthy
  projects, CI/CD exploits).
- Per-component scores with full factor breakdowns.
- The product-level implied support period, including limiting components.

```bash
ossuary score-sbom product.cdx.json --annex-vii governance-assessment.json
```

The schema identifier `ossuary.annex_vii.v1` is included in the record so
downstream tooling can verify the format version it received.

---

## 13. Future Work

1. ~~**Expand ecosystem support**~~: Done - 8 ecosystems (npm, PyPI, Cargo, RubyGems, Packagist, NuGet, Go, GitHub)
2. **Historical snapshots**: Archive reputation/org data for better T-1 analysis
3. **ML enhancement**: Train classifier on larger incident corpus
4. ~~**Dependency file scanning**~~: Done - `ossuary scan` supports requirements.txt, package.json, Cargo.toml, go.mod, Gemfile, composer.json, .csproj
5. **Dependency graph analysis**: Transitive risk aggregation
6. **Maintainer network analysis**: Identify shared maintainer risks across packages
7. ~~**PyPI repository URL discovery**~~: Done - case-insensitive URL extraction with multi-priority fallback
8. ~~**Mature project detection**~~: Done - two-track scoring for projects >5 years old with established history
9. ~~**Takeover detection**~~: Done - proportion shift analysis catches xz-utils pattern 12 months early
10. ~~**SBOM ingestion and Annex VII export**~~: Done in v0.9 — see §12
11. ~~**Implied support period (CRA Art. 13(8))**~~: Done in v0.9 — see §12.2
12. **Surface lifetime-commit count via RiskBreakdown**: would let `support-period-sbom` use the full structural-importance formula instead of falling back on the irreplaceability floor

---

## References

### Essential Reading

These papers directly inform the methodology and should be read in full:

1. **Ladisa, P., et al. (2023). "SoK: Taxonomy of Attacks on Open-Source Software Supply Chains."** IEEE S&P 2023. https://arxiv.org/abs/2204.04008
   — *The* foundational taxonomy. Identifies 12 attack categories; governance-based attacks are a distinct class. Start here.

2. **Eghbal, N. (2016). "Roads and Bridges: The Unseen Labor Behind Our Digital Infrastructure."** Ford Foundation.
   — Established that most OSS is maintained by 1-2 people. The "why bus factor matters" argument for any thesis on this topic.

3. **Raman, N., et al. (2020). "Stress and Burnout in Open Source."** ICSE-NIER 2020. https://dl.acm.org/doi/10.1145/3377816.3381732
   — SVM classifier for toxic discussions; directly influenced Ossuary's sentiment/frustration analysis.

4. **Ohm, M., et al. (2020). "Backstabber's Knife Collection: A Review of Open Source Software Supply Chain Attacks."** DIMVA 2020. https://dasfreak.github.io/Backstabbers-Knife-Collection/
   — Catalog of real-world attacks with patterns. Good for incident classification in lit review.

5. **"A Software Engineering Analysis of the XZ Utils Supply Chain Attack."** arXiv 2504.17473. https://arxiv.org/abs/2504.17473
   — Detailed analysis of the most sophisticated governance attack. The 2.6-year timeline is the key case study.

6. **Guo, Y., et al. (2024). "Sustaining Maintenance Labor for Healthy Open Source Software Projects."** arXiv. https://arxiv.org/abs/2408.06723
   — Argues depleted maintainer capacity → unmaintained projects → security consequences. Supports the activity modifier.

### Additional References

7. "Research Directions in Software Supply Chain Security." ACM TOSEM 2024. https://dl.acm.org/doi/10.1145/3714464
8. Sonatype. "State of the Software Supply Chain." Annual Report. https://www.sonatype.com/state-of-the-software-supply-chain
9. Synopsys. (2022). "Open Source Security and Risk Analysis Report."
10. OpenSSF Scorecard - https://securityscorecards.dev/
11. **Goggins, S., Germonprez, M., & Lumbard, K. (2021). "Making Open Source Project Health Transparent."** IEEE Computer, 54(8), 104–111. https://doi.org/10.1109/MC.2021.3084015 — Key paper on the CHAOSS project and its approach to community health metrics.
12. **Avelino, G., Passos, L., Hora, A., & Valente, M. T. (2016). "A Novel Approach for Estimating Truck Factors."** 24th IEEE International Conference on Program Comprehension (ICPC), 1–10. https://arxiv.org/abs/1604.06766 — Defines the algorithm Ossuary's bus factor metric is based on: minimum contributors whose departure causes >50% of files to become orphaned.
13. CHAOSS Contributor Absence Factor metric definition - https://chaoss.community/kb/metric-contributor-absence-factor/
14. CISA Alert AA25-266A. "Widespread Supply Chain Compromise Impacting npm Ecosystem." September 23, 2025. https://www.cisa.gov/news-events/alerts/2025/09/23/widespread-supply-chain-compromise-impacting-npm-ecosystem
15. CERT/CC VU#534320. "npm ecosystem design weaknesses enabling supply chain compromise." https://kb.cert.org/vuls/id/534320
16. Check Point Research. "The Great NPM Heist: September 2025." https://blog.checkpoint.com/crypto/the-great-npm-heist-september-2025/
17. Unit 42 / Palo Alto Networks. "Shai-Hulud: npm Supply Chain Worm." https://unit42.paloaltonetworks.com/npm-supply-chain-attack/
18. **Cosentino, V., Canovas Izquierdo, J. L., & Cabot, J. (2015). "Assessing the Bus Factor of Git Repositories."** 22nd IEEE SANER, 499–503. https://doi.org/10.1109/SANER.2015.7081864 — Earlier bus factor computation work.
19. LFX Insights / Linux Foundation Open Source Index - https://insights.linuxfoundation.org/open-source-index
20. OSSInsight (PingCAP) - https://ossinsight.io/

---

*Document version: 6.3*
*Last updated: April 2026*
*Validation dataset: 170 packages across 8 ecosystems (Scope B: 96.0% precision, 75.0% recall, F1 0.842)*
*Run validation: `python scripts/validate.py -o validation_results.json`*
