"""Reusable scoring functions for ossuary."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from dateutil.relativedelta import relativedelta

from ossuary.collectors.git import CommitData, GitCollector, GitMetrics
from ossuary.collectors.github import GitHubCollector, GitHubData, IssueData
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
    """All collected data for a package (cached for historical calculations).

    ``fetch_errors`` is the data-completeness contract: any non-empty
    list signals that one or more *essential* upstream fetches failed in
    a *known* way (HTTP non-2xx, exception, malformed payload). The
    scoring engine treats a non-empty list as an instruction to
    short-circuit the score with ``RiskLevel.INSUFFICIENT_DATA`` rather
    than computing a number from partial data. Empty results (a package
    with zero sponsors, a project with zero recent commits) are *not*
    failures and do not populate this list — those are valid
    measurements of zero.

    ``provisional_reasons`` records *non-essential* failures that left
    the score computable but conservative (artificially higher). The
    canonical case is GitHub auxiliary endpoints (sponsors, orgs,
    issues, CII badge) returning a transient 4xx/5xx: the missing
    protective factor defaults to 0, raising the final score. The
    engine still produces a number and a risk_level, but flags the
    breakdown as ``is_provisional=True`` so the user can rescore once
    the upstream recovers.

    Both classes of failure produce a *higher* score than a complete
    run would (a missing protective factor contributes 0 instead of its
    negative bonus). The split is not about direction of bias — both
    are conservative — but about **signal magnitude** and what the
    missing input makes us blind to. Visibility (downloads) is the
    largest single protective factor (−10 to −20) and without it the
    engine cannot distinguish popular packages from obscure ones, so
    we refuse. Auxiliary GitHub signals are smaller (−10 to −15) and
    corroborating; missing one keeps the popularity assessment intact,
    so we publish the (conservative) score with the provisional flag.
    """

    repo_url: str
    all_commits: list[CommitData]
    github_data: GitHubData
    weekly_downloads: Optional[int]
    maintainer_account_created: Optional[datetime]
    repo_stargazers: int = 0
    fetch_errors: list[str] = field(default_factory=list)
    provisional_reasons: list[str] = field(default_factory=list)


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


@dataclass
class RegistryData:
    """Per-package registry-derived signals separated from repo data.

    Holds the cheap "stage 1" output that ``cached_collect`` uses to
    decide whether a cross-package repo-share lookup is worth doing.
    Specifically: ``repo_url`` is the resolved upstream the package
    points to (the cache-share key) and ``weekly_downloads`` is the
    per-package signal we'd overlay onto a shared snapshot. ``warnings``
    is the user-facing list (currently empty for registry; collectors
    surface those via ``fetch_errors``); kept for parity with
    ``collect_package_data``'s return shape.

    For ``ecosystem == "github"`` there is no registry call —
    ``weekly_downloads`` is set to ``None`` (sentinel: "not applicable
    on this path", distinct from "measured zero").
    """

    repo_url: Optional[str]
    weekly_downloads: Optional[int]
    fetch_errors: list[str]
    warnings: list[str]


async def _collect_registry_data(
    package_name: str,
    ecosystem: str,
    repo_url: Optional[str] = None,
) -> RegistryData:
    """Cheap "stage 1" of :func:`collect_package_data`.

    Fetches just the registry-derived signals: resolved ``repo_url``
    and ``weekly_downloads``. Used by ``cached_collect`` to learn the
    canonical repo URL before deciding whether a cross-package
    repo-share lookup can short-circuit the expensive GitHub fetch.

    On the github ecosystem path the URL is derived from the package
    name with no API call and ``weekly_downloads`` is ``None`` (the
    github-direct semantic doesn't use download data).

    Single source of truth: :func:`collect_package_data` calls this
    too (via its ``prefetched_registry`` parameter) so the per-eco
    registry handling lives in exactly one place.
    """
    fetch_errors: list[str] = []
    weekly_downloads: Optional[int] = 0  # default for non-github

    if ecosystem == "github":
        resolved = repo_url
        if not resolved:
            stripped = package_name.strip("/")
            resolved = (
                stripped if stripped.startswith("https://")
                else f"https://github.com/{stripped}"
            )
        return RegistryData(
            repo_url=resolved,
            weekly_downloads=None,  # no registry on this path
            fetch_errors=[],
            warnings=[],
        )

    if ecosystem == "npm":
        collector_cls = NpmCollector
    elif ecosystem == "pypi":
        collector_cls = PyPICollector
    elif ecosystem in REGISTRY_COLLECTORS:
        collector_cls = REGISTRY_COLLECTORS[ecosystem]
    else:
        return RegistryData(
            repo_url=None, weekly_downloads=None,
            fetch_errors=[], warnings=[f"Unsupported ecosystem: {ecosystem}"],
        )

    pkg_collector = collector_cls()
    try:
        pkg_data = await pkg_collector.collect(package_name)
        if not repo_url:
            repo_url = pkg_data.repository_url
        weekly_downloads = pkg_data.weekly_downloads
        fetch_errors = list(pkg_data.fetch_errors)
    finally:
        await pkg_collector.close()

    return RegistryData(
        repo_url=repo_url,
        weekly_downloads=weekly_downloads,
        fetch_errors=fetch_errors,
        warnings=[],
    )


async def cached_collect(
    package_name: str,
    ecosystem: str,
    repo_url: Optional[str] = None,
    cutoff_date: Optional[datetime] = None,
    use_cache: bool = True,
) -> tuple[Optional[CollectedData], list[str]]:
    """
    Wrapper around :func:`collect_package_data` that consults the snapshot cache.

    On a cache hit (most-recent snapshot has ``coverage_until >= cutoff_date``
    and matching ``fetcher_version``), the cached blob is deserialised back
    into a ``CollectedData`` instance and returned without any upstream
    fetches. On a miss, the underlying collector runs and the result is
    persisted before being returned.

    The collector schema is versioned via
    ``ossuary.services.repo_cache.COLLECTOR_VERSION`` — bumping that constant
    invalidates older snapshots automatically. **Methodology version bumps do
    not invalidate snapshots**: the formula reads the same raw data, so
    re-scoring after a methodology change costs DB read time, not API calls.

    Set ``use_cache=False`` to bypass the cache entirely (writes still happen
    on miss; this only forces the read miss). The wrapper preserves the
    ``(CollectedData | None, warnings)`` tuple shape of the underlying
    collector so callers can swap it in transparently.
    """
    from ossuary.services.repo_cache import (
        RepoSnapshotCache,
        canonicalize_repo_url,
        deserialise_collected_data,
        is_permanent_failure,
        serialise_collected_data,
    )

    def _hydrate(snap, *, zero_registry_fields: bool):
        """Deserialise a snapshot blob into CollectedData, optionally zeroing
        registry-derived fields for the github-ecosystem caller.

        Returns ``(data, ok)``: ``ok=False`` signals "blob unhydratable,
        caller should fall through" (logged at the call site)."""
        try:
            data = deserialise_collected_data(snap.blob, CollectedData)
        except (TypeError, KeyError, ValueError):
            return None, False
        if zero_registry_fields:
            # github-ecosystem semantics: no registry, so no download
            # signal. The snapshot may have been written by an
            # npm/pypi caller that recorded real download counts —
            # those would over-credit the visibility bonus here.
            data.weekly_downloads = 0
        return data, True

    if use_cache:
        with session_scope() as session:
            cache = RepoSnapshotCache(session)
            # Positive cache (snapshot hit, package-keyed).
            snapshot = cache.get_snapshot_for_cutoff(
                package_name, ecosystem, cutoff_date
            )
            if snapshot is not None:
                data, ok = _hydrate(snapshot, zero_registry_fields=False)
                if ok:
                    return data, []
                # Defensive fallthrough: blob failed to hydrate
                # (collector schema drift not caught by fetcher_version).
                # Log so we notice if this fires routinely.
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "Snapshot for %s/%s failed to deserialise; "
                    "falling through to fresh collect",
                    ecosystem, package_name,
                )

            # Repo-keyed cross-package lookup — github narrow path
            # (no API call needed). The non-github cross-package /
            # cross-ecosystem path lives outside this session block
            # because it needs to await ``_collect_registry_data``
            # (a network call) before deciding whether to look up.
            if ecosystem == "github" and snapshot is None:
                derived_url = repo_url
                if not derived_url:
                    name = package_name.strip("/")
                    derived_url = (
                        name if name.startswith("https://")
                        else f"https://github.com/{name}"
                    )
                shared = cache.get_snapshot_by_repo_url(
                    derived_url, cutoff_date
                )
                if shared is not None:
                    data, ok = _hydrate(shared, zero_registry_fields=True)
                    if ok:
                        # Persist a snapshot row keyed to *this* package
                        # so subsequent same-package lookups hit the
                        # cheap package-keyed path.
                        try:
                            cache.store_snapshot(
                                name=package_name,
                                ecosystem=ecosystem,
                                repo_url=derived_url,
                                blob=serialise_collected_data(data),
                            )
                        except Exception as exc:  # noqa: BLE001
                            import logging as _logging
                            _logging.getLogger(__name__).warning(
                                "Failed to persist shared-repo snapshot "
                                "for %s/%s: %s",
                                ecosystem, package_name, exc,
                            )
                        return data, []

            # Negative cache: skip the upstream probe entirely if a recent
            # permanent failure is recorded (404, no repo URL, etc.). The
            # TTL-based expiry in get_negative_cache handles re-probes.
            cached_failure = cache.get_negative_cache(package_name, ecosystem)
            if cached_failure is not None:
                return None, [f"(cached) {cached_failure}"]

    # Cross-package / cross-ecosystem repo-share lookup (v0.10.1 step 1b).
    #
    # If a snapshot for *any* package mapping to the same canonical
    # repo URL exists, we can short-circuit the expensive GitHub fetch
    # and just overlay this caller's per-package registry data. The
    # cheap registry probe (one HTTP call) gives us the canonical URL.
    #
    # Only fires for non-github ecosystems — for github the same
    # short-circuit ran inside the session block above with zero API
    # calls. For npm/pypi/etc. we do pay the cheap registry probe;
    # the win is that we save the O(10–100)-call GitHub fetch on a
    # repo-share hit. On a miss we pass the prefetched registry
    # through to ``collect_package_data`` so the registry call isn't
    # duplicated.
    prefetched_registry: Optional[RegistryData] = None
    if use_cache and ecosystem != "github":
        prefetched_registry = await _collect_registry_data(
            package_name, ecosystem, repo_url
        )
        # Gate: only attempt the share if registry probe was clean.
        # A failed probe (rate limit, no-repo-URL, etc.) needs to flow
        # through the full collect path so the standard
        # INSUFFICIENT_DATA / negative-cache machinery fires correctly.
        if (
            prefetched_registry.repo_url
            and not prefetched_registry.fetch_errors
            and not prefetched_registry.warnings
        ):
            with session_scope() as session:
                cache = RepoSnapshotCache(session)
                shared = cache.get_snapshot_by_repo_url(
                    prefetched_registry.repo_url, cutoff_date
                )
                if shared is not None:
                    try:
                        data = deserialise_collected_data(
                            shared.blob, CollectedData
                        )
                    except (TypeError, KeyError, ValueError):
                        data = None
                    if data is not None:
                        # Overlay this caller's per-package registry data
                        # onto the shared repo-derived blob.
                        # ``weekly_downloads`` is the only per-package
                        # field overlaid here; commit history and
                        # GitHub data come from the shared snapshot.
                        # ``repo_url`` is set to this caller's resolved
                        # value (canonicalises to the same thing as the
                        # source's, but using the caller's spelling
                        # keeps diagnostics aligned).
                        data.weekly_downloads = (
                            prefetched_registry.weekly_downloads or 0
                        )
                        data.repo_url = prefetched_registry.repo_url
                        # Persist a snapshot keyed to this package so
                        # subsequent same-package lookups hit the cheap
                        # package-keyed path (and so this package gets
                        # its own row with its own per-package registry
                        # state, distinct from the source package).
                        try:
                            cache.store_snapshot(
                                name=package_name,
                                ecosystem=ecosystem,
                                repo_url=prefetched_registry.repo_url,
                                blob=serialise_collected_data(data),
                            )
                            cache.clear_negative(package_name, ecosystem)
                        except Exception as exc:  # noqa: BLE001
                            import logging as _logging
                            _logging.getLogger(__name__).warning(
                                "Failed to persist shared-repo snapshot "
                                "(non-github) for %s/%s: %s",
                                ecosystem, package_name, exc,
                            )
                        return data, []

    # Freshness-probe wiring intentionally NOT enabled here.
    #
    # An earlier v0.10.1 slice (commit e9af445) wired a single
    # ``GET /repos/{owner}/{repo}`` to compare upstream ``pushed_at``
    # against the snapshot and extend freshness on a match. GPT review
    # caught that ``pushed_at`` only moves on git pushes, but the
    # cached blob also carries maintainer sponsors, org membership,
    # org-admin status, CII badge state, and issue/comment data — none
    # of which are guaranteed to move with ``pushed_at``. A repo with
    # no code pushes but new burnout issues, changed org ownership, or
    # newly enabled sponsors would have its stale snapshot freshness
    # extended on a false signal. For an academic-methodology repo
    # that is too strong a reuse criterion.
    #
    # The building blocks (``GitHubData.pushed_at``,
    # ``RepoSnapshot.upstream_pushed_at``,
    # ``RepoSnapshotCache.get_latest_snapshot_any_age``,
    # ``RepoSnapshotCache.extend_snapshot_freshness``,
    # ``GitHubCollector.probe_pushed_at``) are kept on disk because the
    # right reactivation needs signal-family-aware refresh
    # (``docs/data_reuse_design.md`` GPT roadmap item 2): on a probe
    # match, refresh only the cheap auxiliary signals (sponsors, orgs,
    # CII, issues) while reusing the cached commit history. That makes
    # the probe path sound. Until item 2 lands the probe stays cold.

    data, warnings = await collect_package_data(
        package_name, ecosystem, repo_url,
        prefetched_registry=prefetched_registry,
    )
    if data is not None:
        try:
            with session_scope() as session:
                cache = RepoSnapshotCache(session)
                cache.store_snapshot(
                    name=package_name,
                    ecosystem=ecosystem,
                    repo_url=data.repo_url,
                    blob=serialise_collected_data(data),
                )
                # Recovered from a prior negative-cache state — clear it
                # so the package isn't re-flagged on the next read.
                cache.clear_negative(package_name, ecosystem)
        except Exception as exc:  # noqa: BLE001
            # Snapshot persistence is best-effort; never let a cache write
            # failure block the score path. Log so we can investigate.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Failed to persist snapshot for %s/%s: %s",
                ecosystem, package_name, exc,
            )
    elif use_cache and warnings and is_permanent_failure(warnings[0]):
        # Permanent failure — record it so we don't re-probe on every run.
        # Transient failures (rate limit, 5xx, INSUFFICIENT_DATA) flow
        # through the standard retry path instead.
        try:
            with session_scope() as session:
                cache = RepoSnapshotCache(session)
                cache.store_negative(package_name, ecosystem, warnings[0])
        except Exception as exc:  # noqa: BLE001
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Failed to persist negative cache for %s/%s: %s",
                ecosystem, package_name, exc,
            )
    return data, warnings


async def collect_package_data(
    package_name: str,
    ecosystem: str,
    repo_url: Optional[str] = None,
    prefetched_registry: Optional[RegistryData] = None,
) -> tuple[Optional[CollectedData], list[str]]:
    """
    Collect all data for a package (single pass).

    Returns tuple of (CollectedData or None, list of warnings).

    ``prefetched_registry``: when ``cached_collect`` has already done
    the cheap registry probe to look up the canonical repo URL, it
    passes the result here so we don't redo the registry call. The
    cache-hit path uses pre-fetched registry data to overlay onto a
    shared snapshot; the cache-miss path passes it through to avoid
    duplicate calls. ``None`` means "no prefetch — collect now".
    """
    warnings: list[str] = []
    repo_stargazers = 0

    # 1. Resolve repo URL + per-package registry signals (or skip the
    #    registry call if the caller pre-fetched it).
    registry = prefetched_registry or await _collect_registry_data(
        package_name, ecosystem, repo_url
    )
    if registry.warnings:
        return None, registry.warnings
    repo_url = registry.repo_url
    # weekly_downloads None on the github path means "not measured here";
    # convert to 0 for the dataclass which expects Optional[int] but
    # uses 0 for "no registry call". Keep None semantics out of the blob.
    weekly_downloads: Optional[int] = (
        registry.weekly_downloads if registry.weekly_downloads is not None else 0
    )
    fetch_errors: list[str] = list(registry.fetch_errors)

    if not repo_url:
        return None, [f"Package '{package_name}' not found on {ecosystem} (no repository URL)"]

    # 2. Collect ALL git commits (not filtered by date)
    git_collector = GitCollector()
    try:
        repo_path = git_collector.clone_or_update(repo_url)
        all_commits = git_collector.extract_commits(repo_path)
    except Exception as e:
        err_str = str(e)
        if "not found" in err_str.lower() or "exit code(128)" in err_str:
            return None, [f"Repository not found: {repo_url}"]
        return None, [f"Failed to collect git data from {repo_url}: {e}"]

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
        # Pull through the per-call classification recorded inside
        # GitHubCollector.collect (essential vs non-essential).
        fetch_errors.extend(github_data.fetch_errors)
        # Get repo stargazers for visibility proxy. This is the only
        # other call after collect() — treat its failure as provisional
        # since stars are only used as a fallback when downloads = 0.
        owner, repo = GitHubCollector.parse_repo_url(repo_url)
        if owner and repo:
            repo_info = await github_collector.get_repo_info(owner, repo)
            if repo_info:
                repo_stargazers = repo_info.get("stargazers_count", 0)
                # Capture pushed_at for the snapshot-cache freshness
                # probe (v0.10.1 phase 3 step 3). If unchanged at
                # next refresh, we can validate the cached blob with
                # one API call instead of a full re-collect.
                github_data.pushed_at = repo_info.get("pushed_at", "") or ""
            elif github_collector.last_error:
                github_data.provisional_reasons.append(
                    f"github.repo_stargazers: {github_collector.last_error}"
                )
    except Exception as e:
        warnings.append(f"GitHub data incomplete: {e}")
        # Create minimal github data for graceful degradation. The bare
        # exception path is now uncommon — most failures are caught and
        # classified inside GitHubCollector.collect — but we keep it as
        # a defensive fallback. Any failure that lands here is treated
        # as provisional rather than INSUFFICIENT_DATA, matching the
        # missing-protective-factor → conservative-score rule.
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
            provisional_reasons=[f"github.collect: unhandled exception ({e})"],
        )
    finally:
        await github_collector.close()

    # Surface GitHub's non-essential failures as provisional reasons on
    # the resulting CollectedData (kept separate from the essential
    # `fetch_errors` list).
    provisional_reasons = list(github_data.provisional_reasons)

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
        fetch_errors=fetch_errors,
        provisional_reasons=provisional_reasons,
    ), warnings


def _filter_issues_for_cutoff(issues: list[IssueData], cutoff_date: datetime) -> list[dict]:
    """Drop issue content that post-dates the requested cutoff.

    GitHub issue metadata is fetched from the current API snapshot, so historical
    scoring must exclude issues and comments created after the cutoff to avoid
    leaking future frustration/sentiment signals into T-1 analyses.
    """
    cutoff_naive = cutoff_date.replace(tzinfo=None)
    filtered = []

    def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    for issue in issues:
        issue_created = parse_timestamp(issue.created_at)
        if issue_created and issue_created > cutoff_naive:
            continue

        comments = []
        for comment in issue.comments:
            if not isinstance(comment, dict):
                continue
            comment_created = parse_timestamp(comment.get("created_at"))
            if comment_created and comment_created > cutoff_naive:
                continue
            comments.append(comment)

        filtered.append({
            "title": issue.title,
            "body": issue.body,
            "author_login": issue.author_login,
            "comments": comments,
        })

    return filtered


def calculate_score_for_date(
    package_name: str,
    ecosystem: str,
    collected_data: CollectedData,
    cutoff_date: datetime,
) -> RiskBreakdown:
    """
    Calculate risk score for a specific cutoff date using pre-collected data.

    Honours the data-completeness contract: if ``collected_data.fetch_errors``
    is non-empty, no numeric score is computed. The result is a
    ``RiskBreakdown`` with ``risk_level == INSUFFICIENT_DATA``,
    ``final_score = None``, and the failure list copied to
    ``incomplete_reasons``. Use ``ossuary rescore-invalid`` to retry.
    """
    if collected_data.fetch_errors:
        return RiskBreakdown(
            package_name=package_name,
            ecosystem=ecosystem,
            repo_url=collected_data.repo_url,
            final_score=None,
            risk_level=RiskLevel.INSUFFICIENT_DATA,
            incomplete_reasons=list(collected_data.fetch_errors),
            explanation=(
                "Score not computed: " + "; ".join(collected_data.fetch_errors)
            ),
            recommendations=[
                "Retry later — the failing upstream is most likely transient.",
                "Run `ossuary rescore-invalid` to retry all packages in this state.",
            ],
        )

    # Capture provisional reasons so they propagate to the final
    # breakdown even though the score is computed normally below.
    provisional_reasons = list(collected_data.provisional_reasons)

    git_collector = GitCollector()

    # Filter commits up to cutoff date and calculate metrics
    filtered_commits = [c for c in collected_data.all_commits if c.authored_date <= cutoff_date]
    git_metrics = git_collector.calculate_metrics(filtered_commits, cutoff_date)

    github_data = collected_data.github_data
    # A scoring run is "historical" when the cutoff is meaningfully in the past
    # (more than 1 day ago), not merely a few seconds behind datetime.now().
    is_historical = (datetime.now() - cutoff_date).days > 1

    # For historical scoring, reconstruct what's verifiable at the cutoff date:
    # - Repos: filter to those created before cutoff (created_at available via API)
    # - Stars: sum from repos that existed at cutoff (conservative upper bound)
    # - Tenure: compute age at cutoff, not now (via as_of_date param)
    # - Sponsors: cannot reconstruct, set to 0
    # - Orgs: stable over time for recognized foundations, pass through as-is
    # - Org ownership: stable property, pass through as-is
    if is_historical:
        cutoff_iso = cutoff_date.isoformat()
        historical_repos = [
            r for r in github_data.maintainer_repos
            if r.get("created_at", "9999") <= cutoff_iso
        ]
        historical_sponsor_count = 0  # Cannot reconstruct
        historical_repo_stargazers = 0
    else:
        historical_repos = github_data.maintainer_repos
        historical_sponsor_count = github_data.maintainer_sponsor_count
        historical_repo_stargazers = collected_data.repo_stargazers

    factor_availability = {
        "reputation": "historical_reconstruction" if is_historical else "current_observed",
        "funding": (
            "unavailable_historical_neutralized"
            if is_historical else "current_observed"
        ),
        "visibility": "missing",
        "issue_sentiment": "missing",
    }
    warnings: list[str] = []

    if (collected_data.weekly_downloads or 0) > 0:
        factor_availability["visibility"] = "registry_downloads"
    elif is_historical:
        factor_availability["visibility"] = "unavailable_historical_neutralized"
        if collected_data.repo_stargazers > 0:
            warnings.append(
                "Historical scoring disables GitHub-star visibility proxy to avoid leaking present-day popularity into past scores."
            )
    elif collected_data.repo_stargazers > 0:
        factor_availability["visibility"] = "current_repo_stars_proxy"

    use_issue_sentiment = not is_historical
    if github_data.issues:
        if use_issue_sentiment:
            factor_availability["issue_sentiment"] = "current_snapshot_sample"
        else:
            factor_availability["issue_sentiment"] = "disabled_historical_partial_snapshot"
            warnings.append(
                "Historical scoring disables issue/comment sentiment because the GitHub issue snapshot is current and incomplete."
            )

    # Calculate reputation
    reputation_scorer = ReputationScorer()
    reputation = reputation_scorer.calculate(
        username=github_data.maintainer_username,
        account_created=collected_data.maintainer_account_created,
        repos=historical_repos,
        sponsor_count=historical_sponsor_count,
        orgs=github_data.maintainer_orgs,  # Org membership is stable over time
        packages_maintained=[package_name],
        ecosystem=ecosystem,
        as_of_date=cutoff_date if is_historical else None,
    )

    # Run sentiment analysis on commits up to cutoff. Restrict
    # frustration detection in issues to maintainer-authored text so
    # noisy user comments don't spuriously fire the +20 risk factor;
    # commits already imply maintainer authorship. See
    # ``ossuary.sentiment.analyzer`` module docstring for the v6.2
    # author-attribution design.
    sentiment_analyzer = SentimentAnalyzer()
    commit_sentiment = sentiment_analyzer.analyze_commits([c.message for c in git_metrics.commits])
    maintainer_logins = (
        {github_data.maintainer_username}
        if github_data.maintainer_username
        else None
    )
    if use_issue_sentiment:
        issue_sentiment = sentiment_analyzer.analyze_issues(
            _filter_issues_for_cutoff(github_data.issues, cutoff_date),
            maintainer_logins=maintainer_logins,
        )
    else:
        issue_sentiment = sentiment_analyzer.analyze_issues([])

    total_frustration = commit_sentiment.frustration_count + issue_sentiment.frustration_count
    total_sentiment_texts = (
        commit_sentiment.total_analyzed + issue_sentiment.total_analyzed
    )
    if total_sentiment_texts > 0:
        avg_sentiment = (
            (commit_sentiment.average_compound * commit_sentiment.total_analyzed)
            + (issue_sentiment.average_compound * issue_sentiment.total_analyzed)
        ) / total_sentiment_texts
    else:
        avg_sentiment = 0.0

    # Build metrics
    metrics = PackageMetrics(
        maintainer_concentration=git_metrics.maintainer_concentration,
        commits_last_year=git_metrics.commits_last_year,
        unique_contributors=git_metrics.unique_contributors,
        top_contributor_email=git_metrics.top_contributor_email,
        top_contributor_name=git_metrics.top_contributor_name,
        last_commit_date=git_metrics.last_commit_date,
        # weekly_downloads is Optional[int] in CollectedData (None on fetch
        # failure). Coerce to 0 here so the engine's bucket comparisons stay
        # type-safe; the short-circuit above ensures we only reach this path
        # when fetch_errors was empty (i.e. the value really is an int or 0).
        weekly_downloads=collected_data.weekly_downloads or 0,
        repo_stargazers=historical_repo_stargazers,
        maintainer_username=github_data.maintainer_username,
        maintainer_public_repos=github_data.maintainer_public_repos,
        maintainer_total_stars=github_data.maintainer_total_stars,
        has_github_sponsors=False if is_historical else github_data.has_github_sponsors,
        maintainer_account_created=collected_data.maintainer_account_created,
        maintainer_repos=historical_repos,
        maintainer_sponsor_count=historical_sponsor_count,
        maintainer_orgs=github_data.maintainer_orgs,  # Stable over time
        packages_maintained=[package_name],
        reputation=reputation,
        cii_badge_level=github_data.cii_badge_level,
        is_org_owned=github_data.is_org_owned,  # Stable property
        org_admin_count=github_data.org_admin_count if not is_historical else max(1, github_data.org_admin_count),
        # Maturity detection
        total_commits=git_metrics.total_commits,
        first_commit_date=git_metrics.first_commit_date,
        lifetime_contributors=git_metrics.lifetime_contributors,
        lifetime_concentration=git_metrics.lifetime_concentration,
        is_mature=git_metrics.is_mature,
        repo_age_years=git_metrics.repo_age_years,
        bus_factor=git_metrics.bus_factor,
        elephant_factor=git_metrics.elephant_factor,
        inactive_contributor_ratio=git_metrics.inactive_contributor_ratio,
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
    breakdown = scorer.calculate(package_name, ecosystem, metrics, collected_data.repo_url)
    breakdown.factor_availability = factor_availability
    breakdown.warnings.extend(warnings)
    if provisional_reasons:
        # The score was produced from incomplete-but-conservative inputs
        # (a non-essential signal failed). Surface so the user can rescore.
        breakdown.provisional_reasons = provisional_reasons
        breakdown.recommendations.append(
            "PROVISIONAL: one or more non-essential signals were unavailable; "
            "rescore later via `ossuary rescore-invalid` for the final number."
        )
    return breakdown


def _rebuild_breakdown(cached_score, package_name: str, ecosystem: str) -> Optional[RiskBreakdown]:
    """Reconstruct a RiskBreakdown from cached Score data."""
    try:
        from ossuary.scoring.factors import ProtectiveFactors

        d = cached_score.breakdown
        pkg = d.get("package", {})
        metrics = d.get("metrics", {})
        chaoss = d.get("chaoss_signals", {})
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
            bus_factor=chaoss.get("bus_factor", 0),
            elephant_factor=chaoss.get("elephant_factor", 0),
            inactive_contributor_ratio=chaoss.get("inactive_contributor_ratio", 0.0),
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
            factor_availability=d.get("factor_availability", {}),
            warnings=d.get("warnings", []),
            incomplete_reasons=d.get("incomplete_reasons", []),
            provisional_reasons=d.get("provisional_reasons", []),
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
    freshness_days: Optional[int] = None,
) -> ScoringResult:
    """
    Score a single package.

    Args:
        package_name: Name of the package
        ecosystem: npm, pypi, cargo, rubygems, packagist, nuget, go, or github
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
            cache = ScoreCache(session, freshness_days=freshness_days or ScoreCache(session).freshness_threshold.days)
            package = cache.get_or_create_package(package_name, ecosystem, repo_url)

            if cutoff_date is not None:
                cached_score = cache.get_score_for_cutoff(package, cutoff)
            elif cache.is_fresh(package):
                cached_score = cache.get_current_score(package)
            else:
                cached_score = None

            if cached_score and cached_score.breakdown:
                breakdown = _rebuild_breakdown(cached_score, package_name, ecosystem)
                if breakdown:
                    return ScoringResult(success=True, breakdown=breakdown)

    # Collect data — cached_collect short-circuits to the snapshot cache
    # when a usable prior snapshot exists. Pass through the *original*
    # ``cutoff_date`` parameter (None for current scoring) rather than the
    # derived ``cutoff = datetime.now()``: current scoring needs the
    # freshness SLA path on the cache (≤ 90 days), not the historical
    # ``snapshot.collected_at >= cutoff`` constraint that ``datetime.now()``
    # would impose. See ``docs/data_reuse_design.md`` and
    # ``services/repo_cache.py::get_snapshot_for_cutoff`` for the dispatch.
    collected_data, warnings = await cached_collect(
        package_name, ecosystem, repo_url,
        cutoff_date=cutoff_date,  # original Optional, NOT the derived `cutoff`
        use_cache=use_cache,
    )
    if collected_data is None:
        return ScoringResult(success=False, error=warnings[0] if warnings else "Unknown error")

    # Calculate score
    try:
        breakdown = calculate_score_for_date(package_name, ecosystem, collected_data, cutoff)
    except Exception as e:
        return ScoringResult(success=False, error=str(e), warnings=warnings)

    # Store in cache. INSUFFICIENT_DATA rows persist NULLs for the
    # numeric columns — there is no meaningful score to record, but the
    # row itself documents the attempt and is what `rescore-invalid`
    # finds and retries.
    if use_cache:
        from ossuary.services.repo_cache import RepoSnapshotCache

        is_invalid = breakdown.risk_level == RiskLevel.INSUFFICIENT_DATA
        with session_scope() as session:
            cache = ScoreCache(session, freshness_days=freshness_days or ScoreCache(session).freshness_threshold.days)
            package = cache.get_or_create_package(
                package_name, ecosystem, collected_data.repo_url
            )
            # Look up the snapshot we (or a prior run) just used, so the
            # Score row can record when its underlying upstream data was
            # fetched. Drives the freshness SLA in §4.-0 of methodology.
            # Same dispatch as the cached_collect call above: the original
            # ``cutoff_date`` (Optional) selects current vs historical
            # mode in the cache.
            snapshot_cache = RepoSnapshotCache(session)
            snapshot = snapshot_cache.get_snapshot_for_cutoff(
                package_name, ecosystem, cutoff_date
            )
            data_snapshot_at = snapshot.collected_at if snapshot else None
            score = cache.store_score(
                package=package,
                cutoff_date=cutoff,
                final_score=None if is_invalid else breakdown.final_score,
                risk_level=breakdown.risk_level.value,
                base_risk=None if is_invalid else breakdown.base_risk,
                activity_modifier=None if is_invalid else breakdown.activity_modifier,
                protective_factors_total=None if is_invalid else breakdown.protective_factors.total,
                breakdown=breakdown.to_dict(),
                maintainer_concentration=None if is_invalid else breakdown.maintainer_concentration,
                commits_last_year=None if is_invalid else breakdown.commits_last_year,
                unique_contributors=None if is_invalid else breakdown.unique_contributors,
                weekly_downloads=None if is_invalid else breakdown.weekly_downloads,
                is_provisional=breakdown.is_provisional,
            )
            score.data_snapshot_at = data_snapshot_at
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
            cached_scores = cache.get_historical_scores(package, months)
            if len(cached_scores) >= months:
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
            cache.clear_scores_for_cutoffs(package, [hs.date for hs in historical_scores])

            # Store new scores
            for hs, cutoff in zip(historical_scores, cutoff_dates):
                breakdown = calculate_score_for_date(
                    package_name, ecosystem, collected_data, cutoff
                )
                cache.store_score(
                    package=package,
                    cutoff_date=hs.date,
                    final_score=breakdown.final_score,
                    risk_level=breakdown.risk_level.value,
                    base_risk=breakdown.base_risk,
                    activity_modifier=breakdown.activity_modifier,
                    protective_factors_total=breakdown.protective_factors.total,
                    breakdown=breakdown.to_dict(),
                    maintainer_concentration=hs.concentration,
                    commits_last_year=hs.commits_year,
                    unique_contributors=hs.contributors,
                    weekly_downloads=collected_data.weekly_downloads,
                    is_provisional=breakdown.is_provisional,
                )

    return historical_scores, warnings
