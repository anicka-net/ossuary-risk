"""End-to-end test: v0.9.0 → v0.10.1 in-place upgrade.

A user upgrading their existing local Ossuary install hits
``init_db()`` on first launch — and a broken migration there is the
worst-possible first impression of a new release. This test seeds a
v0.9.0-shaped SQLite DB (the schema before any of the v0.10 cache
work landed), runs ``init_db()``, and verifies:

1. all v0.10 columns get added (``packages.failure_kind``,
   ``packages.last_failed_at``, ``packages.failure_reason``,
   ``scores.is_provisional``, ``scores.data_snapshot_at``,
   ``repo_snapshots.upstream_pushed_at``,
   ``repo_snapshots.repo_url_canonical``).
2. the existing v0.9.0 ``packages`` and ``scores`` rows are
   preserved bit-for-bit.
3. the ``repo_url_canonical`` index exists (under either the
   create_all auto-name or the migration's manual-name; both are
   functionally equivalent — the test pins behaviour, not the name).
4. ``init_db()`` is idempotent — calling it a second time on the
   already-migrated DB doesn't error with "duplicate column".
"""

from __future__ import annotations

import sqlite3

import pytest

from ossuary._compat import utcnow_naive


def _build_v090_db(db_path: str) -> int:
    """Create a v0.9.0-shaped schema + a representative row. Returns
    the package_id so the test can verify preservation."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE packages (
                id INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                ecosystem VARCHAR(50) NOT NULL,
                repo_url VARCHAR(500),
                description TEXT,
                homepage VARCHAR(500),
                created_at DATETIME,
                last_analyzed DATETIME,
                UNIQUE(name, ecosystem)
            );
            CREATE TABLE scores (
                id INTEGER PRIMARY KEY,
                package_id INTEGER NOT NULL REFERENCES packages(id),
                calculated_at DATETIME,
                cutoff_date DATETIME,
                final_score INTEGER,
                risk_level VARCHAR(50),
                base_risk INTEGER,
                activity_modifier INTEGER,
                protective_factors_total INTEGER,
                sentiment_modifier INTEGER,
                breakdown JSON,
                maintainer_concentration FLOAT,
                commits_last_year INTEGER,
                unique_contributors INTEGER,
                weekly_downloads INTEGER
            );
            CREATE TABLE commits (
                id INTEGER PRIMARY KEY,
                package_id INTEGER NOT NULL REFERENCES packages(id),
                sha VARCHAR(40),
                author_name VARCHAR(255),
                author_email VARCHAR(255),
                authored_date DATETIME
            );
        """)
        now = utcnow_naive().isoformat()
        conn.execute(
            "INSERT INTO packages (name, ecosystem, repo_url, "
            "created_at, last_analyzed) VALUES (?, ?, ?, ?, ?)",
            ("requests", "pypi",
             "https://github.com/psf/requests", now, now),
        )
        pkg_id = conn.execute(
            "SELECT id FROM packages WHERE name='requests'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO scores (package_id, calculated_at, cutoff_date, "
            "final_score, risk_level, base_risk, activity_modifier, "
            "protective_factors_total, sentiment_modifier, breakdown, "
            "maintainer_concentration, commits_last_year, "
            "unique_contributors, weekly_downloads) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pkg_id, now, now, 25, "LOW", 30, 5, 10, 0, "{}",
             25.0, 200, 50, 1_000_000),
        )
        conn.commit()
        return pkg_id
    finally:
        conn.close()


@pytest.fixture
def v090_db(tmp_path, monkeypatch):
    """Build a fresh v0.9.0-shaped DB and rebind ossuary.db.session
    against it. Yields ``(db_path, package_id)``."""
    db_path = tmp_path / "v090.db"
    pkg_id = _build_v090_db(str(db_path))

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    # session.py captures DATABASE_URL at import time; rebuild the
    # engine + sessionmaker so init_db hits our fresh DB.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from ossuary.db import session as db_session

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(
        db_session, "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    return str(db_path), pkg_id


class TestV090ToV0101Migration:
    def test_all_v010_columns_get_added(self, v090_db):
        db_path, _ = v090_db
        from ossuary.db.session import init_db
        init_db()

        conn = sqlite3.connect(db_path)
        try:
            expected = {
                "packages": {
                    "last_failed_at", "failure_reason", "failure_kind",
                },
                "scores": {"is_provisional", "data_snapshot_at"},
                "repo_snapshots": {
                    "upstream_pushed_at", "repo_url_canonical",
                },
            }
            for table, want in expected.items():
                cols = {
                    r[1] for r in conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                missing = want - cols
                assert not missing, (
                    f"{table}: migration left columns missing: {missing}"
                )
        finally:
            conn.close()

    def test_existing_v090_rows_preserved(self, v090_db):
        db_path, pkg_id = v090_db
        from ossuary.db.session import init_db
        init_db()

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT name, ecosystem, repo_url FROM packages "
                "WHERE id=?", (pkg_id,),
            ).fetchone()
            assert row == (
                "requests", "pypi",
                "https://github.com/psf/requests",
            ), f"package row corrupted: {row}"

            score = conn.execute(
                "SELECT final_score, risk_level, is_provisional "
                "FROM scores WHERE package_id=?", (pkg_id,),
            ).fetchone()
            # is_provisional defaults to 0 (False) on the new column.
            assert score == (25, "LOW", 0), f"score row issue: {score}"
        finally:
            conn.close()

    def test_repo_url_canonical_index_exists(self, v090_db):
        """The repo_url_canonical column needs an index for the
        SQL-equality lookup in get_snapshot_by_repo_url to scale —
        previous LIMIT 50 + Python filter missed targets at high
        snapshot volume. Pin that the index exists, regardless of
        which name path created it (create_all auto-name vs migration
        manual-name)."""
        db_path, _ = v090_db
        from ossuary.db.session import init_db
        init_db()

        conn = sqlite3.connect(db_path)
        try:
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='repo_snapshots'"
            ).fetchall()
            covering = []
            for (idx_name,) in indexes:
                cols = [
                    r[2] for r in conn.execute(
                        f"PRAGMA index_info({idx_name})"
                    ).fetchall()
                ]
                if cols == ["repo_url_canonical"]:
                    covering.append(idx_name)
            assert covering, (
                f"no index covers repo_url_canonical (existing "
                f"indexes: {[i[0] for i in indexes]})"
            )
        finally:
            conn.close()

    def test_init_db_is_idempotent(self, v090_db):
        """A user who restarts ossuary multiple times must not see
        'duplicate column' errors on the second-and-later runs."""
        from ossuary.db.session import init_db
        init_db()
        # Second call would crash on duplicate ALTER TABLE if migrations
        # forgot to gate on column-already-exists.
        init_db()
        # Third for good measure.
        init_db()

    def test_migrated_db_can_actually_be_used(self, v090_db, monkeypatch):
        """Full smoke test: after migration, the cache layer can
        write and read snapshots — verifies the new columns are
        actually wired into the model, not just present in the DB."""
        from ossuary.db.session import init_db, SessionLocal
        from ossuary.services.repo_cache import RepoSnapshotCache

        init_db()

        with SessionLocal() as s:
            cache = RepoSnapshotCache(s)
            cache.store_snapshot(
                name="newpkg",
                ecosystem="npm",
                repo_url="https://github.com/example/test",
                blob={"github_data": {"pushed_at": "2026-04-24T00:00:00Z"}},
            )
            s.commit()

            snap = cache.get_snapshot_by_repo_url(
                "https://github.com/example/test",
            )
            assert snap is not None
            # The new repo_url_canonical column is what the equality
            # lookup uses internally — if it weren't being populated,
            # this would return None.
            assert snap.repo_url_canonical == (
                "https://github.com/example/test"
            )
