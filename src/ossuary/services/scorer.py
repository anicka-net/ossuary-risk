"""Reusable scoring functions for ossuary."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from dateutil.relativedelta import relativedelta

from ossuary.collectors.git import CommitData, GitCollector, GitMetrics
from ossuary.collectors.github import GitHubCollector, GitHubData
from ossuary.collectors.npm import NpmCollector
from ossuary.collectors.pypi import PyPICollector
from ossuary.collectors.registries import REGISTRY_COLLECTORS
from ossuary.db.session import session_scope
from ossuary.scoring.engine import PackageMetrics, RiskBreakdown, RiskScorer
from ossuary.scoring.factors import RiskLevel
from ossuary.scoring.reputation import ReputationScorer
from ossuary.sentiment.analyzer import SentimentAnalyzer
from ossuary.services.cache import ScoreCache


@dataclass
class CollectedData:
    """All collected data for a package (cached for historical calculations)."""

    repo_url: str
    all_commits: list[CommitData]
    github_data: GitHubData
    weekly_downloads: int
    maintainer_account_created: Optional[datetime]
    repo_stargazers: int = 0


@dataclass
class ScoringResult:
    """Result of a scoring operation."""

    success: bool
    breakdown: Optional[RiskBreakdown] = None
    error: Optional[str] = None
    warnings: list[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


@dataclass
class HistoricalScore:
    """A single historical score data point."""

    date: datetime
    score: int
    risk_level: str
    concentration: float
    commits_year: int
    contributors: int


async def collect_package_data(
    package_name: str,
    ecosystem: str,
    repo_url: Optional[str] = None,
) -> tuple[Optional[CollectedData], list[str]]:
    """
    Collect all data for a package (single pass).

    Returns tuple of (CollectedData or None, list of warnings).
    """
    warnings = []
    weekly_downloads = 0
    repo_stargazers = 0

    # 1. Get package registry info (or construct repo URL for github ecosystem)
    if ecosystem == "github":
        # Direct GitHub repo - package_name is owner/repo
        if not repo_url:
            name = package_name.strip("/")
            if not name.startswith("https://"):
                repo_url = f"https://github.com/{name}"
            else:
                repo_url = name
        # No download data for github-only; we'll use stars as proxy below
    elif ecosystem == "npm":
        pkg_collector = NpmCollector()
        try:
            pkg_data = await pkg_collector.collect(package_name)
            if not repo_url:
                repo_url = pkg_data.repository_url
            weekly_downloads = pkg_data.weekly_downloads
        finally:
            await pkg_collector.close()
    elif ecosystem == "pypi":
        pkg_collector = PyPICollector()
        try:
            pkg_data = await pkg_collector.collect(package_name)
            if not repo_url:
                repo_url = pkg_data.repository_url
            weekly_downloads = pkg_data.weekly_downloads
        finally:
            await pkg_collector.close()
    elif ecosystem in REGISTRY_COLLECTORS:
        collector_cls = REGISTRY_COLLECTORS[ecosystem]
        pkg_collector = collector_cls()
        try:
            pkg_data = await pkg_collector.collect(package_name)
            if not repo_url:
                repo_url = pkg_data.repository_url
            weekly_downloads = pkg_data.weekly_downloads
        finally:
            await pkg_collector.close()
    else:
        return None, [f"Unsupported ecosystem: {ecosystem}"]

    if not repo_url:
        return None, ["Could not find repository URL"]

    # 2. Collect ALL git commits (not filtered by date)
    git_collector = GitCollector()
    try:
        repo_path = git_collector.clone_or_update(repo_url)
        all_commits = git_collector.extract_commits(repo_path)
    except Exception as e:
        return None, [f"Failed to collect git data: {e}"]

    if not all_commits:
        return None, ["No commits found in repository"]

    # 3. Calculate current metrics to get top contributor
    current_metrics = git_collector.calculate_metrics(all_commits, datetime.now())

    # 4. Find top contributor's GitHub username
    top_contributor_username = None
    if current_metrics.top_contributor_email:
        email = current_metrics.top_contributor_email
        if "noreply.github.com" in email:
            parts = email.split("@")[0]
            if "+" in parts:
                top_contributor_username = parts.split("+")[1]
            else:
                top_contributor_username = parts

    # 5. Collect GitHub data
    github_collector = GitHubCollector()
    try:
        github_data = await github_collector.collect(
            repo_url,
            top_contributor_username=top_contributor_username,
            top_contributor_email=current_metrics.top_contributor_email,
        )
        # Get repo stargazers for visibility proxy
        owner, repo = GitHubCollector.parse_repo_url(repo_url)
        if owner and repo:
            repo_info = await github_collector.get_repo_info(owner, repo)
            if repo_info:
                repo_stargazers = repo_info.get("stargazers_count", 0)
    except Exception as e:
        warnings.append(f"GitHub data incomplete: {e}")
        # Create minimal github data for graceful degradation
        from ossuary.collectors.github import GitHubData
        github_data = GitHubData(
            maintainer_username="",
            maintainer_account_created=None,
            maintainer_public_repos=0,
            maintainer_total_stars=0,
            maintainer_repos=[],
            maintainer_sponsor_count=0,
            maintainer_orgs=[],
            has_github_sponsors=False,
            is_org_owned=False,
            org_admin_count=0,
            issues=[],
        )
    finally:
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

    return CollectedData(
        repo_url=repo_url,
        all_commits=all_commits,
        github_data=github_data,
        weekly_downloads=weekly_downloads,
        maintainer_account_created=maintainer_account_created,
        repo_stargazers=repo_stargazers,
    ), warnings


def calculate_score_for_date(
    package_name: str,
    ecosystem: str,
    collected_data: CollectedData,
    cutoff_date: datetime,
) -> RiskBreakdown:
    """
    Calculate risk score for a specific cutoff date using pre-collected data.
    """
    git_collector = GitCollector()

    # Filter commits up to cutoff date and calculate metrics
    filtered_commits = [c for c in collected_data.all_commits if c.authored_date <= cutoff_date]
    git_metrics = git_collector.calculate_metrics(filtered_commits, cutoff_date)

    github_data = collected_data.github_data

    # Calculate reputation
    reputation_scorer = ReputationScorer()
    reputation = reputation_scorer.calculate(
        username=github_data.maintainer_username,
        account_created=collected_data.maintainer_account_created,
        repos=github_data.maintainer_repos,
        sponsor_count=github_data.maintainer_sponsor_count,
        orgs=github_data.maintainer_orgs,
        packages_maintained=[package_name],
        ecosystem=ecosystem,
    )

    # Run sentiment analysis on commits up to cutoff
    sentiment_analyzer = SentimentAnalyzer()
    commit_sentiment = sentiment_analyzer.analyze_commits([c.message for c in git_metrics.commits])
    issue_sentiment = sentiment_analyzer.analyze_issues(
        [{"title": i.title, "body": i.body, "comments": i.comments} for i in github_data.issues]
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
        weekly_downloads=collected_data.weekly_downloads,
        repo_stargazers=collected_data.repo_stargazers,
        maintainer_username=github_data.maintainer_username,
        maintainer_public_repos=github_data.maintainer_public_repos,
        maintainer_total_stars=github_data.maintainer_total_stars,
        has_github_sponsors=github_data.has_github_sponsors,
        maintainer_account_created=collected_data.maintainer_account_created,
        maintainer_repos=github_data.maintainer_repos,
        maintainer_sponsor_count=github_data.maintainer_sponsor_count,
        maintainer_orgs=github_data.maintainer_orgs,
        packages_maintained=[package_name],
        reputation=reputation,
        is_org_owned=github_data.is_org_owned,
        org_admin_count=github_data.org_admin_count,
        # Maturity detection
        total_commits=git_metrics.total_commits,
        first_commit_date=git_metrics.first_commit_date,
        lifetime_contributors=git_metrics.lifetime_contributors,
        lifetime_concentration=git_metrics.lifetime_concentration,
        is_mature=git_metrics.is_mature,
        repo_age_years=git_metrics.repo_age_years,
        takeover_shift=git_metrics.takeover_shift,
        takeover_suspect=git_metrics.takeover_suspect,
        takeover_suspect_name=git_metrics.takeover_suspect_name,
        # Sentiment
        average_sentiment=avg_sentiment,
        frustration_detected=total_frustration > 0,
        frustration_evidence=commit_sentiment.frustration_evidence + issue_sentiment.frustration_evidence,
    )

    # Calculate score
    scorer = RiskScorer()
    return scorer.calculate(package_name, ecosystem, metrics, collected_data.repo_url)


def _rebuild_breakdown(cached_score, package_name: str, ecosystem: str) -> Optional[RiskBreakdown]:
    """Reconstruct a RiskBreakdown from cached Score data."""
    try:
        from ossuary.scoring.factors import ProtectiveFactors

        d = cached_score.breakdown
        pkg = d.get("package", {})
        metrics = d.get("metrics", {})
        score_data = d.get("score", {})
        components = score_data.get("components", {})
        pf = components.get("protective_factors", {})

        protective = ProtectiveFactors(
            reputation_score=pf.get("reputation", {}).get("score", 0),
            funding_score=pf.get("funding", {}).get("score", 0),
            org_score=pf.get("organization", {}).get("score", 0),
            visibility_score=pf.get("visibility", {}).get("score", 0),
            distributed_score=pf.get("distributed_governance", {}).get("score", 0),
            community_score=pf.get("community", {}).get("score", 0),
            cii_score=pf.get("cii_badge", {}).get("score", 0),
            frustration_score=pf.get("frustration", {}).get("score", 0),
            sentiment_score=pf.get("sentiment", {}).get("score", 0),
            maturity_score=pf.get("maturity", {}).get("score", 0),
            takeover_risk_score=pf.get("takeover_risk", {}).get("score", 0),
            reputation_evidence=pf.get("reputation", {}).get("evidence"),
            funding_evidence=pf.get("funding", {}).get("evidence"),
            frustration_evidence=pf.get("frustration", {}).get("evidence", []),
            sentiment_evidence=pf.get("sentiment", {}).get("evidence", []),
            maturity_evidence=pf.get("maturity", {}).get("evidence"),
            takeover_risk_evidence=pf.get("takeover_risk", {}).get("evidence"),
        )

        risk_level = RiskLevel(cached_score.risk_level)

        return RiskBreakdown(
            package_name=package_name,
            ecosystem=ecosystem,
            repo_url=pkg.get("repo_url"),
            maintainer_concentration=metrics.get("maintainer_concentration", cached_score.maintainer_concentration),
            commits_last_year=metrics.get("commits_last_year", cached_score.commits_last_year),
            unique_contributors=metrics.get("unique_contributors", cached_score.unique_contributors),
            weekly_downloads=metrics.get("weekly_downloads", cached_score.weekly_downloads),
            base_risk=cached_score.base_risk,
            activity_modifier=cached_score.activity_modifier,
            protective_factors=protective,
            final_score=cached_score.final_score,
            risk_level=risk_level,
            explanation=d.get("explanation", ""),
            recommendations=d.get("recommendations", []),
            data_sources=d.get("data_sources", {}),
            warnings=d.get("warnings", []),
        )
    except Exception:
        return None


async def score_package(
    package_name: str,
    ecosystem: str,
    repo_url: Optional[str] = None,
    cutoff_date: Optional[datetime] = None,
    use_cache: bool = True,
    force: bool = False,
) -> ScoringResult:
    """
    Score a single package.

    Args:
        package_name: Name of the package
        ecosystem: "npm" or "pypi"
        repo_url: Optional repository URL override
        cutoff_date: Optional cutoff date for T-1 analysis
        use_cache: Whether to use cached results
        force: Force re-scoring even if cache is fresh (still writes to cache)

    Returns:
        ScoringResult with breakdown or error
    """
    cutoff = cutoff_date or datetime.now()

    # Check cache (skip when force=True to ensure re-scoring)
    if use_cache and not force:
        with session_scope() as session:
            cache = ScoreCache(session)
            package = cache.get_or_create_package(package_name, ecosystem, repo_url)

            if cache.is_fresh(package):
                cached_score = cache.get_current_score(package)
                if cached_score and cached_score.breakdown:
                    breakdown = _rebuild_breakdown(cached_score, package_name, ecosystem)
                    if breakdown:
                        return ScoringResult(success=True, breakdown=breakdown)

    # Collect data
    collected_data, warnings = await collect_package_data(package_name, ecosystem, repo_url)
    if collected_data is None:
        return ScoringResult(success=False, error=warnings[0] if warnings else "Unknown error")

    # Calculate score
    try:
        breakdown = calculate_score_for_date(package_name, ecosystem, collected_data, cutoff)
    except Exception as e:
        return ScoringResult(success=False, error=str(e), warnings=warnings)

    # Store in cache
    if use_cache:
        with session_scope() as session:
            cache = ScoreCache(session)
            package = cache.get_or_create_package(
                package_name, ecosystem, collected_data.repo_url
            )
            cache.store_score(
                package=package,
                cutoff_date=cutoff,
                final_score=breakdown.final_score,
                risk_level=breakdown.risk_level.value,
                base_risk=breakdown.base_risk,
                activity_modifier=breakdown.activity_modifier,
                protective_factors_total=breakdown.protective_factors.total,
                breakdown=breakdown.to_dict(),
                maintainer_concentration=breakdown.maintainer_concentration,
                commits_last_year=breakdown.commits_last_year,
                unique_contributors=breakdown.unique_contributors,
                weekly_downloads=breakdown.weekly_downloads,
            )
            cache.mark_analyzed(package)

    return ScoringResult(success=True, breakdown=breakdown, warnings=warnings)


async def get_historical_scores(
    package_name: str,
    ecosystem: str,
    months: int = 24,
    repo_url: Optional[str] = None,
    use_cache: bool = True,
    progress_callback: Optional[callable] = None,
) -> tuple[list[HistoricalScore], list[str]]:
    """
    Calculate historical scores going back from current state.

    Args:
        package_name: Name of the package
        ecosystem: "npm" or "pypi"
        months: Number of months to go back (default 24)
        repo_url: Optional repository URL override
        use_cache: Whether to use/store cached results
        progress_callback: Optional callback(current, total) for progress updates

    Returns:
        Tuple of (list of HistoricalScore, list of warnings)
    """
    warnings = []

    # Check cache
    if use_cache:
        with session_scope() as session:
            cache = ScoreCache(session)
            package = cache.get_or_create_package(package_name, ecosystem, repo_url)

            if cache.is_fresh(package):
                cached_scores = cache.get_historical_scores(package, months)
                if len(cached_scores) >= months:
                    # Return cached historical data
                    return [
                        HistoricalScore(
                            date=s.cutoff_date,
                            score=s.final_score,
                            risk_level=s.risk_level,
                            concentration=s.maintainer_concentration,
                            commits_year=s.commits_last_year,
                            contributors=s.unique_contributors,
                        )
                        for s in sorted(cached_scores, key=lambda x: x.cutoff_date)
                    ], []

    # Collect all data once
    collected_data, collect_warnings = await collect_package_data(package_name, ecosystem, repo_url)
    warnings.extend(collect_warnings)

    if collected_data is None:
        return [], warnings

    # Determine reference date (last commit or now)
    if collected_data.all_commits:
        sorted_commits = sorted(collected_data.all_commits, key=lambda c: c.authored_date)
        reference_date = sorted_commits[-1].authored_date
    else:
        reference_date = datetime.now()

    # Generate monthly cutoff dates going backward
    cutoff_dates = []
    for i in range(months):
        cutoff = reference_date - relativedelta(months=i)
        # Normalize to first of month for consistency
        cutoff = cutoff.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        cutoff_dates.append(cutoff)

    # Sort chronologically (oldest first)
    cutoff_dates.sort()

    # Calculate score for each month
    historical_scores = []
    for i, cutoff in enumerate(cutoff_dates):
        if progress_callback:
            progress_callback(i + 1, len(cutoff_dates))

        try:
            breakdown = calculate_score_for_date(
                package_name, ecosystem, collected_data, cutoff
            )
            historical_scores.append(HistoricalScore(
                date=cutoff,
                score=breakdown.final_score,
                risk_level=breakdown.risk_level.value,
                concentration=breakdown.maintainer_concentration,
                commits_year=breakdown.commits_last_year,
                contributors=breakdown.unique_contributors,
            ))
        except Exception as e:
            warnings.append(f"Failed to calculate score for {cutoff.date()}: {e}")
            # Continue with other dates

    # Store in cache
    if use_cache and historical_scores:
        with session_scope() as session:
            cache = ScoreCache(session)
            package = cache.get_or_create_package(
                package_name, ecosystem, collected_data.repo_url
            )
            # Clear old historical data
            cache.clear_historical_scores(package)

            # Store new scores
            for hs in historical_scores:
                cache.store_score(
                    package=package,
                    cutoff_date=hs.date,
                    final_score=hs.score,
                    risk_level=hs.risk_level,
                    base_risk=0,  # Not stored in HistoricalScore
                    activity_modifier=0,
                    protective_factors_total=0,
                    breakdown={},
                    maintainer_concentration=hs.concentration,
                    commits_last_year=hs.commits_year,
                    unique_contributors=hs.contributors,
                    weekly_downloads=collected_data.weekly_downloads,
                )
            cache.mark_analyzed(package)

    return historical_scores, warnings
