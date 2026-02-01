#!/usr/bin/env python3
"""
Validation script for ossuary risk scoring methodology.

Tests the scoring model against packages with known outcomes:
- Governance failures / abandoned packages
- Maintainer sabotage (active maintainer turns malicious)
- Account compromises (credentials stolen)
- Governance risk (high bus factor, low activity - may not have incident yet)
- Control packages (well-maintained, good governance)

SCOPE LIMITATIONS:
- This tool detects GOVERNANCE RISK, not all supply chain attacks
- Maintainer sabotage by active, reputable maintainers is hard to detect
- Account compromise is outside scope (requires different signals)

Usage:
    python scripts/validate.py
    python scripts/validate.py --output results.json
    python scripts/validate.py --only incidents
    python scripts/validate.py --only controls
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ossuary.collectors.git import GitCollector
from ossuary.collectors.github import GitHubCollector
from ossuary.collectors.npm import NpmCollector
from ossuary.collectors.pypi import PyPICollector
from ossuary.scoring.engine import PackageMetrics, RiskScorer
from ossuary.scoring.reputation import ReputationScorer
from ossuary.sentiment.analyzer import SentimentAnalyzer


@dataclass
class ValidationCase:
    """A package to validate."""

    name: str
    ecosystem: str  # npm or pypi
    expected_outcome: str  # "incident" or "safe"
    attack_type: Optional[str] = None  # governance, account_compromise, sabotage, etc.
    incident_date: Optional[str] = None  # YYYY-MM-DD
    cutoff_date: Optional[str] = None  # For T-1 analysis (day before incident)
    notes: str = ""
    repo_url: Optional[str] = None  # Override if needed


@dataclass
class ValidationResult:
    """Result of validating a single package."""

    case: ValidationCase
    score: int = 0
    risk_level: str = ""
    predicted_outcome: str = ""  # "risky" or "safe"
    correct: bool = False
    classification: str = ""  # TP, TN, FP, FN

    # Details
    maintainer: str = ""
    reputation_score: int = 0
    reputation_tier: str = ""
    concentration: float = 0.0
    commits_last_year: int = 0
    protective_factors_total: int = 0

    error: Optional[str] = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["case"] = asdict(self.case)
        return result


@dataclass
class ValidationSummary:
    """Summary of all validation results."""

    total: int = 0
    correct: int = 0
    accuracy: float = 0.0

    # Confusion matrix
    true_positives: int = 0  # Predicted risky, was incident
    true_negatives: int = 0  # Predicted safe, was safe
    false_positives: int = 0  # Predicted risky, was safe
    false_negatives: int = 0  # Predicted safe, was incident

    # Metrics
    precision: float = 0.0  # TP / (TP + FP)
    recall: float = 0.0  # TP / (TP + FN)
    f1_score: float = 0.0

    # By attack type
    by_attack_type: dict = field(default_factory=dict)

    results: list[ValidationResult] = field(default_factory=list)


# =============================================================================
# Validation Dataset
# =============================================================================

VALIDATION_CASES = [
    # =========================================================================
    # POSITIVE CASES - Should score HIGH (>=60) or CRITICAL (>=80)
    # Includes: actual incidents AND packages with detectable governance risk
    # =========================================================================

    # --- GOVERNANCE FAILURES ---

    # Governance failure - abandoned package handed to attacker
    ValidationCase(
        name="event-stream",
        ecosystem="npm",
        expected_outcome="incident",
        attack_type="governance_failure",
        incident_date="2018-09-16",
        cutoff_date="2018-09-01",
        notes="Abandoned package, malicious maintainer gained access via social engineering",
    ),

    # NOTE: flatmap-stream removed - repository deleted, cannot analyze

    # --- MAINTAINER SABOTAGE ---
    # NOTE: Sabotage by active maintainers is HARD to detect with governance metrics.
    # colors detected due to single-maintainer + low activity. Others may be FN.

    # Maintainer sabotage - intentional disruption (detectable: single maintainer)
    ValidationCase(
        name="colors",
        ecosystem="npm",
        expected_outcome="incident",
        attack_type="maintainer_sabotage",
        incident_date="2022-01-08",
        cutoff_date="2022-01-01",
        notes="Marak sabotaged as protest. Detected due to governance weakness.",
    ),

    # Marak's other package - repo deleted, using community fork
    ValidationCase(
        name="faker",
        ecosystem="npm",
        expected_outcome="incident",
        attack_type="maintainer_sabotage",
        incident_date="2022-01-08",
        cutoff_date="2022-01-01",
        notes="EXPECTED FN: Community fork has good governance now - original incident not detectable.",
        repo_url="https://github.com/faker-js/faker",  # Community fork
    ),

    # node-ipc sabotage - active maintainer, hard to detect
    ValidationCase(
        name="node-ipc",
        ecosystem="npm",
        expected_outcome="incident",
        attack_type="maintainer_sabotage",
        incident_date="2022-03-15",
        cutoff_date="2022-03-01",
        notes="EXPECTED FN: Active maintainer sabotage - governance metrics won't catch active projects.",
    ),

    # --- ACCOUNT COMPROMISE ---
    # NOTE: Account compromise attacks are OUTSIDE our detection scope.
    # These are included to document limitations - we expect FALSE NEGATIVES here.

    # Account compromise - different attack vector (expected to miss)
    ValidationCase(
        name="ua-parser-js",
        ecosystem="npm",
        expected_outcome="incident",
        attack_type="account_compromise",
        incident_date="2021-10-22",
        cutoff_date="2021-10-01",
        notes="EXPECTED FN: Account compromise via email hijacking. Active project - governance metrics won't catch this.",
    ),

    # coa - account compromise (but also has governance issues we detect)
    ValidationCase(
        name="coa",
        ecosystem="npm",
        expected_outcome="incident",
        attack_type="account_compromise",
        incident_date="2021-11-04",
        cutoff_date="2021-11-01",
        notes="Account compromise, but scored HIGH due to underlying governance weakness.",
    ),

    # rc - account compromise (but also has governance issues we detect)
    ValidationCase(
        name="rc",
        ecosystem="npm",
        expected_outcome="incident",
        attack_type="account_compromise",
        incident_date="2021-11-04",
        cutoff_date="2021-11-01",
        notes="Account compromise, but scored HIGH due to underlying governance weakness.",
    ),

    # NOTE: ctx (pypi) removed - cannot find repository URL

    # =========================================================================
    # CONTROL CASES - Should score LOW (<=40) or VERY_LOW (<=20)
    # =========================================================================

    # --- NPM CONTROLS ---

    # High concentration but strong protective factors
    ValidationCase(
        name="chalk",
        ecosystem="npm",
        expected_outcome="safe",
        notes="High concentration but Sindre Sorhus has massive reputation and sponsors",
    ),

    # Popular, well-maintained
    ValidationCase(
        name="lodash",
        ecosystem="npm",
        expected_outcome="safe",
        notes="Very popular, historically high concentration but visible and maintained",
    ),

    # Org-owned, active
    ValidationCase(
        name="express",
        ecosystem="npm",
        expected_outcome="safe",
        notes="OpenJS Foundation, multiple maintainers, very active",
    ),

    # Very popular HTTP client
    ValidationCase(
        name="axios",
        ecosystem="npm",
        expected_outcome="safe",
        notes="Popular HTTP client, active development, multiple contributors",
    ),

    # CLI framework - well maintained
    ValidationCase(
        name="commander",
        ecosystem="npm",
        expected_outcome="safe",
        notes="Popular CLI framework, tj/commander.js, well maintained",
    ),

    # Debugging utility - very widely used
    ValidationCase(
        name="debug",
        ecosystem="npm",
        expected_outcome="safe",
        notes="Extremely popular debugging utility, part of many package trees",
    ),

    # Async utilities
    ValidationCase(
        name="async",
        ecosystem="npm",
        expected_outcome="safe",
        notes="Popular async utilities, caolan/async, well maintained",
    ),

    # Minimist - argument parsing - has real governance risk signals
    # High concentration, low activity. Had prototype pollution vulns (CVE-2020-7598, CVE-2021-44906).
    # We classify this as "governance_risk" - not an incident yet, but correctly flagged.
    ValidationCase(
        name="minimist",
        ecosystem="npm",
        expected_outcome="incident",
        attack_type="governance_risk",
        notes="Governance risk: 100% concentration, ~1 commit/yr, prototype pollution history. Correctly flagged.",
    ),

    # --- PYPI CONTROLS ---

    # Python - successful governance transition
    ValidationCase(
        name="requests",
        ecosystem="pypi",
        expected_outcome="safe",
        notes="Transitioned from Kenneth Reitz to PSF, successful community handoff",
    ),

    # Python - distributed governance
    ValidationCase(
        name="urllib3",
        ecosystem="pypi",
        expected_outcome="safe",
        notes="Org-owned, multiple maintainers, good governance",
    ),

    # Python - very active, foundation backed
    ValidationCase(
        name="django",
        ecosystem="pypi",
        expected_outcome="safe",
        notes="Django Software Foundation, many contributors, professional governance",
    ),

    # Flask - Pallets project, well governed
    ValidationCase(
        name="flask",
        ecosystem="pypi",
        expected_outcome="safe",
        notes="Pallets project, multiple maintainers, professional governance",
    ),

    # pytest - testing framework
    ValidationCase(
        name="pytest",
        ecosystem="pypi",
        expected_outcome="safe",
        notes="Very active, multiple maintainers, well governed",
    ),

    # NOTE: numpy removed - PyPI metadata doesn't expose GitHub repo URL reliably

    # click - CLI framework by Pallets
    ValidationCase(
        name="click",
        ecosystem="pypi",
        expected_outcome="safe",
        notes="Pallets project, same governance as Flask",
    ),

    # pydantic - data validation
    ValidationCase(
        name="pydantic",
        ecosystem="pypi",
        expected_outcome="safe",
        notes="Very active, funded development, growing community",
    ),
]


# =============================================================================
# Validation Logic
# =============================================================================

# Risk threshold for classifying as "risky"
RISK_THRESHOLD = 60  # HIGH or CRITICAL


async def validate_package(case: ValidationCase) -> ValidationResult:
    """Validate a single package."""
    result = ValidationResult(case=case)

    try:
        # Get package info
        if case.ecosystem == "npm":
            pkg_collector = NpmCollector()
            pkg_data = await pkg_collector.collect(case.name)
            await pkg_collector.close()
            repo_url = case.repo_url or pkg_data.repository_url
            weekly_downloads = pkg_data.weekly_downloads
        else:  # pypi
            pkg_collector = PyPICollector()
            pkg_data = await pkg_collector.collect(case.name)
            await pkg_collector.close()
            repo_url = case.repo_url or pkg_data.repository_url
            weekly_downloads = pkg_data.weekly_downloads

        if not repo_url:
            result.error = "Could not find repository URL"
            return result

        # Parse cutoff date
        cutoff = None
        if case.cutoff_date:
            cutoff = datetime.strptime(case.cutoff_date, "%Y-%m-%d")

        # Collect git data
        git_collector = GitCollector()
        git_metrics = await git_collector.collect(repo_url, cutoff)

        # Extract username from email if possible
        top_contributor_username = None
        if git_metrics.top_contributor_email:
            email = git_metrics.top_contributor_email
            if "noreply.github.com" in email:
                parts = email.split("@")[0]
                if "+" in parts:
                    top_contributor_username = parts.split("+")[1]
                else:
                    top_contributor_username = parts

        # Collect GitHub data
        github_collector = GitHubCollector()
        github_data = await github_collector.collect(
            repo_url,
            top_contributor_username=top_contributor_username,
            top_contributor_email=git_metrics.top_contributor_email,
        )
        await github_collector.close()

        # Parse account created date
        maintainer_account_created = None
        if github_data.maintainer_account_created:
            try:
                maintainer_account_created = datetime.fromisoformat(
                    github_data.maintainer_account_created.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        # Calculate reputation (using cutoff date for T-1 analysis)
        reputation_scorer = ReputationScorer()
        reputation = reputation_scorer.calculate(
            username=github_data.maintainer_username,
            account_created=maintainer_account_created,
            repos=github_data.maintainer_repos,
            sponsor_count=github_data.maintainer_sponsor_count,
            orgs=github_data.maintainer_orgs,
            packages_maintained=[case.name],
            ecosystem=case.ecosystem,
            as_of_date=cutoff,
        )

        # Run sentiment analysis
        sentiment_analyzer = SentimentAnalyzer()
        commit_sentiment = sentiment_analyzer.analyze_commits(
            [c.message for c in git_metrics.commits]
        )
        issue_sentiment = sentiment_analyzer.analyze_issues(
            [{"title": i.title, "body": i.body, "comments": i.comments}
             for i in github_data.issues]
        )

        total_frustration = commit_sentiment.frustration_count + issue_sentiment.frustration_count
        avg_sentiment = (commit_sentiment.average_compound + issue_sentiment.average_compound) / 2

        # Build metrics
        metrics = PackageMetrics(
            maintainer_concentration=git_metrics.maintainer_concentration,
            commits_last_year=git_metrics.commits_last_year,
            unique_contributors=git_metrics.unique_contributors,
            top_contributor_email=git_metrics.top_contributor_email,
            top_contributor_name=git_metrics.top_contributor_name,
            last_commit_date=git_metrics.last_commit_date,
            weekly_downloads=weekly_downloads,
            maintainer_username=github_data.maintainer_username,
            maintainer_public_repos=github_data.maintainer_public_repos,
            maintainer_total_stars=github_data.maintainer_total_stars,
            has_github_sponsors=github_data.has_github_sponsors,
            maintainer_account_created=maintainer_account_created,
            maintainer_repos=github_data.maintainer_repos,
            maintainer_sponsor_count=github_data.maintainer_sponsor_count,
            maintainer_orgs=github_data.maintainer_orgs,
            packages_maintained=[case.name],
            reputation=reputation,
            is_org_owned=github_data.is_org_owned,
            org_admin_count=github_data.org_admin_count,
            average_sentiment=avg_sentiment,
            frustration_detected=total_frustration > 0,
            frustration_evidence=commit_sentiment.frustration_evidence + issue_sentiment.frustration_evidence,
        )

        # Calculate score
        scorer = RiskScorer()
        breakdown = scorer.calculate(case.name, case.ecosystem, metrics, repo_url)

        # Populate result
        result.score = breakdown.final_score
        result.risk_level = breakdown.risk_level.value
        result.maintainer = github_data.maintainer_username
        result.reputation_score = reputation.total_score
        result.reputation_tier = reputation.tier.value
        result.concentration = breakdown.maintainer_concentration
        result.commits_last_year = breakdown.commits_last_year
        result.protective_factors_total = breakdown.protective_factors.total

        # Classify prediction
        result.predicted_outcome = "risky" if breakdown.final_score >= RISK_THRESHOLD else "safe"

        # Determine correctness
        if case.expected_outcome == "incident":
            result.correct = result.predicted_outcome == "risky"
            result.classification = "TP" if result.correct else "FN"
        else:  # expected safe
            result.correct = result.predicted_outcome == "safe"
            result.classification = "TN" if result.correct else "FP"

    except Exception as e:
        result.error = str(e)

    return result


def calculate_summary(results: list[ValidationResult]) -> ValidationSummary:
    """Calculate summary statistics from results."""
    summary = ValidationSummary()
    summary.results = results

    # Filter out errors
    valid_results = [r for r in results if r.error is None]
    summary.total = len(valid_results)

    if summary.total == 0:
        return summary

    # Count classifications
    for r in valid_results:
        if r.classification == "TP":
            summary.true_positives += 1
        elif r.classification == "TN":
            summary.true_negatives += 1
        elif r.classification == "FP":
            summary.false_positives += 1
        elif r.classification == "FN":
            summary.false_negatives += 1

        if r.correct:
            summary.correct += 1

        # Track by attack type
        attack_type = r.case.attack_type or "control"
        if attack_type not in summary.by_attack_type:
            summary.by_attack_type[attack_type] = {"total": 0, "correct": 0}
        summary.by_attack_type[attack_type]["total"] += 1
        if r.correct:
            summary.by_attack_type[attack_type]["correct"] += 1

    # Calculate metrics
    summary.accuracy = summary.correct / summary.total if summary.total > 0 else 0

    if summary.true_positives + summary.false_positives > 0:
        summary.precision = summary.true_positives / (summary.true_positives + summary.false_positives)

    if summary.true_positives + summary.false_negatives > 0:
        summary.recall = summary.true_positives / (summary.true_positives + summary.false_negatives)

    if summary.precision + summary.recall > 0:
        summary.f1_score = 2 * (summary.precision * summary.recall) / (summary.precision + summary.recall)

    return summary


def print_results(summary: ValidationSummary):
    """Print validation results in a formatted way."""
    print("\n" + "=" * 80)
    print("OSSUARY VALIDATION RESULTS")
    print("=" * 80)

    # Individual results
    print("\nPACKAGE RESULTS:")
    print("-" * 80)
    print(f"{'Package':<20} {'Expected':<10} {'Score':<8} {'Level':<12} {'Correct':<8} {'Class':<5}")
    print("-" * 80)

    for r in summary.results:
        if r.error:
            print(f"{r.case.name:<20} {'ERROR':<10} {'-':<8} {r.error[:30]}")
        else:
            correct_mark = "✓" if r.correct else "✗"
            print(
                f"{r.case.name:<20} "
                f"{r.case.expected_outcome:<10} "
                f"{r.score:<8} "
                f"{r.risk_level:<12} "
                f"{correct_mark:<8} "
                f"{r.classification:<5}"
            )

    # Summary statistics
    print("\n" + "=" * 80)
    print("SUMMARY STATISTICS")
    print("=" * 80)

    print(f"\nTotal packages tested: {summary.total}")
    print(f"Correct predictions:   {summary.correct}")
    print(f"Accuracy:              {summary.accuracy:.1%}")

    print("\nConfusion Matrix:")
    print(f"  True Positives (TP):  {summary.true_positives} - Predicted risky, was incident")
    print(f"  True Negatives (TN):  {summary.true_negatives} - Predicted safe, was safe")
    print(f"  False Positives (FP): {summary.false_positives} - Predicted risky, was safe")
    print(f"  False Negatives (FN): {summary.false_negatives} - Predicted safe, was incident")

    print(f"\nPrecision: {summary.precision:.1%} (of packages flagged risky, how many were incidents)")
    print(f"Recall:    {summary.recall:.1%} (of actual incidents, how many were flagged)")
    print(f"F1 Score:  {summary.f1_score:.2f}")

    # By attack type
    if summary.by_attack_type:
        print("\nBy Attack Type:")
        for attack_type, stats in summary.by_attack_type.items():
            pct = stats["correct"] / stats["total"] * 100 if stats["total"] > 0 else 0
            print(f"  {attack_type}: {stats['correct']}/{stats['total']} ({pct:.0f}%)")

    # Analysis
    print("\n" + "=" * 80)
    print("ANALYSIS")
    print("=" * 80)

    # False negatives are the most important - incidents we missed
    fn_cases = [r for r in summary.results if r.classification == "FN"]
    if fn_cases:
        print("\nFalse Negatives (missed incidents):")
        for r in fn_cases:
            print(f"  - {r.case.name}: Score {r.score} ({r.risk_level})")
            print(f"    Attack type: {r.case.attack_type}")
            print(f"    Notes: {r.case.notes}")

    # False positives
    fp_cases = [r for r in summary.results if r.classification == "FP"]
    if fp_cases:
        print("\nFalse Positives (safe packages flagged as risky):")
        for r in fp_cases:
            print(f"  - {r.case.name}: Score {r.score} ({r.risk_level})")

    print("\n" + "=" * 80)


async def main():
    parser = argparse.ArgumentParser(description="Validate ossuary risk scoring")
    parser.add_argument("--output", "-o", help="Output JSON file")
    parser.add_argument("--only", choices=["incidents", "controls"], help="Only run subset")
    parser.add_argument("--package", "-p", help="Only run specific package")
    args = parser.parse_args()

    # Check for GitHub token
    if not os.getenv("GITHUB_TOKEN"):
        print("Warning: GITHUB_TOKEN not set. Rate limits will be restrictive.")
        print("Set with: export GITHUB_TOKEN=$(gh auth token)")

    # Filter cases
    cases = VALIDATION_CASES
    if args.only == "incidents":
        cases = [c for c in cases if c.expected_outcome == "incident"]
    elif args.only == "controls":
        cases = [c for c in cases if c.expected_outcome == "safe"]

    if args.package:
        cases = [c for c in cases if c.name == args.package]

    if not cases:
        print("No matching packages found")
        return

    print(f"Validating {len(cases)} packages...")
    print()

    # Run validation
    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case.name} ({case.ecosystem})...", end=" ", flush=True)
        result = await validate_package(case)
        if result.error:
            print(f"ERROR: {result.error[:50]}")
        else:
            mark = "✓" if result.correct else "✗"
            print(f"{result.score} ({result.risk_level}) {mark}")
        results.append(result)

    # Calculate summary
    summary = calculate_summary(results)

    # Print results
    print_results(summary)

    # Save to file if requested
    if args.output:
        output_data = {
            "timestamp": datetime.now().isoformat(),
            "total": summary.total,
            "accuracy": summary.accuracy,
            "precision": summary.precision,
            "recall": summary.recall,
            "f1_score": summary.f1_score,
            "confusion_matrix": {
                "TP": summary.true_positives,
                "TN": summary.true_negatives,
                "FP": summary.false_positives,
                "FN": summary.false_negatives,
            },
            "by_attack_type": summary.by_attack_type,
            "results": [r.to_dict() for r in summary.results],
        }
        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2, default=str)
        print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
