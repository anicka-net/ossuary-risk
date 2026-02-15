"""FastAPI application for ossuary."""

from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from ossuary import __version__
from ossuary.db.session import init_db
from ossuary.scoring.factors import RiskLevel
from ossuary.services.scorer import score_package, ScoringResult

app = FastAPI(
    title="Ossuary",
    description="OSS Supply Chain Risk Scoring API - Where abandoned packages come to rest",
    version=__version__,
)


@app.on_event("startup")
async def startup():
    init_db()


# -- Response models --


class HealthResponse(BaseModel):
    status: str
    version: str


class CheckResponse(BaseModel):
    """Lightweight response for CI/CD pipelines."""

    package: str
    ecosystem: str
    score: int
    risk_level: str
    semaphore: str


class ScoreResponse(BaseModel):
    """Full scoring response with breakdown."""

    package: str
    ecosystem: str
    repo_url: Optional[str] = None
    score: int
    risk_level: str
    semaphore: str
    explanation: str
    breakdown: dict
    recommendations: list[str]
    warnings: list[str] = []


# -- Endpoints --


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return HealthResponse(status="healthy", version=__version__)


@app.get("/check/{ecosystem}/{package:path}", response_model=CheckResponse)
async def check_package(
    ecosystem: str,
    package: str,
    repo_url: Optional[str] = Query(None, description="Repository URL override"),
    max_age: int = Query(7, description="Max cache age in days; 0 = force re-score"),
):
    """
    Quick risk check — returns score and semaphore only.

    Designed for CI/CD pipelines. Returns cached score if fresh,
    otherwise scores the package first.

    Example:
        GET /check/npm/lodash
        GET /check/github/containers/podman
        GET /check/pypi/requests?max_age=1
    """
    result = await _get_score(package, ecosystem, repo_url, max_age)

    return CheckResponse(
        package=package,
        ecosystem=ecosystem,
        score=result.breakdown.final_score,
        risk_level=result.breakdown.risk_level.value,
        semaphore=result.breakdown.risk_level.semaphore,
    )


@app.get("/score/{ecosystem}/{package:path}", response_model=ScoreResponse)
async def get_score(
    ecosystem: str,
    package: str,
    repo_url: Optional[str] = Query(None, description="Repository URL override"),
    max_age: int = Query(7, description="Max cache age in days; 0 = force re-score"),
):
    """
    Full risk score with breakdown, explanation, and recommendations.

    Supported ecosystems: npm, pypi, cargo, rubygems, packagist, nuget, go, github.
    For GitHub repos, use owner/repo as the package name.

    Example:
        GET /score/npm/lodash
        GET /score/github/containers/podman
        GET /score/pypi/requests?max_age=0
    """
    result = await _get_score(package, ecosystem, repo_url, max_age)
    b = result.breakdown

    return ScoreResponse(
        package=package,
        ecosystem=ecosystem,
        repo_url=b.repo_url,
        score=b.final_score,
        risk_level=b.risk_level.value,
        semaphore=b.risk_level.semaphore,
        explanation=b.explanation,
        breakdown=b.to_dict().get("score", {}).get("components", {}),
        recommendations=b.recommendations,
        warnings=result.warnings,
    )


@app.get("/")
async def root():
    """Root endpoint with API info."""
    return {
        "name": "Ossuary",
        "description": "OSS Supply Chain Risk Scoring API",
        "version": __version__,
        "docs": "/docs",
        "endpoints": {
            "check": "/check/{ecosystem}/{package}  — quick score + semaphore",
            "score": "/score/{ecosystem}/{package}  — full breakdown",
            "health": "/health",
        },
    }


# -- Internal --


async def _get_score(
    package: str, ecosystem: str, repo_url: Optional[str], max_age: int,
) -> ScoringResult:
    """Score a package, using cache when fresh enough."""
    use_cache = max_age > 0

    result = await score_package(
        package, ecosystem, repo_url=repo_url, use_cache=use_cache,
    )

    if not result.success or not result.breakdown:
        raise HTTPException(status_code=422, detail=result.error or "Scoring failed")

    return result
