#!/usr/bin/env python3
"""Analyze validation results under different in-scope definitions.

Reads validation_results.json, tags each incident by detectability tier,
computes metrics under 5 different scope definitions, and prints comparison.
"""

import json
from collections import defaultdict

with open("/home/anicka/playground/ossuary/validation_results.json") as f:
    data = json.load(f)

results = data["results"]

# =============================================================================
# Step 1: Tag every incident by detectability tier
# =============================================================================

# Tier 1: Governance decay → compromise (primary target)
TIER1_GOVERNANCE_DECAY = {
    "event-stream", "left-pad", "tukaani-project/xz", "figlief/ctx",
    "polyfillpolyfill/polyfill-library",
    "coa", "rc",  # dormant + compromised
    "purescript",  # maintainer sabotage after governance dispute
    "jteeuwen/go-bindata",  # username recycling, abandoned project
}

# Tier 2: Protestware / single-maintainer sabotage / governance fragility
TIER2_PROTESTWARE = {
    "colors",
    "faker",      # expected FN (community fork)
    "node-ipc",   # active maintainer, hard to detect
    "es5-ext",    # anti-war postinstall (non-destructive)
    "event-source-polyfill",  # anti-war runtime message (non-destructive)
    "is-promise",  # accidental breakage, bus factor 1
}

# Tier 3: Account compromise where governance was ALSO weak
TIER3_WEAK_GOV_COMPROMISE = {
    "strong_password", "rest-client", "bootstrap-sass",
    "is",  # if present
}

# Tier 4: Account compromise at well-governed projects
TIER4_STRONG_GOV_COMPROMISE = {
    "ua-parser-js", "eslint-scope", "LottieFiles/lottie-player",
    "chalk",  # 2025 phishing incident
    "cline", "solana-labs/solana-web3.js",
    "num2words",  # org-backed (savoirfairelinux), 15% concentration, pure credential phishing
    "axios",  # 19% conc, 223 commits/yr, org-owned, bf=6 — pure credential theft
}

# Tier 5: CI/CD exploits
TIER5_CICD = {
    "reviewdog/action-setup", "codecov/codecov-action",
    "web-infra-dev/rspack", "ultralytics",
    "tj-actions/changed-files",  # if present
    "nrwl/nx",  # if present
    "aquasecurity/trivy-action",  # CI/CD tag force-push, TeamPCP stealer
}

# Governance risk (no incident)
GOVERNANCE_RISK = {
    "atomicwrites", "moment", "bcrypt", "orjson", "kind-of",
    "is-number", "extend", "mkdirp", "boltdb/bolt",
    "github.com/go-kit/kit", "devise",
    "core-js",  # maintainer imprisoned, no malicious release
}


def get_tier(r):
    """Classify a result into a tier."""
    case = r["case"]
    name = case["name"]
    outcome = case["expected_outcome"]

    if outcome == "safe":
        return "control"

    if name in TIER1_GOVERNANCE_DECAY:
        return "T1_governance_decay"
    if name in TIER2_PROTESTWARE:
        return "T2_protestware"
    if name in TIER3_WEAK_GOV_COMPROMISE:
        return "T3_weak_gov_compromise"
    if name in TIER4_STRONG_GOV_COMPROMISE:
        return "T4_strong_gov_compromise"
    if name in TIER5_CICD:
        return "T5_cicd"
    if name in GOVERNANCE_RISK:
        return "T_risk_no_incident"

    # Fallback: check attack_type
    at = case.get("attack_type", "")
    if at == "governance_failure":
        return "T1_governance_decay"
    if at == "maintainer_sabotage":
        return "T2_protestware"
    if at == "governance_risk":
        return "T_risk_no_incident"
    if at == "account_compromise":
        # Check notes for "EXPECTED FN" = well-governed
        notes = case.get("notes", "")
        if "EXPECTED FN" in notes:
            return "T4_strong_gov_compromise"
        else:
            return "T3_weak_gov_compromise"

    return "unclassified"


# Tag all results
tagged = []
tier_counts = defaultdict(int)
for r in results:
    tier = get_tier(r)
    tier_counts[tier] += 1
    tagged.append({**r, "_tier": tier})

print("=" * 70)
print("TIER DISTRIBUTION")
print("=" * 70)
for tier, count in sorted(tier_counts.items()):
    print(f"  {tier:35s} {count:3d}")
print()

# Show each incident with tier and score
print("=" * 70)
print("ALL INCIDENTS BY TIER")
print("=" * 70)
for t in tagged:
    if t["case"]["expected_outcome"] == "safe" and t["_tier"] == "control":
        continue
    name = t["case"]["name"]
    score = t["score"]
    risk = t["risk_level"]
    pred = t["predicted_outcome"]
    cls = t["classification"]
    tier = t["_tier"]
    print(f"  {tier:30s}  {score:3d} {risk:10s} {cls:2s}  {name}")
print()


# =============================================================================
# Step 2: Compute metrics under different scope definitions
# =============================================================================

def compute_metrics(tagged_results, in_scope_tiers, label):
    """Compute precision/recall/accuracy for a given scope definition."""
    tp = fp = tn = fn = 0
    in_scope_details = []

    for t in tagged_results:
        tier = t["_tier"]
        score = t["score"]
        predicted_risky = score >= 60
        is_incident = t["case"]["expected_outcome"] == "incident"

        if tier == "control":
            # Controls always count
            if predicted_risky:
                fp += 1
            else:
                tn += 1
        elif tier in in_scope_tiers:
            # In-scope incidents
            if predicted_risky and is_incident:
                tp += 1
                in_scope_details.append((t["case"]["name"], score, "TP"))
            elif not predicted_risky and is_incident:
                fn += 1
                in_scope_details.append((t["case"]["name"], score, "FN"))
            elif predicted_risky and not is_incident:
                fp += 1
            else:
                tn += 1
        # Out-of-scope incidents: excluded from metrics entirely

    total = tp + tn + fp + fn
    accuracy = (tp + tn) / total if total else 0
    precision = tp / (tp + fp) if (tp + fp) else 0
    recall = tp / (tp + fn) if (tp + fn) else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    # Count out-of-scope incidents detected anyway (bonus)
    out_scope_tiers = {"T1_governance_decay", "T2_protestware", "T3_weak_gov_compromise",
                       "T4_strong_gov_compromise", "T5_cicd", "T_risk_no_incident"} - set(in_scope_tiers)
    bonus_detected = 0
    bonus_total = 0
    for t in tagged_results:
        if t["_tier"] in out_scope_tiers and t["case"]["expected_outcome"] == "incident":
            bonus_total += 1
            if t["score"] >= 60:
                bonus_detected += 1

    # Governance risk identification (separate metric)
    risk_detected = sum(1 for t in tagged_results
                        if t["_tier"] == "T_risk_no_incident" and t["score"] >= 60)
    risk_total = sum(1 for t in tagged_results
                     if t["_tier"] == "T_risk_no_incident")

    return {
        "label": label,
        "in_scope_incidents": tp + fn,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "total_evaluated": total,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "bonus_detected": bonus_detected,
        "bonus_total": bonus_total,
        "risk_detected": risk_detected,
        "risk_total": risk_total,
        "fn_details": [(n, s) for n, s, c in in_scope_details if c == "FN"],
    }


# Define 5 scope definitions to test
scopes = [
    (
        "A: Everything (current)",
        {"T1_governance_decay", "T2_protestware", "T3_weak_gov_compromise",
         "T4_strong_gov_compromise", "T5_cicd", "T_risk_no_incident"},
    ),
    (
        "B: Gov decay + protestware + weak-gov + risk",
        {"T1_governance_decay", "T2_protestware", "T3_weak_gov_compromise",
         "T_risk_no_incident"},
    ),
    (
        "C: Gov decay + protestware + risk only",
        {"T1_governance_decay", "T2_protestware", "T_risk_no_incident"},
    ),
    (
        "D: Gov decay + weak-gov + risk (no protestware)",
        {"T1_governance_decay", "T3_weak_gov_compromise", "T_risk_no_incident"},
    ),
    (
        "E: Gov decay only + risk (strictest)",
        {"T1_governance_decay", "T_risk_no_incident"},
    ),
]

print("=" * 70)
print("METRICS UNDER DIFFERENT SCOPE DEFINITIONS")
print("=" * 70)
print()

for label, scope_tiers in scopes:
    m = compute_metrics(tagged, scope_tiers, label)
    print(f"--- {m['label']} ---")
    print(f"  In-scope incidents: {m['in_scope_incidents']}")
    print(f"  TP={m['tp']}  FP={m['fp']}  TN={m['tn']}  FN={m['fn']}")
    print(f"  Accuracy:  {m['accuracy']:.1%}")
    print(f"  Precision: {m['precision']:.1%}")
    print(f"  Recall:    {m['recall']:.1%}")
    print(f"  F1:        {m['f1']:.3f}")
    if m["bonus_total"]:
        print(f"  Bonus (out-of-scope detected): {m['bonus_detected']}/{m['bonus_total']}")
    if m["risk_total"] and "T_risk_no_incident" not in scope_tiers:
        print(f"  Risk identification: {m['risk_detected']}/{m['risk_total']}")
    if m["fn_details"]:
        print(f"  False negatives:")
        for name, score in m["fn_details"]:
            print(f"    {name}: score {score}")
    print()

# =============================================================================
# Step 3: Also try threshold sensitivity (50, 55, 60, 65)
# =============================================================================

print("=" * 70)
print("THRESHOLD SENSITIVITY (Scope B: gov_decay + protestware + weak_gov + risk)")
print("=" * 70)
print()

scope_b = {"T1_governance_decay", "T2_protestware", "T3_weak_gov_compromise", "T_risk_no_incident"}

for threshold in [50, 55, 60, 65]:
    tp = fp = tn = fn = 0
    fn_names = []
    fp_names = []
    for t in tagged:
        tier = t["_tier"]
        score = t["score"]
        predicted_risky = score >= threshold
        is_incident = t["case"]["expected_outcome"] == "incident"

        if tier == "control":
            if predicted_risky:
                fp += 1
                fp_names.append((t["case"]["name"], score))
            else:
                tn += 1
        elif tier in scope_b:
            if predicted_risky and is_incident:
                tp += 1
            elif not predicted_risky and is_incident:
                fn += 1
                fn_names.append((t["case"]["name"], score))
            elif predicted_risky:
                fp += 1
                fp_names.append((t["case"]["name"], score))
            else:
                tn += 1

    total = tp + tn + fp + fn
    precision = tp / (tp + fp) if (tp + fp) else 0
    recall = tp / (tp + fn) if (tp + fn) else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    print(f"  Threshold ≥{threshold}: TP={tp} FP={fp} FN={fn} "
          f"Prec={precision:.1%} Rec={recall:.1%} F1={f1:.3f}")
    if fp_names:
        print(f"    FP: {', '.join(f'{n}({s})' for n, s in fp_names[:5])}")
    if fn_names:
        print(f"    FN: {', '.join(f'{n}({s})' for n, s in fn_names[:5])}")
    print()

# =============================================================================
# Step 4: Per-tier detection rates (Scope B)
# =============================================================================

print("=" * 70)
print("PER-TIER DETECTION RATES")
print("=" * 70)
print()

tier_order = ["T1_governance_decay", "T2_protestware", "T3_weak_gov_compromise",
              "T_risk_no_incident", "T4_strong_gov_compromise", "T5_cicd"]
tier_labels = {
    "T1_governance_decay": "T1: Governance decay",
    "T2_protestware": "T2: Protestware / sabotage",
    "T3_weak_gov_compromise": "T3: Weak-gov compromise",
    "T_risk_no_incident": "T_risk: Governance risk (no incident)",
    "T4_strong_gov_compromise": "T4: Strong-gov compromise (OOS)",
    "T5_cicd": "T5: CI/CD exploits (OOS)",
}

for tier in tier_order:
    tier_items = [t for t in tagged if t["_tier"] == tier]
    if not tier_items:
        continue
    detected = sum(1 for t in tier_items if t["score"] >= 60)
    total = len(tier_items)
    pct = detected / total * 100 if total else 0
    label = tier_labels.get(tier, tier)
    in_scope = tier in scope_b
    marker = "  " if in_scope else "* "
    print(f"{marker}{label:45s} {detected}/{total} ({pct:.0f}%)")
    # Show misses
    for t in tier_items:
        if t["score"] < 60:
            print(f"    miss: {t['case']['name']} (score {t['score']})")

print()
print("  * = out of scope (not counted toward recall)")
