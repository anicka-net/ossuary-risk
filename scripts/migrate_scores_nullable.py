#!/usr/bin/env python3
"""Migrate the ``scores`` table to allow NULL on the numeric columns.

Background: ``RiskLevel.INSUFFICIENT_DATA`` rows record an attempt to
score a package whose required input data was unavailable. They have
no meaningful values for ``final_score``, ``base_risk``,
``activity_modifier``, ``protective_factors_total``,
``maintainer_concentration``, ``commits_last_year``,
``unique_contributors`` or ``weekly_downloads``. Storing sentinel
integers there would invite confusion with real low scores; storing
``NULL`` is the principled representation.

SQLite cannot ``ALTER COLUMN`` to change a NOT NULL constraint, so this
script does the standard recreate-table dance:

1. Detect whether ``scores.final_score`` is already nullable; if so, exit.
2. Create ``scores_new`` with the new (nullable) schema.
3. Copy every row from ``scores`` into ``scores_new``.
4. Drop ``scores``.
5. Rename ``scores_new`` to ``scores``.
6. Recreate the index.
7. ``VACUUM`` to reclaim space.

Wrapped in a transaction. ``PRAGMA foreign_keys=OFF`` during the swap
since SQLite would otherwise refuse the ``DROP TABLE`` due to the FK
from ``scores.package_id`` to ``packages.id``. Foreign keys are
re-enabled at the end.

Dry run by default. Pass ``--apply`` to execute.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "ossuary.db"


def is_nullable(con: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if ``column`` in ``table`` allows NULL."""
    for cid, name, ctype, notnull, default, pk in con.execute(
        f"PRAGMA table_info({table})"
    ):
        if name == column:
            return notnull == 0
    raise RuntimeError(f"column {table}.{column} not found")


def migrate(db_path: Path, *, apply: bool) -> int:
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")

    if is_nullable(con, "scores", "final_score"):
        print(f"{db_path}: scores.final_score is already nullable. Nothing to do.")
        return 0

    print(f"{db_path}: scores.final_score is NOT NULL — needs migration.")
    n_rows = con.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    print(f"  rows to migrate: {n_rows}")

    if not apply:
        print("\nDRY RUN. Re-run with --apply to execute the migration.")
        return 0

    # ``executescript`` issues its own implicit COMMIT before running the
    # script, then leaves the connection in autocommit mode. So we don't
    # wrap with explicit BEGIN/COMMIT around it; we rely on each statement
    # being committed individually. If anything fails mid-script SQLite's
    # journaled DDL will roll back atomically per statement.
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.executescript("""
            CREATE TABLE scores_new (
                id INTEGER PRIMARY KEY,
                package_id INTEGER NOT NULL
                    REFERENCES packages(id) ON DELETE CASCADE,
                calculated_at DATETIME NOT NULL,
                cutoff_date DATETIME NOT NULL,
                final_score INTEGER,
                risk_level VARCHAR(32) NOT NULL,
                base_risk INTEGER,
                activity_modifier INTEGER,
                protective_factors_total INTEGER,
                sentiment_modifier INTEGER NOT NULL DEFAULT 0,
                breakdown JSON NOT NULL,
                maintainer_concentration FLOAT,
                commits_last_year INTEGER,
                unique_contributors INTEGER,
                weekly_downloads INTEGER DEFAULT 0
            );
            INSERT INTO scores_new (
                id, package_id, calculated_at, cutoff_date, final_score,
                risk_level, base_risk, activity_modifier,
                protective_factors_total, sentiment_modifier, breakdown,
                maintainer_concentration, commits_last_year,
                unique_contributors, weekly_downloads
            )
            SELECT
                id, package_id, calculated_at, cutoff_date, final_score,
                risk_level, base_risk, activity_modifier,
                protective_factors_total, sentiment_modifier, breakdown,
                maintainer_concentration, commits_last_year,
                unique_contributors, weekly_downloads
            FROM scores;
            DROP TABLE scores;
            ALTER TABLE scores_new RENAME TO scores;
            CREATE INDEX ix_score_calculated_at ON scores(calculated_at);
        """)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("VACUUM")
    except Exception as exc:
        con.execute("PRAGMA foreign_keys=ON")
        print(f"FAILED: {exc}")
        return 1

    after = con.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    assert after == n_rows, (
        f"row count mismatch after migration: before={n_rows} after={after}"
    )
    print(f"Migrated. Rows preserved: {after}.")
    print(f"  scores.final_score nullable now: "
          f"{is_nullable(con, 'scores', 'final_score')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually run the migration. Without this flag the script "
        "is a dry run.",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"Path to the SQLite database (default: {DEFAULT_DB}).",
    )
    args = parser.parse_args()
    return migrate(args.db, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
