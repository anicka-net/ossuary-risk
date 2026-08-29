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

Evidence modes (current-state cases — controls and T_risk — have
``cutoff_date=None`` in the case definitions):

* **Pinned checkpoint (required for final/thesis runs):** pass
  ``--replay-instant`` and ``--snapshot-collected-before`` together.
  Current-state cases are then collected and scored at the declared
  canonical current-state cutoff, replaying the frozen snapshot cache
  (``cache_only=True``, no upstream contact), exactly like
  ``scripts/validate.py --replay-instant ...``. Historical incidents
  keep their declared T-1 cutoffs and replay from the same frozen
  snapshot pool.
* **Run-time cutoff (explicit opt-in only):** pass
  ``--allow-run-time-cutoff`` to score current-state cases at
  ``datetime.now()`` against whatever the snapshot SLA serves. This
  mode is for interactive exploration only; final artefacts produced
  this way are not frozen-evidence diagnostics.

Usage:
    python scripts/ablation.py --limit 5 --allow-run-time-cutoff   # smoke
    python scripts/ablation.py --factors visibility,frustration --limit 20 \
        --allow-run-time-cutoff
    python scripts/ablation.py \
        --replay-instant 2026-08-15T15:49:38.364446Z \
        --snapshot-collected-before 2026-08-15T18:48:00Z \
        --output thesis/ablation_results_pinned_20260815.json \
        --table thesis/ablation_table_pinned_20260815.md
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
    parse_replay_instant,
)
from ossuary.scoring import METHODOLOGY_VERSION  # noqa: E402
from ossuary.scoring.engine import RiskScorer  # noqa: E402
from ossuary.scoring.factors import ProtectiveFactors  # noqa: E402
from ossuary._compat import parse_utc_date_end  # noqa: E402
from ossuary.services.repo_cache import COLLECTOR_VERSION  # noqa: E402
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


def check_arg_compatibility(replay_instant, snapshot_collected_before,
                            allow_run_time_cutoff):
    """Return an error message for an unsafe flag combination, or None.

    Guards the diagnosed harness defect (2026-08-29): a run without a
    declared canonical current-state checkpoint silently scored
    controls/T_risk at ``datetime.now()``, drifting current-state
    evidence away from the canonical validation checkpoint (isarray
    40→60 between 15 Aug and 28 Aug on identical snapshot blobs). Final
    ablation artefacts must pin the checkpoint; run-time scoring is an
    explicit opt-in for interactive exploration only.
    """
    if bool(replay_instant) != bool(snapshot_collected_before):
        return ("--replay-instant and --snapshot-collected-before are "
                "required together")
    if replay_instant is None and not allow_run_time_cutoff:
        return ("refusing to score current-state cases at run time: pass "
                "--replay-instant and --snapshot-collected-before to pin the "
                "canonical current-state checkpoint (frozen-evidence mode), or "
                "--allow-run-time-cutoff to explicitly opt in to run-time "
                "scoring (interactive exploration only, not a frozen-evidence "
                "diagnostic)")
    if replay_instant is not None and allow_run_time_cutoff:
        return ("--allow-run-time-cutoff is meaningless with --replay-instant: "
                "the current-state cutoff is already pinned")
    return None


async def collect_all(cases, *, current_state_cutoff=None,
                      snapshot_collected_before=None, cache_only=False):
    """Fetch upstream data once per case; return case_key -> (collected, cutoff, error).

    Keyed on ``case_key(case)`` (full tuple) rather than ``case.name``: the
    validation set deliberately contains duplicate names with different
    scenarios — ``chalk`` and ``axios`` each appear once as a control
    (current state) and once as a 2026 T4 incident (pre-compromise cutoff).
    Keying on name alone would let the second iteration overwrite the
    first's data, contaminating the per-case dump that downstream §5.10.1
    cites.

    ``current_state_cutoff`` (the pinned replay instant) changes both the
    collection lookup and the scoring cutoff for current-state cases
    (``cutoff_date=None``): they are collected via the frozen-snapshot
    replay path (``cutoff_date=replay instant``, ``collected_before``
    upper bound) and scored at that instant — never at ``datetime.now()``.
    Historical incidents keep their declared T-1 cutoffs in both modes.
    """
    cache = {}
    print(f"Collecting upstream data for {len(cases)} packages...")
    for i, case in enumerate(cases, 1):
        # Current-state cases: cutoff_for_collect is None in run-time mode
        # (SLA-served current snapshot) or the pinned replay instant in
        # frozen-evidence mode. cutoff_for_score is always concrete.
        cutoff_for_collect = (
            parse_utc_date_end(case.cutoff_date)
            if case.cutoff_date else current_state_cutoff
        )
        cutoff_for_score = cutoff_for_collect or datetime.now()
        key = case_key(case)
        try:
            collected, warnings = await cached_collect(
                case.name, case.ecosystem, case.repo_url,
                cutoff_date=cutoff_for_collect,
                cache_only=cache_only,
                snapshot_collected_before=snapshot_collected_before,
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
            case.name,
            case.ecosystem,
            collected,
            cutoff,
            is_historical=case.cutoff_date is not None,
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


def write_markdown_table(path, runs, factors, baseline, n_cases, provenance=None):
    base = baseline
    lines = [
        f"# Factor ablation — scope-B (n={base['n']} of {n_cases} cases)",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
    ]
    if provenance:
        lines += [
            f"Evidence mode: {provenance.get('evidence_mode', 'unspecified')}",
            f"Methodology: {provenance.get('methodology_version', '?')} "
            f"(collector v{provenance.get('collector_version', '?')})",
        ]
        if provenance.get("current_state_cutoff"):
            lines.append(
                "Current-state checkpoint (replayed, cache-only): "
                f"{provenance['current_state_cutoff']} "
                f"(snapshots collected before {provenance['snapshot_collected_before']})"
            )
    lines += [
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
    ap.add_argument(
        "--replay-instant",
        type=parse_replay_instant,
        help=("Pin the canonical current-state cutoff (ISO-8601 with UTC "
              "offset). Current-state cases are collected from the frozen "
              "snapshot cache and scored at this instant; no upstream contact"),
    )
    ap.add_argument(
        "--snapshot-collected-before",
        type=parse_replay_instant,
        help=("Upper bound for snapshot collection time in pinned mode; "
              "prevents later refreshes from replacing frozen evidence"),
    )
    ap.add_argument(
        "--allow-run-time-cutoff",
        action="store_true",
        help=("Explicitly opt in to scoring current-state cases at run time "
              "(interactive exploration only; not a frozen-evidence diagnostic)"),
    )
    args = ap.parse_args()

    err = check_arg_compatibility(
        args.replay_instant, args.snapshot_collected_before,
        args.allow_run_time_cutoff,
    )
    if err:
        ap.error(err)

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

    cache = await collect_all(
        cases,
        current_state_cutoff=args.replay_instant,
        snapshot_collected_before=args.snapshot_collected_before,
        cache_only=bool(args.replay_instant),
    )

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
    try:
        import subprocess
        git_commit = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        git_commit = None
    payload = {
        "timestamp": datetime.now().isoformat(),
        "methodology_version": METHODOLOGY_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "git_commit": git_commit,
        "evidence_mode": (
            "pinned current-state checkpoint (cache-only replay)"
            if args.replay_instant else "run-time current-state cutoff (opt-in)"
        ),
        "current_state_cutoff": (
            args.replay_instant.isoformat() + "Z" if args.replay_instant else None
        ),
        "snapshot_collected_before": (
            args.snapshot_collected_before.isoformat() + "Z"
            if args.snapshot_collected_before else None
        ),
        "n_cases": len(cases),
        "scope": "B (in-scope only)",
        "threshold": RISK_THRESHOLD,
        "factors": factors,
        "runs": runs,
    }
    out_path.write_text(json.dumps(payload, indent=2))

    write_markdown_table(
        args.table, runs, factors, baseline_metrics, len(cases),
        provenance=payload,
    )

    print(f"\nResults JSON: {out_path}")
    print(f"Markdown table: {args.table}")


if __name__ == "__main__":
    asyncio.run(main())
