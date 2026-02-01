"""SQLAlchemy models for ossuary."""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    JSON,
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_analyzed: Mapped[Optional[datetime]] = mapped_column(DateTime)

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
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    cutoff_date: Mapped[datetime] = mapped_column(DateTime)

    # Final score
    final_score: Mapped[int] = mapped_column(Integer)
    risk_level: Mapped[str] = mapped_column(String(20))  # CRITICAL, HIGH, MODERATE, LOW, VERY_LOW

    # Score components
    base_risk: Mapped[int] = mapped_column(Integer)
    activity_modifier: Mapped[int] = mapped_column(Integer)
    protective_factors_total: Mapped[int] = mapped_column(Integer)
    sentiment_modifier: Mapped[int] = mapped_column(Integer, default=0)

    # Detailed breakdown stored as JSON
    breakdown: Mapped[dict] = mapped_column(JSON)

    # Core metrics at time of scoring
    maintainer_concentration: Mapped[float] = mapped_column(Float)
    commits_last_year: Mapped[int] = mapped_column(Integer)
    unique_contributors: Mapped[int] = mapped_column(Integer)
    weekly_downloads: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    package: Mapped["Package"] = relationship(back_populates="scores")

    __table_args__ = (Index("ix_score_calculated_at", "calculated_at"),)


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
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    package: Mapped["Package"] = relationship(back_populates="sentiment_records")

    __table_args__ = (
        UniqueConstraint("package_id", "text_hash", name="uq_sentiment_package_hash"),
        Index("ix_sentiment_source_type", "source_type"),
    )
