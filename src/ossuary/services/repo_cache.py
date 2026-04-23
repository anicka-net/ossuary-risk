"""Snapshot cache for raw collected upstream data.

Persists ``CollectedData`` blobs so that repeat scoring of the same package
can reuse the cached upstream view instead of re-hitting npm/PyPI/GitHub.
The big payoff is methodology iteration — bumping the scoring formula
invalidates the score cache (see ``services/cache.py::ScoreCache``) but
*not* the snapshot cache, so re-scoring 170 validation packages after a
methodology change is bounded by DB read time, not GitHub rate limit.

See ``docs/data_reuse_design.md`` for the full design. v0.10 was strictly
package-keyed (one snapshot per ``(name, ecosystem)``). v0.10.1 adds an
opportunistic repo-keyed lookup path: when scoring via the ``github``
ecosystem (the only path where the canonical repo URL is known with zero
upstream calls), a snapshot written by *any* package — across ecosystems —
that resolved to the same canonical URL can be served. Registry-derived
fields (currently just ``weekly_downloads``) are zeroed before return so
the github-ecosystem score retains its "no registry data" semantics.

The broader cross-ecosystem repo share — e.g. ``axios`` on npm and
``requests`` on pypi sharing repo data when both happen to land on the
same monorepo — requires splitting ``CollectedData`` into a
registry-derived half and a repo-derived half so the registry data can
stay per-package while repo data is shared. That split is the v0.11
design (``docs/data_reuse_design.md`` §4) and is intentionally out of
scope for the v0.10.1 slice.

**Lookup semantics — current vs historical scoring.**

- *Current scoring* (caller passes ``cutoff_date=None``): the most-recent
  snapshot is returned subject to the freshness SLA in
  ``docs/methodology.md`` §4.-0 (≤ 90 days = usable; older = miss, force
  refresh).
- *Historical scoring* (caller passes a past ``cutoff_date``): the
  most-recent snapshot whose ``collected_at >= cutoff_date`` is returned.
  Such a snapshot necessarily contains all data that existed at the
  cutoff (later commits / issues are filtered out at scoring time).
  ``coverage_until`` is recorded for phase-2 incremental fetches and is
  not used as a lookup constraint here — using it would require
  ``coverage_until >= cutoff``, which fails for any project whose last
  commit predates the cutoff date.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from ossuary._compat import utcnow_naive
from ossuary.db.models import Package, RepoSnapshot
from ossuary.services.cache import normalize_package_name


# ---------------------------------------------------------------------------
# Canonical repo URL
# ---------------------------------------------------------------------------

# Captures one or two leading slashes, optional ssh-form (git@host:owner/repo),
# trailing slash, and trailing ``.git``. The intent is conservative: only
# normalisations that demonstrably point to the same underlying repo.
# Forks and redirects are NOT followed — that's a v0.11 question (design
# doc §9.3); doing it wrong here would silently merge different repos.
_GIT_SSH_RE = re.compile(r"^git@([^:]+):(.+?)(?:\.git)?/?$")


def canonicalize_repo_url(repo_url: Optional[str]) -> Optional[str]:
    """Normalise a repo URL to a canonical form for cache key comparison.

    Rules (intentionally conservative):

    - Strip surrounding whitespace.
    - Convert ``git@host:owner/repo`` → ``https://host/owner/repo``.
    - Strip trailing ``.git``.
    - Strip trailing ``/``.
    - Lowercase the host and the path (GitHub treats owner/repo as
      case-insensitive; case-only differences across snapshots would
      otherwise miss the cache).
    - Force ``https://`` for ``http://`` and ssh forms.

    Returns ``None`` if the input is empty or looks unparseable. We do
    NOT chase redirects, normalise www. prefixes, or merge mirror
    domains — those are correctness decisions for a future repo-identity
    layer (design doc §9.3).
    """
    if not repo_url:
        return None
    url = repo_url.strip()
    if not url:
        return None

    ssh_match = _GIT_SSH_RE.match(url)
    if ssh_match:
        host, path = ssh_match.group(1), ssh_match.group(2)
        url = f"https://{host}/{path}"
    elif url.startswith("http://"):
        url = "https://" + url[len("http://") :]

    # Strip trailing slashes first so a path like ``foo.git/`` collapses
    # before the ``.git`` check, then strip ``.git``, then strip any
    # residual trailing slash. Idempotent under repeated application.
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    url = url.rstrip("/")

    # Lowercase scheme + host + path. GitHub paths are case-insensitive
    # for routing; lowercasing makes ``axios-http/axios`` and
    # ``Axios-Http/Axios`` collide on the same key.
    if "://" in url:
        scheme, rest = url.split("://", 1)
        url = f"{scheme.lower()}://{rest.lower()}"
    else:
        url = url.lower()

    return url or None

logger = logging.getLogger(__name__)


# Bumped when the on-disk shape of CollectedData (or its nested dataclasses,
# CommitData, GitHubData, IssueData) changes in a way that breaks
# deserialisation. Methodology bumps do NOT touch this — the formula reads
# the same raw data whether it's v6.3 or v6.5.
COLLECTOR_VERSION = 1


# Freshness SLA bands (days) for the current-scoring path. See
# ``docs/methodology.md`` §4.-0 ("Operational SLA"). Beyond EXPIRED_DAYS
# the snapshot is rejected by the cache and a fresh fetch is forced.
SLA_FRESH_DAYS = 30
SLA_EXPIRED_DAYS = 90


# Negative-cache TTLs (days). A *permanent* failure (404, no repo URL on
# the registry, etc.) is recorded so subsequent runs skip the upstream
# probe. Re-probed after the TTL elapses — repos rarely come back from
# 404, but registry metadata can be updated more often, so the latter
# uses a shorter TTL.
NEGCACHE_TTL_DEAD_REPO_DAYS = 90
NEGCACHE_TTL_NO_REPO_FIELD_DAYS = 30


# Typed failure classifier (GPT review #3 priority 4). String constants
# rather than a Python ``Enum`` so SQLAlchemy can store them as a plain
# ``String`` column without backend-specific enum types — keeps the
# Postgres / SQLite story uniform. Stored on ``Package.failure_kind``
# alongside the human-readable ``failure_reason``.
class FailureKind:
    NO_REPO_URL = "no_repo_url"          # registry has no repository URL field
    REPO_NOT_FOUND = "repo_not_found"    # 404 on the resolved repo URL
    UNSUPPORTED_ECOSYSTEM = "unsupported_ecosystem"  # caller asked for an eco we don't collect

    ALL = (NO_REPO_URL, REPO_NOT_FOUND, UNSUPPORTED_ECOSYSTEM)


# Per-kind TTL. Looking up by typed key replaces the v0.10 free-text
# matching that needed ``func.lower(...).like(...)`` to be portable.
_TTL_BY_KIND = {
    FailureKind.NO_REPO_URL: NEGCACHE_TTL_NO_REPO_FIELD_DAYS,
    FailureKind.REPO_NOT_FOUND: NEGCACHE_TTL_DEAD_REPO_DAYS,
    FailureKind.UNSUPPORTED_ECOSYSTEM: NEGCACHE_TTL_DEAD_REPO_DAYS,
}


def classify_failure(warning: str) -> Optional[str]:
    """Map a collector warning string to a ``FailureKind`` constant.

    Returns ``None`` for transient failures (rate limit, 5xx, network,
    INSUFFICIENT_DATA) and for warnings that don't match any known
    permanent class — both cases mean "don't negative-cache this; let
    the standard retry path handle it".

    The text-matching layer is preserved here because collectors return
    free-form warning strings from heterogeneous sources (npm 404 vs
    PyPI "not found" vs git clone exit 128). The result is a typed
    constant the rest of the cache layer uses for TTL and SQL filters.
    """
    if not warning:
        return None
    text = warning.lower()
    # Transient — never negative-cache.
    if any(token in text for token in (
        "rate limit", "rate-limit", "429", "500", "502", "503", "504",
        "timeout", "transport", "insufficient_data",
    )):
        return None
    if "no repository url" in text:
        return FailureKind.NO_REPO_URL
    if "unsupported ecosystem" in text:
        return FailureKind.UNSUPPORTED_ECOSYSTEM
    if "not found" in text:
        # 404, missing repo, registry-says-no-such-package, etc. The
        # ``not found`` check is intentionally last so the more
        # specific phrases above win.
        return FailureKind.REPO_NOT_FOUND
    return None


def is_permanent_failure(warning: str) -> bool:
    """Backwards-compatible boolean wrapper around :func:`classify_failure`."""
    return classify_failure(warning) is not None


def _ttl_for_kind(kind: Optional[str]) -> int:
    """Days to wait before re-probing this failure class.

    Falls back to the dead-repo TTL for unknown / legacy rows (cautious:
    longer TTL so we don't hammer upstream re-probing something we can't
    classify)."""
    return _TTL_BY_KIND.get(kind or "", NEGCACHE_TTL_DEAD_REPO_DAYS)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _isoformat(value: Any) -> Any:
    """Recursively convert datetimes to ISO strings; pass through everything else.

    Used at serialisation time so ``json.dumps`` (via SQLAlchemy's JSON column)
    accepts the blob.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _isoformat(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_isoformat(v) for v in value]
    return value


def serialise_collected_data(data: Any) -> dict:
    """Turn a CollectedData dataclass instance into a JSON-safe dict.

    ``CollectedData`` and its nested dataclasses (``CommitData``,
    ``GitHubData``, ``IssueData``) all use ``@dataclass`` so
    ``dataclasses.asdict()`` recursively walks them. The only post-processing
    needed is converting ``datetime`` instances to ISO strings.
    """
    if not is_dataclass(data):
        raise TypeError(f"Expected a dataclass, got {type(data).__name__}")
    return _isoformat(asdict(data))


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Best-effort ISO-string → datetime parser. Returns None on falsy input."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _hydrate_dataclass(cls: type, blob: dict) -> Any:
    """Reconstruct a dataclass from a dict, walking nested dataclass fields.

    Uses ``typing.get_type_hints`` to resolve string-form annotations and
    parameterised generics (``list[CommitData]``, ``Optional[datetime]``)
    consistently. Datetime fields are detected by annotation, so an empty
    string at a datetime field is correctly parsed (to None) rather than
    silently kept as a string.
    """
    import typing as _typing

    if blob is None:
        return None

    try:
        type_hints = _typing.get_type_hints(cls)
    except Exception:
        type_hints = {}

    kwargs = {}
    for f in fields(cls):
        if f.name not in blob:
            continue
        raw = blob[f.name]
        anno = type_hints.get(f.name, f.type)

        kwargs[f.name] = _hydrate_value(anno, raw)

    return cls(**kwargs)


def _hydrate_value(anno: Any, raw: Any) -> Any:
    """Convert a raw JSON value back to the type the annotation calls for.

    Handles datetimes, nested dataclasses, ``list[T]``, and ``Optional[T]``.
    Falls through to returning ``raw`` unchanged for primitives and
    structures we don't need to reconstruct (free-form ``dict[str, Any]``,
    issue ``comments`` lists of dicts, etc.).
    """
    import typing as _typing

    if raw is None:
        return None

    origin = _typing.get_origin(anno)
    args = _typing.get_args(anno)

    # Optional[T] → Union[T, None]
    if origin is _typing.Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _hydrate_value(non_none[0], raw)
        # Multi-type unions are unusual on our dataclasses; pass through.
        return raw

    # Datetime
    if anno is datetime:
        return _parse_datetime(raw)

    # Nested dataclass
    if isinstance(anno, type) and is_dataclass(anno):
        return _hydrate_dataclass(anno, raw)

    # list[T]
    if origin in (list, tuple):
        if args and (is_dataclass(args[0]) or args[0] is datetime):
            return [_hydrate_value(args[0], item) for item in (raw or [])]
        return list(raw or [])

    # Plain primitive / dict / unknown: pass through.
    return raw


def deserialise_collected_data(blob: dict, target_cls: type) -> Any:
    """Inverse of :func:`serialise_collected_data`. Returns a CollectedData instance.

    The caller passes ``target_cls=CollectedData`` rather than this module
    importing it directly, to avoid a circular import (services.scorer →
    repo_cache → services.scorer).
    """
    return _hydrate_dataclass(target_cls, blob)


def _coverage_until_from_blob(blob: dict) -> Optional[datetime]:
    """Extract the latest authored_date across cached commits.

    Used at write time to populate ``RepoSnapshot.coverage_until`` so that
    cutoff-based lookups can answer "is this snapshot fresh enough?" without
    re-scanning the blob on every read.
    """
    commits = blob.get("all_commits") or []
    latest: Optional[datetime] = None
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        dt = _parse_datetime(commit.get("authored_date"))
        if dt is None:
            continue
        if latest is None or dt > latest:
            latest = dt
    return latest


# ---------------------------------------------------------------------------
# Cache class
# ---------------------------------------------------------------------------

class RepoSnapshotCache:
    """Read/write access to ``repo_snapshots`` rows.

    Append-only: ``store`` always inserts a new row. Lookups return the most
    recent snapshot whose ``coverage_until`` is on or after the requested
    cutoff (or any snapshot, when ``cutoff_date`` is None — used for
    "current" scoring with the freshness SLA layered on top).
    """

    def __init__(self, session: Session):
        self.session = session

    def _get_or_create_package(
        self, name: str, ecosystem: str, repo_url: Optional[str] = None
    ) -> Package:
        """Mirror of ScoreCache.get_or_create_package, kept local so this
        module does not depend on the score cache (separable concerns)."""
        canonical = normalize_package_name(name, ecosystem)
        package = (
            self.session.query(Package)
            .filter(Package.name == canonical, Package.ecosystem == ecosystem)
            .first()
        )
        if package is None:
            package = Package(name=canonical, ecosystem=ecosystem, repo_url=repo_url)
            self.session.add(package)
            self.session.flush()
        elif repo_url and not package.repo_url:
            package.repo_url = repo_url
        return package

    def get_snapshot_for_cutoff(
        self,
        name: str,
        ecosystem: str,
        cutoff_date: Optional[datetime] = None,
        sla_expired_days: int = SLA_EXPIRED_DAYS,
    ) -> Optional[RepoSnapshot]:
        """Return the most recent usable snapshot, or None on miss.

        Filters by ``fetcher_version == COLLECTOR_VERSION`` so a collector
        schema bump invalidates older blobs without explicit eviction.

        Two modes:

        - ``cutoff_date is None`` → *current scoring*. Returns the most
          recent snapshot whose ``collected_at`` is within ``sla_expired_days``
          of now (the SLA "Expired" boundary). Older snapshots are not
          served; the caller will refetch.
        - ``cutoff_date is not None`` → *historical scoring*. Returns the
          most recent snapshot whose ``collected_at >= cutoff_date`` —
          i.e., the snapshot was fetched at or after the cutoff and so
          contains everything that existed at that point (later activity
          gets filtered out at scoring time). ``coverage_until`` is
          deliberately NOT used as a lookup constraint here — see module
          docstring for why.
        """
        canonical = normalize_package_name(name, ecosystem)
        package = (
            self.session.query(Package)
            .filter(Package.name == canonical, Package.ecosystem == ecosystem)
            .first()
        )
        if package is None:
            return None

        query = (
            self.session.query(RepoSnapshot)
            .filter(
                RepoSnapshot.package_id == package.id,
                RepoSnapshot.fetcher_version == COLLECTOR_VERSION,
            )
        )
        if cutoff_date is None:
            # Current-scoring path: enforce freshness SLA on collected_at.
            sla_cutoff = utcnow_naive() - timedelta(days=sla_expired_days)
            query = query.filter(RepoSnapshot.collected_at >= sla_cutoff)
        else:
            # Historical-scoring path: snapshot must have been taken on
            # or after the requested cutoff date.
            query = query.filter(RepoSnapshot.collected_at >= cutoff_date)

        return query.order_by(RepoSnapshot.collected_at.desc()).first()

    def get_snapshot_by_repo_url(
        self,
        repo_url: str,
        cutoff_date: Optional[datetime] = None,
        sla_expired_days: int = SLA_EXPIRED_DAYS,
    ) -> Optional[RepoSnapshot]:
        """Look up the most recent snapshot for a canonical repo URL.

        Unlike :meth:`get_snapshot_for_cutoff`, this is keyed on the
        repo URL rather than the package, so a snapshot written by *any*
        package whose ``repo_url`` canonicalises to the same value is
        eligible. Used by ``cached_collect`` for the github-ecosystem
        path, where the canonical URL is known before any upstream call.

        Same freshness / cutoff semantics as the package-keyed lookup
        (see that method's docstring). Filters by ``fetcher_version`` so
        a collector schema bump invalidates older blobs.

        Returns ``None`` for an unparseable / empty URL — defensive: if
        canonicalisation fails the caller should fall through to the
        normal collector path rather than risk a wrong-repo hit.

        Implementation: filters on ``repo_url_canonical`` (indexed) with
        SQL exact equality. The earlier v0.10.1-step-1a version pulled
        a LIMIT-bounded set of recent snapshots and re-canonicalised in
        Python, which broke at high volume — GPT review reproduced a
        miss with 51 newer unrelated snapshots. The current design is
        O(log n) instead of O(50) and correct at any scale.
        """
        canonical = canonicalize_repo_url(repo_url)
        if not canonical:
            return None

        query = (
            self.session.query(RepoSnapshot)
            .filter(
                RepoSnapshot.fetcher_version == COLLECTOR_VERSION,
                RepoSnapshot.repo_url_canonical == canonical,
            )
        )

        if cutoff_date is None:
            sla_cutoff = utcnow_naive() - timedelta(days=sla_expired_days)
            query = query.filter(RepoSnapshot.collected_at >= sla_cutoff)
        else:
            query = query.filter(RepoSnapshot.collected_at >= cutoff_date)

        return query.order_by(RepoSnapshot.collected_at.desc()).first()

    # ----- Negative cache -----

    def get_negative_cache(
        self, name: str, ecosystem: str
    ) -> Optional[str]:
        """Return the cached failure reason if the package is in the negative
        cache and the TTL has not elapsed; ``None`` otherwise.

        A return value of ``None`` means "go ahead and probe upstream" —
        either there is no recorded failure, or the TTL has expired and we
        should re-check whether the failure is still present (a renamed
        repo might be reachable again, a registry might have grown a
        repository URL field).
        """
        canonical = normalize_package_name(name, ecosystem)
        package = (
            self.session.query(Package)
            .filter(Package.name == canonical, Package.ecosystem == ecosystem)
            .first()
        )
        if package is None or package.last_failed_at is None or not package.failure_reason:
            return None

        # Prefer the typed kind; fall back to re-classifying the legacy
        # text for rows written by pre-v0.10.1 code that didn't populate
        # failure_kind. The auto-migration backfills, so this fallback
        # is just defensive belt-and-braces.
        kind = package.failure_kind or classify_failure(package.failure_reason)
        age = utcnow_naive() - package.last_failed_at
        ttl_days = _ttl_for_kind(kind)
        if age >= timedelta(days=ttl_days):
            # TTL elapsed — caller should re-probe.
            return None

        return package.failure_reason

    def store_negative(self, name: str, ecosystem: str, reason: str) -> None:
        """Record a permanent failure on the package row.

        Idempotent: replaces any prior failure on the same package. A later
        successful collection should clear this via ``clear_negative``. The
        free-text ``reason`` is classified into a typed ``failure_kind`` so
        TTL lookups and ``stats()`` queries can use exact equality instead
        of fragile LIKE matching.
        """
        package = self._get_or_create_package(name, ecosystem)
        package.last_failed_at = utcnow_naive()
        package.failure_reason = reason
        package.failure_kind = classify_failure(reason)

    def clear_negative(self, name: str, ecosystem: str) -> None:
        """Clear any negative-cache state for a package.

        Called after a successful collection so a recovered package
        doesn't keep returning the cached failure once its TTL is back to
        zero from the next failure.
        """
        canonical = normalize_package_name(name, ecosystem)
        package = (
            self.session.query(Package)
            .filter(Package.name == canonical, Package.ecosystem == ecosystem)
            .first()
        )
        if package is not None:
            package.last_failed_at = None
            package.failure_reason = None
            package.failure_kind = None

    # ----- Statistics / introspection -----

    def stats(self) -> dict:
        """Return a snapshot of the cache's operational state.

        Used by ``ossuary cache-stats`` and the thesis operational-scalability
        section. All counts are exact (no sampling); on a SUSE-scale DB this
        is a few cheap aggregates over indexed columns.
        """
        from sqlalchemy import func

        total_snapshots = self.session.query(func.count(RepoSnapshot.id)).scalar() or 0
        unique_packages = (
            self.session.query(func.count(func.distinct(RepoSnapshot.package_id)))
            .scalar() or 0
        )

        now = utcnow_naive()
        fresh_cutoff = now - timedelta(days=SLA_FRESH_DAYS)
        expired_cutoff = now - timedelta(days=SLA_EXPIRED_DAYS)

        fresh = self.session.query(func.count(RepoSnapshot.id)).filter(
            RepoSnapshot.collected_at >= fresh_cutoff,
            RepoSnapshot.fetcher_version == COLLECTOR_VERSION,
        ).scalar() or 0
        stale = self.session.query(func.count(RepoSnapshot.id)).filter(
            RepoSnapshot.collected_at < fresh_cutoff,
            RepoSnapshot.collected_at >= expired_cutoff,
            RepoSnapshot.fetcher_version == COLLECTOR_VERSION,
        ).scalar() or 0
        expired = self.session.query(func.count(RepoSnapshot.id)).filter(
            RepoSnapshot.collected_at < expired_cutoff,
            RepoSnapshot.fetcher_version == COLLECTOR_VERSION,
        ).scalar() or 0
        wrong_version = self.session.query(func.count(RepoSnapshot.id)).filter(
            RepoSnapshot.fetcher_version != COLLECTOR_VERSION,
        ).scalar() or 0

        # Negative cache: split into "active" (still within TTL — what
        # ``get_negative_cache`` would actually serve) and "total" (every
        # row with a recorded failure, including expired ones still
        # taking up disk).
        #
        # TTL is per failure-class. Lookup uses exact equality on the
        # typed ``failure_kind`` column — no LIKE / func.lower required.
        # The pre-v0.10.1 version had to ``func.lower(...).like(...)``
        # the free-text ``failure_reason`` and got bitten by case
        # sensitivity on PostgreSQL / MySQL. Now the classifier runs
        # once at write time and the SQL stays a clean equality filter.
        # Legacy rows (``failure_kind IS NULL``) are bucketed under the
        # longer dead-repo TTL — same fallback as ``_ttl_for_kind``.
        no_repo_cutoff = now - timedelta(days=NEGCACHE_TTL_NO_REPO_FIELD_DAYS)
        dead_repo_cutoff = now - timedelta(days=NEGCACHE_TTL_DEAD_REPO_DAYS)

        active_no_repo = self.session.query(func.count(Package.id)).filter(
            Package.last_failed_at.isnot(None),
            Package.failure_reason.isnot(None),
            Package.failure_kind == FailureKind.NO_REPO_URL,
            Package.last_failed_at >= no_repo_cutoff,
        ).scalar() or 0
        active_dead_repo = self.session.query(func.count(Package.id)).filter(
            Package.last_failed_at.isnot(None),
            Package.failure_reason.isnot(None),
            (Package.failure_kind != FailureKind.NO_REPO_URL)
            | (Package.failure_kind.is_(None)),
            Package.last_failed_at >= dead_repo_cutoff,
        ).scalar() or 0
        neg_active = active_no_repo + active_dead_repo

        neg_total_recorded = self.session.query(func.count(Package.id)).filter(
            Package.last_failed_at.isnot(None),
            Package.failure_reason.isnot(None),
        ).scalar() or 0

        return {
            "snapshots": {
                "total": total_snapshots,
                "unique_packages": unique_packages,
                "fresh": fresh,        # ≤ SLA_FRESH_DAYS
                "stale": stale,        # SLA_FRESH_DAYS < age ≤ SLA_EXPIRED_DAYS
                "expired": expired,    # > SLA_EXPIRED_DAYS
                "wrong_collector_version": wrong_version,
            },
            "negative_cache": {
                # Currently being served by get_negative_cache — what
                # actually skips upstream probes today.
                "active": neg_active,
                # Every row with a recorded failure, including expired
                # ones taking up disk. Useful for capacity planning;
                # not what affects cache-hit behaviour.
                "total_recorded": neg_total_recorded,
            },
            "sla": {
                "fresh_days": SLA_FRESH_DAYS,
                "expired_days": SLA_EXPIRED_DAYS,
                "negcache_no_repo_days": NEGCACHE_TTL_NO_REPO_FIELD_DAYS,
                "negcache_dead_repo_days": NEGCACHE_TTL_DEAD_REPO_DAYS,
            },
            "collector_version": COLLECTOR_VERSION,
        }

    # ----- Snapshot write -----

    def store_snapshot(
        self,
        name: str,
        ecosystem: str,
        repo_url: Optional[str],
        blob: dict,
        collected_at: Optional[datetime] = None,
    ) -> RepoSnapshot:
        """Write a new snapshot row. Append-only — does not mutate prior rows.

        ``coverage_until`` is derived from the latest commit ``authored_date``
        in the blob; if there are no commits (empty repo, fetch failed), it
        falls back to ``collected_at`` so the snapshot is at least valid for
        cutoffs up to the moment of fetch.
        """
        package = self._get_or_create_package(name, ecosystem, repo_url)
        now = collected_at or utcnow_naive()
        coverage_until = _coverage_until_from_blob(blob) or now

        # Pull github_data.pushed_at out of the blob (if present) so the
        # freshness probe can compare against it on the next refresh
        # without rehydrating the full CollectedData.
        upstream_pushed_at = None
        gh = blob.get("github_data") if isinstance(blob, dict) else None
        if isinstance(gh, dict):
            upstream_pushed_at = gh.get("pushed_at") or None

        snapshot = RepoSnapshot(
            package_id=package.id,
            collected_at=now,
            coverage_until=coverage_until,
            repo_url=repo_url,
            repo_url_canonical=canonicalize_repo_url(repo_url),
            blob=blob,
            fetcher_version=COLLECTOR_VERSION,
            upstream_pushed_at=upstream_pushed_at,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def get_latest_snapshot_any_age(
        self, name: str, ecosystem: str
    ) -> Optional[RepoSnapshot]:
        """Return the most recent snapshot for a package regardless of SLA.

        Used by the freshness-probe path: when ``get_snapshot_for_cutoff``
        misses because a snapshot is past the SLA window, we still want
        to know whether a (now-stale) snapshot exists so we can probe the
        upstream and revalidate it cheaply if the repo hasn't changed.

        Filters by ``fetcher_version`` — a snapshot from an old collector
        schema can't be revalidated, only re-collected.
        """
        canonical = normalize_package_name(name, ecosystem)
        package = (
            self.session.query(Package)
            .filter(Package.name == canonical, Package.ecosystem == ecosystem)
            .first()
        )
        if package is None:
            return None
        return (
            self.session.query(RepoSnapshot)
            .filter(
                RepoSnapshot.package_id == package.id,
                RepoSnapshot.fetcher_version == COLLECTOR_VERSION,
            )
            .order_by(RepoSnapshot.collected_at.desc())
            .first()
        )

    def extend_snapshot_freshness(self, snapshot: RepoSnapshot) -> None:
        """Bump a snapshot's ``collected_at`` to ``now`` after validating
        upstream is unchanged. The blob, ``coverage_until`` and
        ``upstream_pushed_at`` stay the same — only the freshness clock
        is reset. Idempotent.
        """
        snapshot.collected_at = utcnow_naive()
        self.session.flush()
