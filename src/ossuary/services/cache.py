"""Database caching layer for ossuary scores."""

import os
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ossuary.db.models import Package, Score


# Default freshness threshold: 7 days
CACHE_FRESHNESS_DAYS = int(os.getenv("OSSUARY_CACHE_DAYS", "7"))


class ScoreCache:
    """Manages cached score persistence and freshness."""

    def __init__(self, session: Session, freshness_days: int = CACHE_FRESHNESS_DAYS):
        self.session = session
        self.freshness_threshold = timedelta(days=freshness_days)

    def get_or_create_package(
        self, name: str, ecosystem: str, repo_url: Optional[str] = None
    ) -> Package:
        """Get existing package or create new one."""
        package = (
            self.session.query(Package)
            .filter(Package.name == name, Package.ecosystem == ecosystem)
            .first()
        )

        if package is None:
            package = Package(name=name, ecosystem=ecosystem, repo_url=repo_url)
            self.session.add(package)
            self.session.flush()  # Get the ID

        elif repo_url and not package.repo_url:
            package.repo_url = repo_url

        return package

    def is_fresh(self, package: Package) -> bool:
        """Check if package data is fresh (< threshold old)."""
        if package.last_analyzed is None:
            return False

        age = datetime.utcnow() - package.last_analyzed
        return age < self.freshness_threshold

    def get_current_score(self, package: Package) -> Optional[Score]:
        """Get most recent score for a package (no cutoff filter)."""
        return (
            self.session.query(Score)
            .filter(Score.package_id == package.id)
            .order_by(Score.calculated_at.desc())
            .first()
        )

    def get_historical_scores(
        self, package: Package, months: int = 24
    ) -> list[Score]:
        """Retrieve cached historical scores for a package.

        Returns scores ordered by cutoff_date descending (most recent first).
        """
        return (
            self.session.query(Score)
            .filter(Score.package_id == package.id)
            .order_by(Score.cutoff_date.desc())
            .limit(months)
            .all()
        )

    def store_score(
        self,
        package: Package,
        cutoff_date: datetime,
        final_score: int,
        risk_level: str,
        base_risk: int,
        activity_modifier: int,
        protective_factors_total: int,
        breakdown: dict,
        maintainer_concentration: float,
        commits_last_year: int,
        unique_contributors: int,
        weekly_downloads: int = 0,
        sentiment_modifier: int = 0,
    ) -> Score:
        """Store a calculated score in the database."""
        score = Score(
            package_id=package.id,
            calculated_at=datetime.utcnow(),
            cutoff_date=cutoff_date,
            final_score=final_score,
            risk_level=risk_level,
            base_risk=base_risk,
            activity_modifier=activity_modifier,
            protective_factors_total=protective_factors_total,
            sentiment_modifier=sentiment_modifier,
            breakdown=breakdown,
            maintainer_concentration=maintainer_concentration,
            commits_last_year=commits_last_year,
            unique_contributors=unique_contributors,
            weekly_downloads=weekly_downloads,
        )
        self.session.add(score)
        return score

    def mark_analyzed(self, package: Package) -> None:
        """Update package's last_analyzed timestamp."""
        package.last_analyzed = datetime.utcnow()

    def clear_historical_scores(self, package: Package) -> int:
        """Delete all historical scores for a package (before recalculation)."""
        count = (
            self.session.query(Score)
            .filter(Score.package_id == package.id)
            .delete()
        )
        return count
