"""Database caching layer for ossuary scores."""

import os
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ossuary._compat import utcnow_naive
from ossuary.db.models import Package, Score


# Default freshness threshold: 7 days
CACHE_FRESHNESS_DAYS = int(os.getenv("OSSUARY_CACHE_DAYS", "7"))


_PYPI_NORMALIZE_RE = re.compile(r"[-_.]+")


def normalize_package_name(name: str, ecosystem: str) -> str:
    """Return the canonical name used for DB lookup and storage.

    Currently normalises PyPI names per PEP 503 (lowercase, runs of ``_``,
    ``-`` and ``.`` collapsed to a single ``-``). Other ecosystems are
    pass-through pending evidence of similar duplication bugs — speculative
    normalisation is worse than no normalisation because it hides legitimate
    name distinctions (e.g. case-sensitive scoped npm packages).

    Reason this exists: ``get_or_create_package`` previously did a
    case-sensitive ``Package.name == name`` lookup, so the same logical
    PyPI package could end up in the DB under multiple capitalisations
    (``PyYAML`` vs ``pyyaml``) with separately-cached scores. PEP 503
    fixes the canonical form for PyPI; applying it at the cache chokepoint
    eliminates the duplication at both the lookup and the insert sides.
    """
    if ecosystem == "pypi":
        return _PYPI_NORMALIZE_RE.sub("-", name.strip().lower())
    return name


class ScoreCache:
    """Manages cached score persistence and freshness."""

    def __init__(self, session: Session, freshness_days: int = CACHE_FRESHNESS_DAYS):
        self.session = session
        self.freshness_threshold = timedelta(days=freshness_days)

    def get_package(self, name: str, ecosystem: str) -> Optional[Package]:
        """Look up a Package row without creating one.

        Used by cache-check paths so a lookup miss does not leak an empty
        Package row when the subsequent collection / scoring step then
        fails. Pre-creating the row leaves it stuck with
        ``last_analyzed=None`` and no Score, which the dashboard then
        surfaces as "N tracked / 0 scored". Persistence is the caller's
        job — call ``get_or_create_package`` only once you are about to
        write a Score row tied to it.
        """
        canonical = normalize_package_name(name, ecosystem)
        return (
            self.session.query(Package)
            .filter(Package.name == canonical, Package.ecosystem == ecosystem)
            .first()
        )

    def get_or_create_package(
        self, name: str, ecosystem: str, repo_url: Optional[str] = None
    ) -> Package:
        """Get existing package or create new one.

        ``name`` is normalised per :func:`normalize_package_name` before
        lookup and storage so that case / underscore variants of the same
        PyPI distribution resolve to the same row.

        Reserve this for write paths (about to attach a Score, snapshot,
        or negative-cache entry). For read-only cache checks call
        :meth:`get_package` instead — see its docstring.
        """
        canonical = normalize_package_name(name, ecosystem)
        package = (
            self.session.query(Package)
            .filter(Package.name == canonical, Package.ecosystem == ecosystem)
            .first()
        )

        if package is None:
            package = Package(name=canonical, ecosystem=ecosystem, repo_url=repo_url)
            self.session.add(package)
            self.session.flush()  # Get the ID

        elif repo_url and not package.repo_url:
            package.repo_url = repo_url

        return package

    def is_fresh(self, package: Package) -> bool:
        """Check if package data is fresh (< threshold old)."""
        if package.last_analyzed is None:
            return False

        age = utcnow_naive() - package.last_analyzed
        return age < self.freshness_threshold

    def get_score_for_cutoff(self, package: Package, cutoff_date: datetime) -> Optional[Score]:
        """Get cached score for an exact cutoff date."""
        return (
            self.session.query(Score)
            .filter(Score.package_id == package.id, Score.cutoff_date == cutoff_date)
            .order_by(Score.calculated_at.desc())
            .first()
        )

    def get_current_score(self, package: Package) -> Optional[Score]:
        """Get most recent current score for a package.

        Current scores use a live cutoff timestamp close to the calculation time.
        Historical month snapshots use older cutoff dates and must not satisfy
        current-cache lookups.
        """
        fresh_cutoff = utcnow_naive() - self.freshness_threshold
        return (
            self.session.query(Score)
            .filter(
                Score.package_id == package.id,
                Score.cutoff_date >= fresh_cutoff,
            )
            .order_by(Score.cutoff_date.desc(), Score.calculated_at.desc())
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
        final_score: Optional[int],
        risk_level: str,
        base_risk: Optional[int],
        activity_modifier: Optional[int],
        protective_factors_total: Optional[int],
        breakdown: dict,
        maintainer_concentration: Optional[float],
        commits_last_year: Optional[int],
        unique_contributors: Optional[int],
        weekly_downloads: Optional[int] = 0,
        sentiment_modifier: int = 0,
        is_provisional: bool = False,
    ) -> Score:
        """Store a calculated score in the database.

        Numeric columns are ``Optional`` to support
        ``risk_level == 'INSUFFICIENT_DATA'`` rows, where the score has
        not been computed and the components are unknown. The breakdown
        JSON carries the failure reasons under ``incomplete_reasons``.

        ``is_provisional=True`` flags rows where the score *was*
        computed but a non-essential signal failed (e.g. GitHub
        Sponsors lookup) — the score is conservative and should be
        retried via ``rescore-invalid``.
        """
        score = Score(
            package_id=package.id,
            calculated_at=utcnow_naive(),
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
            is_provisional=is_provisional,
        )
        self.session.add(score)
        return score

    def mark_analyzed(self, package: Package) -> None:
        """Update package's last_analyzed timestamp."""
        package.last_analyzed = utcnow_naive()

    def clear_scores_for_cutoffs(self, package: Package, cutoff_dates: list[datetime]) -> int:
        """Delete cached scores for a specific set of cutoff dates."""
        if not cutoff_dates:
            return 0
        count = (
            self.session.query(Score)
            .filter(Score.package_id == package.id, Score.cutoff_date.in_(cutoff_dates))
            .delete()
        )
        return count
