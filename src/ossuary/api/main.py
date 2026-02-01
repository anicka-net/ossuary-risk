"""FastAPI application for ossuary."""

from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from ossuary import __version__
from ossuary.collectors.git import GitCollector
from ossuary.collectors.github import GitHubCollector
from ossuary.collectors.npm import NpmCollector
from ossuary.collectors.pypi import PyPICollector
from ossuary.scoring.engine import PackageMetrics, RiskScorer
from ossuary.scoring.factors import RiskLevel
from ossuary.sentiment.analyzer import SentimentAnalyzer

app = FastAPI(
    title="Ossuary",
    description="OSS Supply Chain Risk Scoring API - Where abandoned packages come to rest",
    version=__version__,
)


# Response models
class ScoreResponse(BaseModel):
    """Response model for score endpoint."""

    package: str
    ecosystem: str
    repo_url: Optional[str]
    score: int
    risk_level: str
    semaphore: str
    explanation: str
    breakdown: dict
    recommendations: list[str]


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return HealthResponse(status="healthy", version=__version__)


@app.get("/score/{ecosystem}/{package:path}", response_model=ScoreResponse)
async def get_score(
    ecosystem: str,
    package: str,
    repo_url: Optional[str] = Query(None, description="Repository URL (auto-detected if not provided)"),
    cutoff_date: Optional[str] = Query(None, description="Cutoff date for T-1 analysis (YYYY-MM-DD)"),
):
    """
    Calculate risk score for a package.

    Args:
        ecosystem: Package ecosystem (npm or pypi)
        package: Package name
        repo_url: Optional repository URL
        cutoff_date: Optional cutoff date for historical analysis
    """
    if ecosystem not in ("npm", "pypi"):
        raise HTTPException(status_code=400, detail=f"Unsupported ecosystem: {ecosystem}")

    cutoff = None
    if cutoff_date:
        try:
            cutoff = datetime.strptime(cutoff_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        # Get package info
        if ecosystem == "npm":
            pkg_collector = NpmCollector()
            pkg_data = await pkg_collector.collect(package)
            await pkg_collector.close()
            if not repo_url:
                repo_url = pkg_data.repository_url
            weekly_downloads = pkg_data.weekly_downloads
        else:  # pypi
            pkg_collector = PyPICollector()
            pkg_data = await pkg_collector.collect(package)
            await pkg_collector.close()
            if not repo_url:
                repo_url = pkg_data.repository_url
            weekly_downloads = pkg_data.weekly_downloads

        if not repo_url:
            raise HTTPException(
                status_code=400,
                detail="Could not find repository URL. Please provide with repo_url query parameter",
            )

        # Collect git data
        git_collector = GitCollector()
        git_metrics = await git_collector.collect(repo_url, cutoff)

        # Collect GitHub data
        github_collector = GitHubCollector()
        github_data = await github_collector.collect(repo_url)
        await github_collector.close()

        # Run sentiment analysis
        sentiment_analyzer = SentimentAnalyzer()
        commit_sentiment = sentiment_analyzer.analyze_commits([c.message for c in git_metrics.commits])
        issue_sentiment = sentiment_analyzer.analyze_issues(
            [{"title": i.title, "body": i.body, "comments": i.comments} for i in github_data.issues]
        )

        # Aggregate sentiment
        total_frustration = commit_sentiment.frustration_count + issue_sentiment.frustration_count
        avg_sentiment = (commit_sentiment.average_compound + issue_sentiment.average_compound) / 2

        # Build metrics
        metrics = PackageMetrics(
            maintainer_concentration=git_metrics.maintainer_concentration,
            commits_last_year=git_metrics.commits_last_year,
            unique_contributors=git_metrics.unique_contributors,
            top_contributor_email=git_metrics.top_contributor_email,
            last_commit_date=git_metrics.last_commit_date,
            weekly_downloads=weekly_downloads,
            maintainer_username=github_data.maintainer_username,
            maintainer_public_repos=github_data.maintainer_public_repos,
            maintainer_total_stars=github_data.maintainer_total_stars,
            has_github_sponsors=github_data.has_github_sponsors,
            is_org_owned=github_data.is_org_owned,
            org_admin_count=github_data.org_admin_count,
            average_sentiment=avg_sentiment,
            frustration_detected=total_frustration > 0,
            frustration_evidence=commit_sentiment.frustration_evidence + issue_sentiment.frustration_evidence,
        )

        # Calculate score
        scorer = RiskScorer()
        breakdown = scorer.calculate(package, ecosystem, metrics, repo_url)

        return ScoreResponse(
            package=package,
            ecosystem=ecosystem,
            repo_url=repo_url,
            score=breakdown.final_score,
            risk_level=breakdown.risk_level.value,
            semaphore=breakdown.risk_level.semaphore,
            explanation=breakdown.explanation,
            breakdown=breakdown.to_dict()["score"]["components"],
            recommendations=breakdown.recommendations,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    """Root endpoint with API info."""
    return {
        "name": "Ossuary",
        "description": "OSS Supply Chain Risk Scoring API",
        "version": __version__,
        "docs": "/docs",
        "endpoints": {
            "score": "/score/{ecosystem}/{package}",
            "health": "/health",
        },
    }
