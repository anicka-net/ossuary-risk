"""Database session management."""

import logging
import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from ossuary.db.models import Base

logger = logging.getLogger(__name__)

# Default to SQLite for development
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ossuary.db")

# Handle SQLite URL format for SQLAlchemy
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _autoapply_simple_migrations(connection) -> None:
    """Apply non-destructive ``ADD COLUMN`` migrations in place.

    SQLAlchemy's ``create_all`` only creates *missing* tables; it
    silently leaves an existing table alone even if the model has
    grown new columns. Without help, a user who upgrades Ossuary in
    place hits a hard SQL error on the first write to a new column
    (most recently: ``scores.is_provisional``).

    Each entry below is an in-place ``ALTER TABLE ... ADD COLUMN``
    that the SQLite engine can apply without recreating the table.
    Idempotent — we check ``PRAGMA table_info`` first and skip any
    column that already exists. Only safe migrations live here;
    schema changes that need data movement (NOT NULL → nullable,
    column rename, etc.) still ship as standalone scripts under
    ``scripts/``.
    """
    inspector = inspect(connection)
    if "scores" not in inspector.get_table_names():
        # Brand-new DB — create_all() built the table with the column already.
        return

    existing_cols = {col["name"] for col in inspector.get_columns("scores")}
    if "is_provisional" not in existing_cols:
        logger.warning(
            "Auto-migrating scores schema: adding is_provisional column "
            "(see scripts/migrate_provisional_column.py for the standalone version)"
        )
        connection.execute(text(
            "ALTER TABLE scores ADD COLUMN is_provisional "
            "BOOLEAN NOT NULL DEFAULT 0"
        ))
    if "data_snapshot_at" not in existing_cols:
        logger.warning(
            "Auto-migrating scores schema: adding data_snapshot_at column "
            "for the v0.10 repo-snapshot cache freshness SLA"
        )
        connection.execute(text(
            "ALTER TABLE scores ADD COLUMN data_snapshot_at DATETIME"
        ))

    if "packages" in inspector.get_table_names():
        package_cols = {col["name"] for col in inspector.get_columns("packages")}
        if "last_failed_at" not in package_cols:
            logger.warning(
                "Auto-migrating packages schema: adding last_failed_at + "
                "failure_reason for the v0.10 negative cache"
            )
            connection.execute(text(
                "ALTER TABLE packages ADD COLUMN last_failed_at DATETIME"
            ))
        if "failure_reason" not in package_cols:
            connection.execute(text(
                "ALTER TABLE packages ADD COLUMN failure_reason VARCHAR(500)"
            ))
        if "failure_kind" not in package_cols:
            # GPT review #3 priority 4: typed failure classifier replaces
            # free-text LIKE matching in stats() and TTL lookups. The
            # column starts NULL on existing rows; the backfill below
            # populates it from each row's prior failure_reason text.
            logger.warning(
                "Auto-migrating packages schema: adding failure_kind + "
                "backfilling typed classifications from prior failure_reason"
            )
            connection.execute(text(
                "ALTER TABLE packages ADD COLUMN failure_kind VARCHAR(50)"
            ))
            # Backfill in Python so the classifier stays in one place
            # (services/repo_cache.classify_failure) rather than being
            # duplicated as portable SQL CASE expressions across backends.
            from ossuary.services.repo_cache import classify_failure
            rows = connection.execute(text(
                "SELECT id, failure_reason FROM packages "
                "WHERE failure_reason IS NOT NULL AND failure_kind IS NULL"
            )).fetchall()
            for row in rows:
                kind = classify_failure(row.failure_reason)
                if kind is not None:
                    connection.execute(
                        text(
                            "UPDATE packages SET failure_kind = :k WHERE id = :i"
                        ),
                        {"k": kind, "i": row.id},
                    )


def init_db() -> None:
    """Initialize the database, creating tables and applying pending
    in-place migrations.

    Existing databases that pre-date a schema-extending release would
    otherwise crash on the first write — see
    :func:`_autoapply_simple_migrations` for the auto-applied set.
    Migrations that need data movement still ship as scripts under
    ``scripts/``.
    """
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _autoapply_simple_migrations(conn)


def get_session() -> Generator[Session, None, None]:
    """Get a database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
