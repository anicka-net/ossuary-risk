#!/usr/bin/env python3
"""Add the ``scores.is_provisional`` column.

Background: ``RiskBreakdown`` gained an ``is_provisional`` flag for
scores computed with one or more non-essential signals missing
(e.g. GitHub Sponsors lookup rate-limited). The score is conservative
(higher than truth) but still actionable; ``rescore-invalid`` should
retry these rows alongside the strict ``INSUFFICIENT_DATA`` ones.

Older score rows pre-date the flag and are correctly modeled as
``False`` — they were either fully successful or hard failures, never
"successful but with caveats".

SQLite ``ALTER TABLE ADD COLUMN`` accepts a default and is non-destructive,
so this migration is much simpler than the nullable-numeric one. Idempotent:
re-running detects the column and exits cleanly.

Dry run by default. Pass ``--apply`` to execute.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "ossuary.db"


def column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    for _cid, name, *_ in con.execute(f"PRAGMA table_info({table})"):
        if name == column:
            return True
    return False


def migrate(db_path: Path, *, apply: bool) -> int:
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    con = sqlite3.connect(db_path)
    if column_exists(con, "scores", "is_provisional"):
        print(f"{db_path}: scores.is_provisional already present. Nothing to do.")
        return 0

    n_rows = con.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    print(f"{db_path}: scores.is_provisional missing — needs migration.")
    print(f"  rows to backfill (default False): {n_rows}")

    if not apply:
        print("\nDRY RUN. Re-run with --apply to execute the migration.")
        return 0

    try:
        con.execute(
            "ALTER TABLE scores ADD COLUMN is_provisional BOOLEAN NOT NULL DEFAULT 0"
        )
        con.commit()
    except Exception as exc:
        print(f"FAILED: {exc}")
        return 1

    after = con.execute(
        "SELECT COUNT(*) FROM scores WHERE is_provisional = 0"
    ).fetchone()[0]
    print(f"Migrated. Rows backfilled to is_provisional=False: {after}.")
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
