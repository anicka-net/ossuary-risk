from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ossuary._compat import utcnow_naive
from ossuary.collectors.git import CommitData
from ossuary.collectors.github import GitHubData
from ossuary.db.models import Base
from ossuary.services.repo_cache import (
    RepoSnapshotCache,
    serialise_collected_data,
)
from ossuary.services.scorer import CollectedData, RegistryData, cached_collect


@pytest.fixture
def isolated_cache(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    @contextmanager
    def isolated_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr("ossuary.services.scorer.session_scope", isolated_scope)
    yield isolated_scope
    engine.dispose()


def _data(repo_url: str, *, fetch_errors=None) -> CollectedData:
    owner, repo = repo_url.removeprefix("https://github.com/").split("/", 1)
    return CollectedData(
        repo_url=repo_url,
        all_commits=[],
        github_data=GitHubData(owner=owner, repo=repo),
        weekly_downloads=0,
        maintainer_account_created=None,
        fetch_errors=list(fetch_errors or []),
    )


@pytest.mark.asyncio
async def test_explicit_repo_override_bypasses_mismatched_package_snapshot(
    isolated_cache,
):
    old = _data("https://github.com/old/project")
    new = _data("https://github.com/new/project")
    with isolated_cache() as session:
        RepoSnapshotCache(session).store_snapshot(
            "demo", "github", old.repo_url, serialise_collected_data(old),
        )

    with patch(
        "ossuary.services.scorer.collect_package_data",
        return_value=(new, []),
    ) as fresh_collect:
        result, warnings = await cached_collect(
            "demo",
            "github",
            repo_url=new.repo_url,
        )

    assert warnings == []
    assert result is not None
    assert result.repo_url == new.repo_url
    fresh_collect.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_data_bypasses_invalid_snapshot(isolated_cache):
    stale = _data(
        "https://github.com/acme/project",
        fetch_errors=["github.repo_info: HTTP 503"],
    )
    recovered = _data("https://github.com/acme/project")
    with isolated_cache() as session:
        RepoSnapshotCache(session).store_snapshot(
            "acme/project",
            "github",
            stale.repo_url,
            serialise_collected_data(stale),
        )

    with patch(
        "ossuary.services.scorer.collect_package_data",
        return_value=(recovered, []),
    ) as fresh_collect:
        result, warnings = await cached_collect(
            "acme/project",
            "github",
            refresh_data=True,
        )

    assert warnings == []
    assert result is not None
    assert result.fetch_errors == []
    fresh_collect.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_snapshot_rekey_preserves_collection_time(isolated_cache):
    donor_time = utcnow_naive() - timedelta(days=10)
    donor = _data("https://github.com/acme/shared")
    with isolated_cache() as session:
        RepoSnapshotCache(session).store_snapshot(
            "donor",
            "github",
            donor.repo_url,
            serialise_collected_data(donor),
            collected_at=donor_time,
        )

    result, warnings = await cached_collect(
        "acme/shared",
        "github",
        repo_url=donor.repo_url,
    )

    assert warnings == []
    assert result is not None
    with isolated_cache() as session:
        copied = RepoSnapshotCache(session).get_snapshot_for_cutoff(
            "acme/shared", "github",
        )
        assert copied is not None
        assert copied.collected_at == donor_time


@pytest.mark.asyncio
async def test_prefetched_registry_still_uses_shared_repo_snapshot(isolated_cache):
    donor = _data("https://github.com/acme/shared")
    with isolated_cache() as session:
        RepoSnapshotCache(session).store_snapshot(
            "donor",
            "npm",
            donor.repo_url,
            serialise_collected_data(donor),
        )

    prefetched = RegistryData(
        repo_url=donor.repo_url,
        weekly_downloads=123,
        fetch_errors=[],
        warnings=[],
    )
    with patch(
        "ossuary.services.scorer.collect_package_data",
    ) as fresh_collect:
        result, warnings = await cached_collect(
            "recipient",
            "pypi",
            prefetched_registry=prefetched,
        )

    assert warnings == []
    assert result is not None
    assert result.repo_url == donor.repo_url
    assert result.weekly_downloads == 123
    fresh_collect.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_snapshot_backfills_maintainer_source_identity(isolated_cache):
    collected_at = utcnow_naive()
    old = _data("https://github.com/acme/project")
    old.all_commits = [
        CommitData(
            sha="1",
            author_name="Alice",
            author_email="alice@example.com",
            authored_date=collected_at - timedelta(days=10),
            committer_name="Alice",
            committer_email="alice@example.com",
            committed_date=collected_at - timedelta(days=10),
            message="test",
        )
    ]
    with isolated_cache() as session:
        RepoSnapshotCache(session).store_snapshot(
            "project",
            "github",
            old.repo_url,
            serialise_collected_data(old),
            collected_at=collected_at,
        )

    result, warnings = await cached_collect("project", "github")

    assert warnings == []
    assert result is not None
    assert result.github_data.maintainer_source_email == "alice@example.com"
