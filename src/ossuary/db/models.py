"""SQLAlchemy models for ossuary."""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ossuary._compat import utcnow_naive


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class Ecosystem(str, Enum):
    """Supported package ecosystems."""

    NPM = "npm"
    PYPI = "pypi"


class Package(Base):
    """A package being tracked."""

    __tablename__ = "packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ecosystem: Mapped[str] = mapped_column(String(50), nullable=False)
    repo_url: Mapped[Optional[str]] = mapped_column(String(500))

    # Metadata from registry
    description: Mapped[Optional[str]] = mapped_column(Text)
    homepage: Mapped[Optional[str]] = mapped_column(String(500))

    # Tracking
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    last_analyzed: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Negative cache: when a collection attempt fails with a *permanent*
    # error (404 on the repo, no repository URL on the registry, etc.),
    # we record the timestamp and reason so subsequent runs can skip the
    # probe until the TTL elapses. Transient failures (rate limit, 5xx,
    # network) are NOT cached here — they go through the standard retry
    # path because they can recover. See
    # ``services/repo_cache.py::is_permanent_failure``.
    last_failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    failure_reason: Mapped[Optional[str]] = mapped_column(String(500))
    # Typed classification of the failure (no_repo_url, repo_not_found,
    # unsupported_ecosystem). Lets ``stats()`` and TTL lookups use exact
    # equality instead of free-text LIKE matching — see GPT review #3
    # priority 4. ``failure_reason`` stays around as the operator-readable
    # message; ``failure_kind`` is the SQL-friendly identifier.
    failure_kind: Mapped[Optional[str]] = mapped_column(String(50))

    # Relationships
    commits: Mapped[list["Commit"]] = relationship(back_populates="package", cascade="all, delete-orphan")
    issues: Mapped[list["Issue"]] = relationship(back_populates="package", cascade="all, delete-orphan")
    scores: Mapped[list["Score"]] = relationship(back_populates="package", cascade="all, delete-orphan")
    sentiment_records: Mapped[list["SentimentRecord"]] = relationship(
        back_populates="package", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("name", "ecosystem", name="uq_package_name_ecosystem"),
        Index("ix_package_ecosystem", "ecosystem"),
    )


class Commit(Base):
    """A commit from a package's repository."""

    __tablename__ = "commits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"))

    sha: Mapped[str] = mapped_column(String(40), nullable=False)
    author_name: Mapped[str] = mapped_column(String(255))
    author_email: Mapped[str] = mapped_column(String(255))
    authored_date: Mapped[datetime] = mapped_column(DateTime)

    committer_name: Mapped[Optional[str]] = mapped_column(String(255))
    committer_email: Mapped[Optional[str]] = mapped_column(String(255))
    committed_date: Mapped[Optional[datetime]] = mapped_column(DateTime)

    message: Mapped[str] = mapped_column(Text)

    # Relationships
    package: Mapped["Package"] = relationship(back_populates="commits")

    __table_args__ = (
        UniqueConstraint("package_id", "sha", name="uq_commit_package_sha"),
        Index("ix_commit_authored_date", "authored_date"),
        Index("ix_commit_author_email", "author_email"),
    )


class Issue(Base):
    """An issue or PR from a package's repository."""

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"))

    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[Optional[str]] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20))  # open, closed
    is_pull_request: Mapped[bool] = mapped_column(default=False)

    author_login: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Store comments as JSON array
    comments: Mapped[Optional[dict]] = mapped_column(JSON)

    # Relationships
    package: Mapped["Package"] = relationship(back_populates="issues")

    __table_args__ = (
        UniqueConstraint("package_id", "number", name="uq_issue_package_number"),
        Index("ix_issue_created_at", "created_at"),
    )


class Score(Base):
    """A calculated risk score for a package."""

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"))

    # Score calculation date and cutoff
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    cutoff_date: Mapped[datetime] = mapped_column(DateTime)

    # Final score. ``NULL`` when ``risk_level == 'INSUFFICIENT_DATA'`` —
    # the methodology contract is not to compute a numeric score from
    # partial input data; reasons are captured in the ``breakdown`` JSON
    # under ``incomplete_reasons``. The component columns below are
    # likewise nullable for the same reason.
    final_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(32))

    # Score components (NULL for INSUFFICIENT_DATA rows)
    base_risk: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    activity_modifier: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    protective_factors_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sentiment_modifier: Mapped[int] = mapped_column(Integer, default=0)

    # Detailed breakdown stored as JSON
    breakdown: Mapped[dict] = mapped_column(JSON)

    # Core metrics at time of scoring (NULL for INSUFFICIENT_DATA rows)
    maintainer_concentration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    commits_last_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    unique_contributors: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    weekly_downloads: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)

    # True when the score was computed with one or more non-essential
    # signals missing (e.g. GitHub Sponsors lookup rate-limited). The
    # score itself is valid but conservative; ``rescore-invalid`` retries
    # these rows alongside ``risk_level == 'INSUFFICIENT_DATA'`` rows.
    # Reasons live in ``breakdown['provisional_reasons']``.
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # When the underlying CollectedData snapshot was fetched. Distinct
    # from ``calculated_at`` (when the formula was run): a methodology
    # bump can produce a fresh ``calculated_at`` against an older
    # ``data_snapshot_at``. Drives the freshness SLA bands surfaced in
    # CLI / API output (see ``docs/methodology.md`` Operational SLA).
    # Nullable for legacy rows that pre-date the snapshot cache.
    data_snapshot_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    package: Mapped["Package"] = relationship(back_populates="scores")

    __table_args__ = (Index("ix_score_calculated_at", "calculated_at"),)


class RepoSnapshot(Base):
    """Cached raw collected data for a package, point-in-time.

    Stores the full ``CollectedData`` blob (commits, GitHub data, registry
    metadata, downloads) as JSON so that repeat scoring of the same package
    can skip redundant upstream calls. Append-only — each refresh writes a
    new row rather than mutating the previous one, so a query for any
    historical cutoff resolves to the earliest snapshot whose
    ``coverage_until`` is on or after that cutoff.

    **Cache key in v0.10:** keyed on ``package_id`` (i.e. ``(name,
    ecosystem)``). The repo URL the snapshot resolved to is recorded in
    ``repo_url`` so that a future refactor can re-key on canonical repo URL
    and share snapshots between ecosystem packages and the GitHub-side
    counterpart of the same project. See ``docs/data_reuse_design.md`` for
    the target architecture.

    **Methodology vs collector versioning:** ``fetcher_version`` invalidates
    snapshots when the collector schema changes (a new field on
    ``CollectedData`` means old blobs cannot be deserialised back).
    Methodology version bumps deliberately do *not* invalidate snapshots —
    the formula reads the same raw data, so iteration on the formula does
    not cost API calls.
    """

    __tablename__ = "repo_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"))

    # Server clock when this snapshot was fetched.
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    # Latest ``authored_date`` across the cached commits (i.e. the upper
    # bound of historical data the snapshot covers). A scoring request
    # for ``cutoff_date <= coverage_until`` can use this snapshot;
    # anything later requires a refresh.
    coverage_until: Mapped[datetime] = mapped_column(DateTime)

    # Canonical repo URL the snapshot resolved to (or None when the
    # package has no upstream repo). The ``repo_url`` field stores the
    # original spelling as provided by the registry (preserved for
    # diagnostics); ``repo_url_canonical`` stores the normalised form
    # (lowercase, no .git, no trailing slash, ssh→https) so SQL lookups
    # for cross-package shared-repo hits use exact equality on an
    # indexable column. Filtering in Python after a LIMIT-bounded read
    # — the v0.10.1-step-1a approach — broke at high snapshot volume
    # (GPT review: with 51 newer unrelated snapshots, the target was
    # not returned).
    repo_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    repo_url_canonical: Mapped[Optional[str]] = mapped_column(String(500), nullable=True, index=True)

    # Serialised CollectedData blob: commits, github_data (incl. issues),
    # weekly_downloads, maintainer_account_created, repo_stargazers,
    # fetch_errors, provisional_reasons. Datetimes are ISO strings.
    blob: Mapped[dict] = mapped_column(JSON)

    # Snapshot is invalidated when the on-disk collector schema no longer
    # matches what serialised the blob — bumped when CollectedData or its
    # nested dataclasses gain/lose fields in a backwards-incompatible way.
    fetcher_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # GitHub ``pushed_at`` for the repo at snapshot time (ISO string from
    # the API). Used by the freshness probe (v0.10.1 phase 3 step 3): one
    # cheap GET /repos/{owner}/{repo} compares this against current
    # upstream pushed_at; if unchanged, the snapshot is still valid and
    # we extend its freshness without paying for a full re-collect.
    # Nullable for legacy rows written before the column existed — those
    # cannot use the probe path and fall through to full re-collect.
    upstream_pushed_at: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Relationships (no back_populates intentionally — the relationship is
    # one-way; Package does not enumerate its snapshots in normal use).

    __table_args__ = (
        Index("ix_repo_snapshot_package_collected", "package_id", "collected_at"),
        Index("ix_repo_snapshot_coverage_until", "coverage_until"),
    )


class SentimentRecord(Base):
    """Sentiment analysis result for a piece of text."""

    __tablename__ = "sentiment_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"))

    # Source of the text
    source_type: Mapped[str] = mapped_column(String(50))  # commit, issue, comment
    source_id: Mapped[str] = mapped_column(String(255))  # sha or issue number

    # Text hash for deduplication
    text_hash: Mapped[str] = mapped_column(String(64))

    # Sentiment scores
    compound_score: Mapped[float] = mapped_column(Float)  # -1 to 1
    positive_score: Mapped[float] = mapped_column(Float)
    negative_score: Mapped[float] = mapped_column(Float)
    neutral_score: Mapped[float] = mapped_column(Float)

    # Frustration detection
    frustration_detected: Mapped[bool] = mapped_column(default=False)
    frustration_keywords: Mapped[Optional[list]] = mapped_column(JSON)

    # Metadata
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    # Relationships
    package: Mapped["Package"] = relationship(back_populates="sentiment_records")

    __table_args__ = (
        UniqueConstraint("package_id", "text_hash", name="uq_sentiment_package_hash"),
        Index("ix_sentiment_source_type", "source_type"),
    )
