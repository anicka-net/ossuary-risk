"""Snapshot cache for raw collected upstream data.

Persists ``CollectedData`` blobs so that repeat scoring of the same package
can reuse the cached upstream view instead of re-hitting npm/PyPI/GitHub.
The big payoff is methodology iteration — bumping the scoring formula
invalidates the score cache (see ``services/cache.py::ScoreCache``) but
*not* the snapshot cache, so re-scoring 170 validation packages after a
methodology change is bounded by DB read time, not GitHub rate limit.

See ``docs/data_reuse_design.md`` for the full design including the v0.11
repo-keyed evolution. v0.10 is package-keyed (one snapshot per
``(name, ecosystem)``) with the resolved repo URL recorded for the future
re-keying.

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
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from ossuary.db.models import Package, RepoSnapshot
from ossuary.services.cache import normalize_package_name

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
            sla_cutoff = datetime.utcnow() - timedelta(days=sla_expired_days)
            query = query.filter(RepoSnapshot.collected_at >= sla_cutoff)
        else:
            # Historical-scoring path: snapshot must have been taken on
            # or after the requested cutoff date.
            query = query.filter(RepoSnapshot.collected_at >= cutoff_date)

        return query.order_by(RepoSnapshot.collected_at.desc()).first()

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
        now = collected_at or datetime.utcnow()
        coverage_until = _coverage_until_from_blob(blob) or now

        snapshot = RepoSnapshot(
            package_id=package.id,
            collected_at=now,
            coverage_until=coverage_until,
            repo_url=repo_url,
            blob=blob,
            fetcher_version=COLLECTOR_VERSION,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot
