"""FastAPI application for ossuary."""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from ossuary import __version__
from ossuary.db.session import init_db
from ossuary.scoring.factors import RiskLevel
from ossuary.services.scorer import ScoringResult, score_package


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield

app = FastAPI(
    title="Ossuary",
    description="OSS Supply Chain Risk Scoring API - Where abandoned packages come to rest",
    version=__version__,
    lifespan=lifespan,
)


# -- Response models --


class HealthResponse(BaseModel):
    status: str
    version: str


class CheckResponse(BaseModel):
    """Lightweight response for CI/CD pipelines.

    ``score`` is ``None`` when ``risk_level == "INSUFFICIENT_DATA"``: the
    methodology refuses to produce a number from partial input data.
    Reasons for that state are listed in ``incomplete_reasons``. CI/CD
    consumers should treat INSUFFICIENT_DATA as a separate gate from any
    numeric threshold rather than coercing it to 0 or 100.
    """

    package: str
    ecosystem: str
    score: Optional[int] = None
    risk_level: str
    semaphore: str
    incomplete_reasons: list[str] = []


class ScoreResponse(BaseModel):
    """Full scoring response with breakdown.

    ``score`` is ``None`` for INSUFFICIENT_DATA; see :class:`CheckResponse`.
    The full breakdown and ``incomplete_reasons`` are still returned so
    callers can render the failing inputs and decide their own retry
    policy.
    """

    package: str
    ecosystem: str
    repo_url: Optional[str] = None
    score: Optional[int] = None
    risk_level: str
    semaphore: str
    explanation: str
    breakdown: dict
    recommendations: list[str]
    warnings: list[str] = []
    incomplete_reasons: list[str] = []


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
        incomplete_reasons=result.breakdown.incomplete_reasons,
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
        breakdown=b.to_dict(),
        recommendations=b.recommendations,
        warnings=result.warnings,
        incomplete_reasons=b.incomplete_reasons,
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
        freshness_days=max_age if max_age > 0 else None,
    )

    if not result.success or not result.breakdown:
        raise HTTPException(status_code=422, detail=result.error or "Scoring failed")

    return result
