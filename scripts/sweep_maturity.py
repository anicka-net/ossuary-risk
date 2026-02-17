#!/usr/bin/env python3
"""
Quick parameter sweep for maturity scoring.

Collects data ONCE, then re-scores with different maturity parameters.
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from ossuary.services.scorer import collect_package_data, calculate_score_for_date
from ossuary.scoring.engine import RiskScorer, PackageMetrics

# Import validation cases
from validate import VALIDATION_CASES, RISK_THRESHOLD


async def collect_all():
    """Collect data for all validation cases (slow, done once)."""
    collected = []
    total = len(VALIDATION_CASES)
    for i, case in enumerate(VALIDATION_CASES):
        print(f"  [{i+1}/{total}] {case.name}...", end=" ", flush=True)
        try:
            cutoff = datetime.now()
            if case.cutoff_date:
                cutoff = datetime.strptime(case.cutoff_date, "%Y-%m-%d")

            data, warnings = await collect_package_data(
                case.name, case.ecosystem, case.repo_url,
            )
            if data is None:
                print("SKIP (no data)")
                collected.append((case, None, cutoff))
                continue

            breakdown = calculate_score_for_date(
                case.name, case.ecosystem, data, cutoff,
            )
            # Extract the PackageMetrics that was built internally
            # We need to reconstruct it from the collected data
            collected.append((case, data, cutoff))
            print(f"OK")
        except Exception as e:
            print(f"ERROR: {e}")
            collected.append((case, None, cutoff))
    return collected


def rescore(collected, maturity_bonus, lifetime_threshold):
    """Re-score all packages with given maturity parameters.

    Args:
        collected: list of (case, collected_data, cutoff)
        maturity_bonus: points for maturity factor (e.g. -15, -10, -5, 0)
        lifetime_threshold: use lifetime concentration when commits_last_year < this
    """
    scorer = RiskScorer()
    tp = fp = tn = fn = 0

    for case, data, cutoff in collected:
        if data is None:
            continue

        # Rebuild breakdown using services layer
        breakdown = calculate_score_for_date(
            case.name, case.ecosystem, data, cutoff,
        )

        score = breakdown.final_score
        predicted = "risky" if score >= RISK_THRESHOLD else "safe"

        if case.expected_outcome == "incident":
            if predicted == "risky":
                tp += 1
            else:
                fn += 1
        else:
            if predicted == "safe":
                tn += 1
            else:
                fp += 1

    total = tp + fp + tn + fn
    accuracy = (tp + tn) / total if total else 0
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "accuracy": accuracy, "precision": precision,
        "recall": recall, "f1": f1,
    }


def patch_and_rescore(collected, maturity_bonus, lifetime_threshold):
    """Monkey-patch the scoring engine, rescore, then restore."""
    import ossuary.scoring.engine as eng

    # Save originals
    orig_calculate = eng.RiskScorer.calculate
    orig_protective = eng.RiskScorer.calculate_protective_factors

    def patched_calculate(self, package_name, ecosystem, metrics, repo_url=None):
        from ossuary.scoring.factors import RiskBreakdown, RiskLevel
        breakdown = RiskBreakdown(
            package_name=package_name, ecosystem=ecosystem, repo_url=repo_url,
        )
        breakdown.maintainer_concentration = metrics.maintainer_concentration
        breakdown.commits_last_year = metrics.commits_last_year
        breakdown.unique_contributors = metrics.unique_contributors
        breakdown.weekly_downloads = metrics.weekly_downloads

        if metrics.is_mature:
            if metrics.commits_last_year < lifetime_threshold:
                breakdown.base_risk = self.calculate_base_risk(metrics.lifetime_concentration)
            else:
                breakdown.base_risk = self.calculate_base_risk(metrics.maintainer_concentration)
            raw_activity = self.calculate_activity_modifier(metrics.commits_last_year)
            breakdown.activity_modifier = min(0, raw_activity)
        else:
            breakdown.base_risk = self.calculate_base_risk(metrics.maintainer_concentration)
            breakdown.activity_modifier = self.calculate_activity_modifier(metrics.commits_last_year)

        breakdown.protective_factors = self.calculate_protective_factors(metrics, ecosystem)
        # Override maturity score
        if metrics.is_mature:
            breakdown.protective_factors.maturity_score = maturity_bonus

        raw_score = breakdown.base_risk + breakdown.activity_modifier + breakdown.protective_factors.total
        breakdown.final_score = max(0, min(100, raw_score))
        breakdown.risk_level = RiskLevel.from_score(breakdown.final_score)
        breakdown.explanation = self.generate_explanation(breakdown, metrics)
        breakdown.recommendations = self.generate_recommendations(breakdown)
        return breakdown

    eng.RiskScorer.calculate = patched_calculate

    result = rescore(collected, maturity_bonus, lifetime_threshold)

    # Restore
    eng.RiskScorer.calculate = orig_calculate
    return result


async def main():
    print("=== Collecting data (one-time) ===")
    collected = await collect_all()
    print(f"\nCollected {len(collected)} packages\n")

    # Parameter sweep
    print("=== Parameter Sweep ===")
    print(f"{'bonus':>6s}  {'lt_thresh':>9s}  {'TP':>3s}  {'FP':>3s}  {'FN':>3s}  {'TN':>4s}  {'Acc':>6s}  {'Prec':>6s}  {'Recall':>6s}  {'F1':>6s}")
    print("-" * 75)

    for lifetime_threshold in [1, 4, 8, 12]:
        for maturity_bonus in [0, -5, -10, -15]:
            r = patch_and_rescore(collected, maturity_bonus, lifetime_threshold)
            print(f"{maturity_bonus:>6d}  {lifetime_threshold:>9d}  "
                  f"{r['tp']:>3d}  {r['fp']:>3d}  {r['fn']:>3d}  {r['tn']:>4d}  "
                  f"{r['accuracy']*100:>5.1f}%  {r['precision']*100:>5.1f}%  "
                  f"{r['recall']*100:>5.1f}%  {r['f1']:>5.2f}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
