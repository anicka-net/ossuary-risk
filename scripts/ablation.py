#!/usr/bin/env python3
"""
Factor ablation harness for Ossuary risk scoring.

Re-runs the full validation set with each protective factor clamped to 0
in turn, and reports how scope-B precision / recall / F1 shift relative
to the un-clamped baseline. Output: a JSON results file plus a markdown
table for thesis §5.10.

Strategy: collect per-package data once, then re-score N+1 times with
the protective-factor calculator monkey-patched. This avoids re-hitting
the network for each factor and keeps runs comparable on identical inputs.

Usage:
    python scripts/ablation.py
    python scripts/ablation.py --factors visibility,frustration --limit 20
    python scripts/ablation.py --output thesis/ablation_results.json \
                               --table thesis/ablation_table.md
"""

import argparse
import asyncio
import json
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from validate import (  # noqa: E402  (path inserted above)
    VALIDATION_CASES,
    ValidationResult,
    RISK_THRESHOLD,
)
from ossuary.scoring.engine import RiskScorer  # noqa: E402
from ossuary.scoring.factors import ProtectiveFactors  # noqa: E402
from ossuary.services.scorer import (  # noqa: E402
    cached_collect,
    calculate_score_for_date,
)


# Protective factors to ablate. Each entry is the field stem of a
# ProtectiveFactors attribute (e.g. "visibility" → visibility_score).
# Order roughly by expected impact: structural / popularity factors first,
# then signal-derived factors, then small modifiers.
FACTORS = [
    "visibility",
    "reputation",
    "funding",
    "org",
    "distributed",
    "community",
    "cii",
    "maturity",
    "frustration",
    "sentiment",
    "takeover_risk",
]

# Evidence fields that should be cleared alongside the score, so the
# ablated breakdown is self-consistent for any downstream introspection.
EVIDENCE_FIELDS = {
    "reputation": "reputation_evidence",
    "funding": "funding_evidence",
    "frustration": "frustration_evidence",
    "sentiment": "sentiment_evidence",
    "maturity": "maturity_evidence",
    "takeover_risk": "takeover_risk_evidence",
}


@contextmanager
def clamp_factor(factor):
    """Wrap RiskScorer.calculate_protective_factors so the named factor
    contributes 0 to the score. Pass ``None`` for the un-patched baseline."""
    if factor is None:
        yield
        return

    score_field = f"{factor}_score"
    if not hasattr(ProtectiveFactors(), score_field):
        raise ValueError(f"Unknown ProtectiveFactors attribute: {score_field}")

    original = RiskScorer.calculate_protective_factors
    evidence_field = EVIDENCE_FIELDS.get(factor)

    def patched(self, metrics, ecosystem="npm"):
        pf = original(self, metrics, ecosystem)
        setattr(pf, score_field, 0)
        if evidence_field and hasattr(pf, evidence_field):
            current = getattr(pf, evidence_field)
            setattr(pf, evidence_field, [] if isinstance(current, list) else None)
        return pf

    RiskScorer.calculate_protective_factors = patched
    try:
        yield
    finally:
        RiskScorer.calculate_protective_factors = original


IN_SCOPE_TIERS = {"T1", "T2", "T3", "T_risk"}


def is_in_scope(case) -> bool:
    """Scope-B membership per the §5.5 tier framework.

    Controls are always in-scope. Incidents are in-scope iff their
    ``tier`` is one of T1 (governance decay), T2 (protestware), T3
    (weak-gov compromise), or T_risk (governance risk, no incident).
    T4 (strong-gov compromise) and T5 (CI/CD exploits) are out of
    scope. An incident without a tier is treated as untiered and
    therefore out of scope — the caller should tier it properly.
    """
    if case.expected_outcome == "safe":
        return True
    return case.tier in IN_SCOPE_TIERS


async def collect_all(cases):
    """Fetch upstream data once per case; return case_key -> (collected, cutoff, error).

    Keyed on ``case_key(case)`` (full tuple) rather than ``case.name``: the
    validation set deliberately contains duplicate names with different
    scenarios — ``chalk`` and ``axios`` each appear once as a control
    (current state) and once as a 2026 T4 incident (pre-compromise cutoff).
    Keying on name alone would let the second iteration overwrite the
    first's data, contaminating the per-case dump that downstream §5.10.1
    cites.
    """
    cache = {}
    print(f"Collecting upstream data for {len(cases)} packages...")
    for i, case in enumerate(cases, 1):
        # Pass the original cutoff_date — None for controls (current
        # scoring, freshness SLA applies) vs an explicit historical
        # datetime for incidents. The scorer below derives a concrete
        # cutoff for `calculate_score_for_date` separately.
        cutoff_for_collect = (
            datetime.strptime(case.cutoff_date, "%Y-%m-%d")
            if case.cutoff_date else None
        )
        cutoff_for_score = cutoff_for_collect or datetime.now()
        key = case_key(case)
        try:
            collected, warnings = await cached_collect(
                case.name, case.ecosystem, case.repo_url,
                cutoff_date=cutoff_for_collect,
            )
            err = None if collected is not None else (warnings[0] if warnings else "no data")
            cache[key] = (collected, cutoff_for_score, err)
            tag = "ok" if err is None else f"FAIL: {err[:40]}"
            print(f"  [{i}/{len(cases)}] {case.name:<28} {tag}")
        except Exception as e:
            cache[key] = (None, cutoff_for_score, str(e))
            print(f"  [{i}/{len(cases)}] {case.name:<28} EXC: {str(e)[:40]}")
    return cache


def score_one(case, collected, cutoff, prior_error):
    """Score using pre-collected data, return a ValidationResult."""
    result = ValidationResult(case=case)
    if prior_error is not None:
        result.error = prior_error
        return result

    try:
        breakdown = calculate_score_for_date(
            case.name, case.ecosystem, collected, cutoff,
        )
    except Exception as e:
        result.error = str(e)
        return result

    if breakdown.final_score is None:
        # INSUFFICIENT_DATA: methodology refuses to score. Excluded from metrics.
        result.error = f"INSUFFICIENT_DATA: {'; '.join(breakdown.incomplete_reasons)}"
        return result

    result.score = breakdown.final_score
    result.risk_level = breakdown.risk_level.value
    result.protective_factors_total = breakdown.protective_factors.total
    result.predicted_outcome = (
        "risky" if breakdown.final_score >= RISK_THRESHOLD else "safe"
    )

    if case.expected_outcome == "incident":
        result.correct = result.predicted_outcome == "risky"
        result.classification = "TP" if result.correct else "FN"
    else:
        result.correct = result.predicted_outcome == "safe"
        result.classification = "TN" if result.correct else "FP"
    return result


def scope_b_metrics(results):
    """Precision / recall / F1 over the scope-B subset only."""
    pool = [
        r for r in results
        if is_in_scope(r.case) and r.error is None and r.score is not None
    ]
    tp = sum(1 for r in pool if r.classification == "TP")
    tn = sum(1 for r in pool if r.classification == "TN")
    fp = sum(1 for r in pool if r.classification == "FP")
    fn = sum(1 for r in pool if r.classification == "FN")
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {
        "n": len(pool),
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "precision": prec,
        "recall": rec,
        "f1": f1,
    }


def case_key(case):
    """Unique key for a case — name alone collides (chalk and axios each
    appear twice in the dataset, once as control and once as 2025 incident)."""
    return (case.name, case.ecosystem, case.expected_outcome, case.cutoff_date or "")


def diff_results(baseline, ablated):
    """List cases whose classification changed under this ablation."""
    by_key = {case_key(r.case): r for r in baseline}
    flips = []
    for r in ablated:
        b = by_key.get(case_key(r.case))
        if b is None or b.error or r.error:
            continue
        if r.classification != b.classification:
            flips.append({
                "package": r.case.name,
                "ecosystem": r.case.ecosystem,
                "expected": r.case.expected_outcome,
                "cutoff": r.case.cutoff_date,
                "baseline": {"score": b.score, "class": b.classification},
                "ablated": {"score": r.score, "class": r.classification},
            })
    return flips


def dump_results(results):
    """Lightweight per-case dump for offline re-analysis."""
    return [
        {
            "name": r.case.name,
            "ecosystem": r.case.ecosystem,
            "expected": r.case.expected_outcome,
            "attack_type": r.case.attack_type,
            "tier": r.case.tier,
            "cutoff": r.case.cutoff_date,
            "in_scope": is_in_scope(r.case),
            "score": r.score,
            "risk_level": r.risk_level,
            "classification": r.classification if r.error is None else None,
            "error": r.error,
        }
        for r in results
    ]


def run_pass(label, factor, cases, cache):
    print(f"\n=== {label} (clamp: {factor or 'none'}) ===")
    with clamp_factor(factor):
        results = []
        for case in cases:
            collected, cutoff, prior_error = cache[case_key(case)]
            r = score_one(case, collected, cutoff, prior_error)
            results.append(r)
    metrics = scope_b_metrics(results)
    print(
        f"  scope-B (n={metrics['n']}): "
        f"P={metrics['precision']:.2f} R={metrics['recall']:.2f} F1={metrics['f1']:.2f} "
        f"(TP={metrics['tp']} FN={metrics['fn']} FP={metrics['fp']} TN={metrics['tn']})"
    )
    return results, metrics


def write_markdown_table(path, runs, factors, baseline, n_cases):
    base = baseline
    lines = [
        f"# Factor ablation — scope-B (n={base['n']} of {n_cases} cases)",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Each row clamps one protective factor to 0 and re-runs the full validation set.",
        "Negative ΔF1 means the factor was load-bearing on this dataset; values close to",
        "zero mean the factor's contribution is not detectable at the scope-B threshold of",
        f"{RISK_THRESHOLD}. ΔP and ΔR show whether the factor was holding precision or",
        "recall up.",
        "",
        "| Factor clamped | TP | FN | FP | TN | P | R | F1 | ΔP | ΔR | ΔF1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| _baseline_ | {base['tp']} | {base['fn']} | {base['fp']} | {base['tn']} | "
        f"{base['precision']:.2f} | {base['recall']:.2f} | {base['f1']:.2f} | — | — | — |",
    ]
    for f in factors:
        s = runs[f]["metrics"]
        dp = s['precision'] - base['precision']
        dr = s['recall'] - base['recall']
        df = s['f1'] - base['f1']
        lines.append(
            f"| `{f}` | {s['tp']} | {s['fn']} | {s['fp']} | {s['tn']} | "
            f"{s['precision']:.2f} | {s['recall']:.2f} | {s['f1']:.2f} | "
            f"{dp:+.2f} | {dr:+.2f} | {df:+.2f} |"
        )

    # Per-factor flip lists for the narrative
    lines += ["", "## Classification flips per ablation", ""]
    for f in factors:
        flips = runs[f]["flips"]
        if not flips:
            lines.append(f"- **`{f}`**: no scope-B classification changes.")
            continue
        lines.append(f"- **`{f}`** ({len(flips)} flip(s)):")
        for fl in flips:
            lines.append(
                f"  - `{fl['package']}` ({fl['ecosystem']}): "
                f"{fl['baseline']['class']}→{fl['ablated']['class']}, "
                f"score {fl['baseline']['score']}→{fl['ablated']['score']}"
            )
    Path(path).write_text("\n".join(lines) + "\n")


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", "-o", default=str(REPO / "thesis" / "ablation_results.json"))
    ap.add_argument("--table", "-t", default=str(REPO / "thesis" / "ablation_table.md"))
    ap.add_argument("--factors", help="Comma-separated subset of factor names")
    ap.add_argument("--limit", type=int, help="Only run first N cases (smoke test)")
    args = ap.parse_args()

    cases = list(VALIDATION_CASES)
    if args.limit:
        cases = cases[:args.limit]

    factors = (
        [f.strip() for f in args.factors.split(",")]
        if args.factors else list(FACTORS)
    )
    for f in factors:
        if not hasattr(ProtectiveFactors(), f"{f}_score"):
            raise SystemExit(f"Unknown factor: {f}. Known: {FACTORS}")

    cache = await collect_all(cases)

    runs = {}
    baseline_results, baseline_metrics = run_pass("BASELINE", None, cases, cache)
    runs["baseline"] = {
        "metrics": baseline_metrics,
        "flips": [],
        "results": dump_results(baseline_results),
    }

    for f in factors:
        results, metrics = run_pass(f"ABLATE: {f}", f, cases, cache)
        runs[f] = {
            "metrics": metrics,
            "flips": diff_results(baseline_results, results),
            "results": dump_results(results),
        }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now().isoformat(),
        "n_cases": len(cases),
        "scope": "B (in-scope only)",
        "threshold": RISK_THRESHOLD,
        "factors": factors,
        "runs": runs,
    }
    out_path.write_text(json.dumps(payload, indent=2))

    write_markdown_table(args.table, runs, factors, baseline_metrics, len(cases))

    print(f"\nResults JSON: {out_path}")
    print(f"Markdown table: {args.table}")


if __name__ == "__main__":
    asyncio.run(main())
