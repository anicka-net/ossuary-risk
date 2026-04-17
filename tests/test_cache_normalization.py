"""Name-normalisation contract for the cache layer.

Background: a DB duplicate was found on 2026-04-17 where the same PyPI
distribution lived under two different capitalisations (``PyYAML`` and
``pyyaml``) with separately cached scores. The cache-layer lookup was
case-sensitive so two code paths feeding different cases created two
rows. This test pins the fix: ``get_or_create_package`` normalises
PyPI names per PEP 503 so the same logical package resolves to the
same row regardless of how the caller wrote it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ossuary.db.models import Base
from ossuary.services.cache import ScoreCache, normalize_package_name


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class TestNormalizePackageName:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("PyYAML", "pyyaml"),
            ("pyyaml", "pyyaml"),
            ("py_yaml", "py-yaml"),
            ("py.yaml", "py-yaml"),
            ("  PyYAML  ", "pyyaml"),
            # Runs of dividers collapse to a single hyphen (PEP 503).
            ("py__yaml", "py-yaml"),
            ("py.-yaml", "py-yaml"),
            # Already canonical.
            ("requests", "requests"),
            ("urllib3", "urllib3"),
        ],
    )
    def test_pypi_pep503(self, given, expected):
        assert normalize_package_name(given, "pypi") == expected

    @pytest.mark.parametrize(
        "ecosystem,given",
        [
            # Other ecosystems are pass-through. Lockdown against speculative
            # normalisation being added without evidence.
            ("npm", "@babel/core"),
            ("npm", "Socket.IO"),
            ("cargo", "Serde_json"),
            ("rubygems", "Rails"),
            ("go", "github.com/Spf13/cobra"),
            ("github", "anicka-net/ossuary-risk"),
            ("packagist", "symfony/Console"),
            ("nuget", "Newtonsoft.Json"),
        ],
    )
    def test_other_ecosystems_pass_through(self, ecosystem, given):
        assert normalize_package_name(given, ecosystem) == given


class TestGetOrCreatePackageDedup:
    def test_case_variants_resolve_to_same_row(self, session):
        # All four resolve to canonical "pyyaml" — no internal dividers.
        cache = ScoreCache(session)
        a = cache.get_or_create_package("PyYAML", "pypi")
        b = cache.get_or_create_package("pyyaml", "pypi")
        c = cache.get_or_create_package("PYYAML", "pypi")
        d = cache.get_or_create_package("  PyYAML  ", "pypi")
        session.commit()
        assert a.id == b.id == c.id == d.id
        assert a.name == "pyyaml"  # canonical form stored

    def test_divider_variants_resolve_to_same_row(self, session):
        # PEP 503: ``-``, ``_`` and ``.`` collapse to a single ``-``. So
        # ``foo-bar``, ``foo_bar``, ``foo.bar``, ``Foo--Bar`` all dedupe.
        cache = ScoreCache(session)
        a = cache.get_or_create_package("foo-bar", "pypi")
        b = cache.get_or_create_package("foo_bar", "pypi")
        c = cache.get_or_create_package("foo.bar", "pypi")
        d = cache.get_or_create_package("Foo--Bar", "pypi")
        session.commit()
        assert a.id == b.id == c.id == d.id
        assert a.name == "foo-bar"

    def test_packages_differing_only_by_dividers_distinct(self, session):
        # Lockdown: a name without dividers (``pyyaml``) and one with
        # (``py-yaml``) ARE different canonical packages on PyPI. Don't
        # over-dedupe.
        cache = ScoreCache(session)
        a = cache.get_or_create_package("pyyaml", "pypi")
        b = cache.get_or_create_package("py-yaml", "pypi")
        session.commit()
        assert a.id != b.id

    def test_same_ecosystem_different_name_creates_distinct_rows(self, session):
        cache = ScoreCache(session)
        a = cache.get_or_create_package("pyyaml", "pypi")
        b = cache.get_or_create_package("pydantic", "pypi")
        session.commit()
        assert a.id != b.id

    def test_same_name_different_ecosystem_creates_distinct_rows(self, session):
        # ``lodash`` exists on npm; if somebody also tried it on PyPI the rows
        # must remain distinct — normalisation is per-ecosystem, not global.
        cache = ScoreCache(session)
        a = cache.get_or_create_package("lodash", "npm")
        b = cache.get_or_create_package("lodash", "pypi")
        session.commit()
        assert a.id != b.id
        assert a.ecosystem == "npm"
        assert b.ecosystem == "pypi"

    def test_repo_url_filled_in_on_dedup_hit(self, session):
        """When a later call supplies a repo_url the row didn't have yet,
        it should be populated — not just silently discarded."""
        cache = ScoreCache(session)
        first = cache.get_or_create_package("PyYAML", "pypi", repo_url=None)
        assert first.repo_url is None
        cache.get_or_create_package("pyyaml", "pypi",
                                     repo_url="https://github.com/yaml/pyyaml")
        session.commit()
        refreshed = cache.get_or_create_package("PyYAML", "pypi")
        assert refreshed.repo_url == "https://github.com/yaml/pyyaml"

    def test_npm_case_sensitivity_preserved(self, session):
        """Lockdown: npm must still be case-sensitive until we have evidence
        it should not be. A silent npm normalisation would collide scoped
        packages or legitimately-distinct registrations."""
        cache = ScoreCache(session)
        a = cache.get_or_create_package("Socket.IO", "npm")
        b = cache.get_or_create_package("socket.io", "npm")
        session.commit()
        # Different rows; the bug we fixed is pypi-specific.
        assert a.id != b.id
