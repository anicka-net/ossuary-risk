"""Composite reputation scoring for maintainers."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ReputationTier(str, Enum):
    """Reputation tier classification."""

    TIER_1 = "TIER_1"  # Strong reputation, -25 risk points
    TIER_2 = "TIER_2"  # Established, -10 risk points
    UNKNOWN = "UNKNOWN"  # No reduction

    @classmethod
    def from_score(cls, score: int) -> "ReputationTier":
        """Get tier from reputation score."""
        if score >= 60:
            return cls.TIER_1
        elif score >= 30:
            return cls.TIER_2
        else:
            return cls.UNKNOWN

    @property
    def risk_reduction(self) -> int:
        """Get risk reduction points for this tier."""
        return {
            ReputationTier.TIER_1: -25,
            ReputationTier.TIER_2: -10,
            ReputationTier.UNKNOWN: 0,
        }[self]


# Recognized organizations that confer institutional backing
RECOGNIZED_ORGS = {
    # JavaScript/Node
    "nodejs",
    "openjs-foundation",
    "npm",
    "expressjs",
    "mochajs",
    "eslint",
    "webpack",
    "babel",
    "rollup",
    "vitejs",
    # Python
    "python",
    "psf",
    "pypa",
    "pallets",
    "django",
    "encode",
    "tiangolo",
    # General
    "apache",
    "cncf",
    "linux-foundation",
    "mozilla",
    "rust-lang",
    "golang",
    # Cloud/Infra
    "kubernetes",
    "docker",
    "hashicorp",
}

# Top packages by ecosystem (starter list, should be expanded)
TOP_PACKAGES = {
    "npm": {
        "lodash",
        "chalk",
        "express",
        "react",
        "vue",
        "axios",
        "moment",
        "webpack",
        "babel",
        "eslint",
        "typescript",
        "next",
        "prettier",
        "jest",
        "mocha",
        "commander",
        "debug",
        "async",
        "request",
        "underscore",
        "uuid",
        "minimist",
        "glob",
        "yargs",
        "semver",
        "fs-extra",
        "bluebird",
        "rxjs",
        "socket.io",
        "mongoose",
    },
    "pypi": {
        "requests",
        "numpy",
        "pandas",
        "django",
        "flask",
        "pytest",
        "boto3",
        "urllib3",
        "setuptools",
        "pip",
        "certifi",
        "pyyaml",
        "cryptography",
        "pillow",
        "sqlalchemy",
        "jinja2",
        "click",
        "scipy",
        "matplotlib",
        "tensorflow",
        "pytorch",
        "fastapi",
        "pydantic",
        "httpx",
        "aiohttp",
        "redis",
        "celery",
        "scrapy",
        "beautifulsoup4",
        "lxml",
    },
}


@dataclass
class ReputationBreakdown:
    """Detailed breakdown of reputation score."""

    username: str = ""

    # Individual signal scores
    tenure_score: int = 0  # +15 for >5 years
    portfolio_score: int = 0  # +15 for >50 original repos with stars
    stars_score: int = 0  # +15 for >50K total stars
    sponsors_score: int = 0  # +15 for sponsors with >=10 backers
    packages_score: int = 0  # +10 for >20 packages published
    top_package_score: int = 0  # +15 for maintaining top-1000 package
    org_membership_score: int = 0  # +15 for recognized org membership

    # Evidence for each signal
    account_age_years: float = 0.0
    original_repos_with_stars: int = 0
    total_stars: int = 0
    sponsor_count: Optional[int] = None
    packages_published: int = 0
    top_packages_maintained: list[str] = field(default_factory=list)
    recognized_orgs: list[str] = field(default_factory=list)

    @property
    def total_score(self) -> int:
        """Calculate total reputation score."""
        return (
            self.tenure_score
            + self.portfolio_score
            + self.stars_score
            + self.sponsors_score
            + self.packages_score
            + self.top_package_score
            + self.org_membership_score
        )

    @property
    def tier(self) -> ReputationTier:
        """Get reputation tier."""
        return ReputationTier.from_score(self.total_score)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "username": self.username,
            "total_score": self.total_score,
            "tier": self.tier.value,
            "risk_reduction": self.tier.risk_reduction,
            "signals": {
                "tenure": {
                    "score": self.tenure_score,
                    "years": self.account_age_years,
                },
                "portfolio": {
                    "score": self.portfolio_score,
                    "original_repos_with_stars": self.original_repos_with_stars,
                },
                "stars": {
                    "score": self.stars_score,
                    "total": self.total_stars,
                },
                "sponsors": {
                    "score": self.sponsors_score,
                    "count": self.sponsor_count,
                },
                "packages": {
                    "score": self.packages_score,
                    "count": self.packages_published,
                },
                "top_packages": {
                    "score": self.top_package_score,
                    "packages": self.top_packages_maintained,
                },
                "organizations": {
                    "score": self.org_membership_score,
                    "recognized": self.recognized_orgs,
                },
            },
        }


class ReputationScorer:
    """Calculate composite reputation score for maintainers."""

    # Thresholds
    TENURE_YEARS = 5
    MIN_REPOS_WITH_STARS = 50
    MIN_STARS_PER_REPO = 10
    TOTAL_STARS_THRESHOLD = 50_000
    MIN_SPONSORS = 10
    MIN_PACKAGES = 20

    def calculate(
        self,
        username: str,
        account_created: Optional[datetime],
        repos: list[dict],
        sponsor_count: Optional[int],
        orgs: list[str],
        packages_maintained: list[str],
        ecosystem: str = "npm",
        as_of_date: Optional[datetime] = None,
    ) -> ReputationBreakdown:
        """
        Calculate reputation score for a maintainer.

        Args:
            username: GitHub username
            account_created: Account creation date
            repos: List of repo dicts with 'fork', 'stargazers_count' keys
            sponsor_count: Number of sponsors (None if unknown)
            orgs: List of organization logins user belongs to
            packages_maintained: List of package names maintained
            ecosystem: Package ecosystem for top-package lookup
            as_of_date: Date to use as "now" for T-1 analysis (default: actual now)

        Returns:
            ReputationBreakdown with scores and evidence
        """
        breakdown = ReputationBreakdown(username=username)

        # Signal 1: Tenure (+15 for >5 years)
        if account_created:
            # Handle timezone-aware vs naive datetime comparison
            now = as_of_date or datetime.now()
            if account_created.tzinfo is not None and now.tzinfo is None:
                now = datetime.now(account_created.tzinfo)
            elif account_created.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            age_years = (now - account_created).days / 365.25
            breakdown.account_age_years = round(age_years, 1)
            if age_years >= self.TENURE_YEARS:
                breakdown.tenure_score = 15

        # Signal 2: Portfolio - original repos with stars (+15)
        original_repos_with_stars = 0
        total_stars = 0
        for repo in repos:
            if not repo.get("fork", False):
                stars = repo.get("stargazers_count", 0)
                total_stars += stars
                if stars >= self.MIN_STARS_PER_REPO:
                    original_repos_with_stars += 1

        breakdown.original_repos_with_stars = original_repos_with_stars
        breakdown.total_stars = total_stars

        if original_repos_with_stars >= self.MIN_REPOS_WITH_STARS:
            breakdown.portfolio_score = 15

        # Signal 3: Total stars (+15 for >50K)
        if total_stars >= self.TOTAL_STARS_THRESHOLD:
            breakdown.stars_score = 15

        # Signal 4: Sponsors (+15 for >=10 sponsors)
        breakdown.sponsor_count = sponsor_count
        if sponsor_count is not None and sponsor_count >= self.MIN_SPONSORS:
            breakdown.sponsors_score = 15

        # Signal 5: Packages published (+10 for >20)
        breakdown.packages_published = len(packages_maintained)
        if len(packages_maintained) >= self.MIN_PACKAGES:
            breakdown.packages_score = 10

        # Signal 6: Top package maintainer (+15)
        top_packages = TOP_PACKAGES.get(ecosystem, set())
        maintained_top = [p for p in packages_maintained if p.lower() in top_packages]
        breakdown.top_packages_maintained = maintained_top
        if maintained_top:
            breakdown.top_package_score = 15

        # Signal 7: Recognized org membership (+15)
        recognized = [org for org in orgs if org.lower() in RECOGNIZED_ORGS]
        breakdown.recognized_orgs = recognized
        if recognized:
            breakdown.org_membership_score = 15

        logger.info(
            f"Reputation for {username}: {breakdown.total_score} ({breakdown.tier.value}) - "
            f"tenure={breakdown.tenure_score}, portfolio={breakdown.portfolio_score}, "
            f"stars={breakdown.stars_score}, sponsors={breakdown.sponsors_score}"
        )

        return breakdown
