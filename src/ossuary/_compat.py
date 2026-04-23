"""Compatibility helpers for Python version differences.

Currently houses the naive-UTC replacement for ``datetime.utcnow()``,
which was deprecated in Python 3.12 and is scheduled for removal.
"""

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Return current UTC time as a naive ``datetime`` (no tzinfo).

    Drop-in replacement for the deprecated ``datetime.utcnow()``.
    Preserves naive-UTC semantics so existing DB columns
    (SQLAlchemy ``DateTime`` is naive by default) and arithmetic
    against other naive UTC datetimes stored in the database keep
    working without a wider migration to timezone-aware datetimes.

    A future PR can swap this implementation for fully-aware UTC
    once the schema and all comparison sites have been audited; one
    central helper keeps that migration to a single edit.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
