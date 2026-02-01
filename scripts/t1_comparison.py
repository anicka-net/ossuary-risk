#!/usr/bin/env python3
"""
T-1 Analysis: Compare scores at incident time vs current state.

This validates the predictive power of the methodology by showing
what scores would have been BEFORE incidents occurred.

Limitations:
- Git metrics (concentration, activity) can be properly filtered by cutoff date
- GitHub metrics (stars, sponsors, repos) reflect CURRENT state, not historical
- Account tenure is calculated relative to cutoff date

Usage:
    python scripts/t1_comparison.py
"""

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ossuary.collectors.git import GitCollector
from ossuary.collectors.github import GitHubCollector
from ossuary.collectors.npm import NpmCollector
from ossuary.scoring.engine import PackageMetrics, RiskScorer
from ossuary.scoring.reputation import ReputationScorer
from ossuary.sentiment.analyzer import SentimentAnalyzer


@dataclass
class IncidentCase:
    """A known incident for T-1 analysis."""
    name: str
    ecosystem: str
    incident_date: str
    cutoff_date: str
    attack_type: str
    repo_url: Optional[str] = None


# Incident cases for T-1 comparison
INCIDENT_CASES = [
    IncidentCase(
        name="event-stream",
        ecosystem="npm",
        incident_date="2018-09-16",
        cutoff_date="2018-09-01",
        attack_type="governance_failure",
    ),
    IncidentCase(
        name="colors",
        ecosystem="npm",
        incident_date="2022-01-08",
        cutoff_date="2022-01-01",
        attack_type="maintainer_sabotage",
    ),
    IncidentCase(
        name="coa",
        ecosystem="npm",
        incident_date="2021-11-04",
        cutoff_date="2021-11-01",
        attack_type="account_compromise",
    ),
    IncidentCase(
        name="rc",
        ecosystem="npm",
        incident_date="2021-11-04",
        cutoff_date="2021-11-01",
        attack_type="account_compromise",
    ),
]


async def score_package(name: str, ecosystem: str, cutoff: Optional[datetime] = None) -> dict:
    """Score a package, optionally at a historical cutoff date."""

    # Get package info
    pkg_collector = NpmCollector()
    pkg_data = await pkg_collector.collect(name)
    await pkg_collector.close()
    repo_url = pkg_data.repository_url

    if not repo_url:
        return {"error": "No repository URL"}

    # Collect git data
    git_collector = GitCollector()
    git_metrics = await git_collector.collect(repo_url, cutoff)

    # Extract username from email
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

    # Calculate reputation
    reputation_scorer = ReputationScorer()
    reputation = reputation_scorer.calculate(
        username=github_data.maintainer_username,
        account_created=maintainer_account_created,
        repos=github_data.maintainer_repos,
        sponsor_count=github_data.maintainer_sponsor_count,
        orgs=github_data.maintainer_orgs,
        packages_maintained=[name],
        ecosystem=ecosystem,
        as_of_date=cutoff,
    )

    # Sentiment analysis
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
        weekly_downloads=pkg_data.weekly_downloads,
        maintainer_username=github_data.maintainer_username,
        maintainer_public_repos=github_data.maintainer_public_repos,
        maintainer_total_stars=github_data.maintainer_total_stars,
        has_github_sponsors=github_data.has_github_sponsors,
        maintainer_account_created=maintainer_account_created,
        maintainer_repos=github_data.maintainer_repos,
        maintainer_sponsor_count=github_data.maintainer_sponsor_count,
        maintainer_orgs=github_data.maintainer_orgs,
        packages_maintained=[name],
        reputation=reputation,
        is_org_owned=github_data.is_org_owned,
        org_admin_count=github_data.org_admin_count,
        average_sentiment=avg_sentiment,
        frustration_detected=total_frustration > 0,
        frustration_evidence=commit_sentiment.frustration_evidence + issue_sentiment.frustration_evidence,
    )

    # Calculate score
    scorer = RiskScorer()
    breakdown = scorer.calculate(name, ecosystem, metrics, repo_url)

    return {
        "score": breakdown.final_score,
        "risk_level": breakdown.risk_level.value,
        "concentration": breakdown.maintainer_concentration,
        "commits_year": breakdown.commits_last_year,
        "maintainer": github_data.maintainer_username,
        "reputation_score": reputation.total_score,
        "reputation_tier": reputation.tier.value,
        "tenure_years": reputation.account_age_years,
    }


async def main():
    if not os.getenv("GITHUB_TOKEN"):
        print("Warning: GITHUB_TOKEN not set. Set with: export GITHUB_TOKEN=$(gh auth token)")

    print("=" * 90)
    print("T-1 ANALYSIS: Comparing scores at incident time vs current state")
    print("=" * 90)
    print()
    print("LIMITATIONS:")
    print("  - Git metrics (concentration, commits/yr) are filtered by cutoff date")
    print("  - GitHub metrics (stars, sponsors) are CURRENT values, not historical")
    print("  - Account tenure is calculated relative to cutoff date")
    print()

    results = []

    for case in INCIDENT_CASES:
        print(f"Analyzing {case.name}...")

        cutoff = datetime.strptime(case.cutoff_date, "%Y-%m-%d")

        # Score at T-1 (before incident)
        print(f"  T-1 ({case.cutoff_date})...", end=" ", flush=True)
        t1_result = await score_package(case.name, case.ecosystem, cutoff)
        if "error" in t1_result:
            print(f"ERROR: {t1_result['error']}")
            continue
        print(f"Score: {t1_result['score']}")

        # Score at current time
        print(f"  Current...", end=" ", flush=True)
        current_result = await score_package(case.name, case.ecosystem, None)
        if "error" in current_result:
            print(f"ERROR: {current_result['error']}")
            continue
        print(f"Score: {current_result['score']}")

        results.append({
            "case": case,
            "t1": t1_result,
            "current": current_result,
        })
        print()

    # Summary table
    print("=" * 90)
    print("COMPARISON RESULTS")
    print("=" * 90)
    print()
    print(f"{'Package':<15} {'Attack Type':<20} {'T-1 Score':<12} {'Current':<12} {'Delta':<8} {'Detected?'}")
    print("-" * 90)

    for r in results:
        case = r["case"]
        t1 = r["t1"]
        current = r["current"]
        delta = current["score"] - t1["score"]
        detected = "YES" if t1["score"] >= 60 else "NO"

        print(
            f"{case.name:<15} "
            f"{case.attack_type:<20} "
            f"{t1['score']:<12} "
            f"{current['score']:<12} "
            f"{delta:+d}{'':4} "
            f"{detected}"
        )

    print()
    print("=" * 90)
    print("DETAILED BREAKDOWN")
    print("=" * 90)

    for r in results:
        case = r["case"]
        t1 = r["t1"]
        current = r["current"]

        print(f"\n{case.name} ({case.attack_type})")
        print(f"  Incident date: {case.incident_date}")
        print(f"  Cutoff date:   {case.cutoff_date}")
        print()
        print(f"  {'Metric':<25} {'T-1':>15} {'Current':>15}")
        print(f"  {'-'*55}")
        print(f"  {'Score':<25} {t1['score']:>15} {current['score']:>15}")
        print(f"  {'Risk Level':<25} {t1['risk_level']:>15} {current['risk_level']:>15}")
        print(f"  {'Concentration':<25} {t1['concentration']:>14.0f}% {current['concentration']:>14.0f}%")
        print(f"  {'Commits/year':<25} {t1['commits_year']:>15} {current['commits_year']:>15}")
        print(f"  {'Maintainer':<25} {t1['maintainer']:>15} {current['maintainer']:>15}")
        print(f"  {'Reputation Score':<25} {t1['reputation_score']:>15} {current['reputation_score']:>15}")
        print(f"  {'Reputation Tier':<25} {t1['reputation_tier']:>15} {current['reputation_tier']:>15}")
        print(f"  {'Account Age (years)':<25} {t1['tenure_years']:>15.1f} {current['tenure_years']:>15.1f}")

    print()
    print("=" * 90)

    # Summary
    detected_count = sum(1 for r in results if r["t1"]["score"] >= 60)
    print(f"\nT-1 Detection Rate: {detected_count}/{len(results)} ({detected_count/len(results)*100:.0f}%)")
    print("(Would have flagged these packages BEFORE the incident occurred)")


if __name__ == "__main__":
    asyncio.run(main())
