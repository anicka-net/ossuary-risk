# Changelog

All notable changes to Ossuary are documented in this file. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions track [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.1] — 2026-04-24

First public release on PyPI. Headline change is the v0.10 snapshot
cache: scoring N packages that share a repository now does the
expensive GitHub fetch once, not N times. Real-world impact measured
on a 491-package seed run: 87.8% scored, zero crashes, zero rate-limit
hits.

### Added — caching

- **Snapshot cache**: append-only repo-snapshot store keyed by
  package, with a freshness SLA (90 days configurable). Scoring a
  package whose snapshot is fresh skips the upstream fetch entirely.
- **Cross-package / cross-ecosystem repo share**: snapshot lookup by
  canonical repo URL deduplicates across packages and ecosystems
  (e.g. `pypi:requests` and `github:psf/requests` share one
  snapshot). Canonical URL stored as an indexed column for
  SQL-equality lookup.
- **Cheap freshness probe**: when a snapshot's age exceeds the SLA,
  a one-call `pushed_at` probe checks whether the repo has actually
  changed. On match, the cache refreshes only the auxiliary
  signal families (sponsors / orgs / CII / issues / maintainer
  profile) instead of doing a full re-collect, preserving the
  cached commit history.
- **Negative cache** with typed failure classification
  (`failure_kind`: `no_repo_url`, `repo_not_found`,
  `unsupported_ecosystem`) and per-kind TTLs. Stops repeated
  upstream calls for known-permanent failures while still allowing
  retries via `--no-cache`.
- **Per-family GitHub collector refactor**: `GitHubCollector.collect`
  split into 6 family methods (`collect_repo_meta`,
  `collect_maintainer_profile`, `collect_org_admins_family`,
  `collect_cii_family`, `collect_issues_family`,
  `resolve_maintainer`) so the freshness probe can refresh
  individual families on their own cadence.
- **Auto-migration** in `init_db()`: existing v0.9.0 databases get
  the new columns added in place. Idempotent on re-init.

### Added — batch / repo-aware

- **`--repo-aware`** flag on `seed-custom` and `seed-suse`: groups
  packages by canonical repo URL and serialises within each group
  so the snapshot cache is warmed once per repo. Eliminates the
  race where N concurrent scorings of packages sharing a repo each
  do their own GitHub fetch.
- **`--probe-registries`** flag (paired with `--repo-aware`):
  pre-probes registries to learn canonical URLs for entries
  without an explicit `repo:` field, so sibling packages from the
  same monorepo (`nvidia-cuda-*`, `jupyter-*`) get grouped instead
  of racing in parallel. The pre-pass result is plumbed through
  to scoring so `cached_collect` reuses it — net new HTTP per
  probed entry stays at one registry call.
- Repo-aware planning telemetry on `BatchResult`: `unique_repos`,
  `shared_repo_packages`, `unplanable`, `probed`, `probe_resolved`.

### Added — dashboard

- **Per-ecosystem refresh buttons** on the Ecosystems page:
  - "Retry N unscored" — bypasses all caches (score, snapshot,
    negative) for packages stuck in a failure state.
  - "Re-score all N" — bypasses score cache only, keeps snapshot
    reuse for cheap repeats.
- "N tracked but never scored" caption surfaces orphan rows so
  they're discoverable instead of silently inflating package
  counts.

### Added — sentiment

- **Sentiment v6.2** rule templates with maintainer-author
  attribution (replaces v6.1 hard-coded comment scoring).
- **v6.2.1** tightens frustration rules per GPT precision review.

### Changed

- **Version source-of-truth**: `__init__.py` now reads via
  `importlib.metadata.version("ossuary-risk")`, so `pyproject.toml`
  is the only place to bump. Fixes the "dashboard shows v0.9.0
  forever" class of bug.
- **Cargo collector** now carries `homepage_url` as a 404-fallback
  for the `repository` URL. Recovers packages with typo'd
  repository fields (canonical case: `agg` had `savge13/agg` —
  missing 'a' — vs correct `savage13/agg` in homepage). Plumbed
  through to `collect_package_data`, which retries with the
  fallback when the primary 404s.
- **Score cache** no longer pre-creates a `Package` row during the
  cache-check phase. New `ScoreCache.get_package()` is lookup-only;
  `get_or_create_package()` is reserved for write paths. Eliminates
  orphan `last_analyzed=None` rows that surfaced as
  "5 tracked / 0 scored" on the dashboard after a transient
  collection failure.
- **`datetime.utcnow()`** replaced everywhere with
  `ossuary._compat.utcnow_naive()` — drops the Python 3.13
  deprecation warning without changing on-disk semantics.

### Fixed

- `get_snapshot_by_repo_url` previously used `LIMIT 50` + Python
  filter; missed targets at high snapshot volumes. Now uses SQL
  exact equality on the indexed `repo_url_canonical` column.
- Probe-match aux-refresh refuses to reuse a cached blob carrying
  essential `fetch_errors` (would otherwise lock packages into
  stale `INSUFFICIENT_DATA` after a transient registry failure).
- Probe-match aux-refresh clears stale provisional reasons before
  re-running the per-family collectors, so a recovered package
  doesn't carry forward old "GitHub Sponsors lookup failed" notes.

### Internal

- `_compat.py` for the `utcnow_naive()` shim.
- 500+ test suite; new modules: `test_v090_migration.py`,
  `test_cargo_homepage_fallback.py`,
  `test_dashboard_ecosystems_actions.py`, expanded
  `test_repo_cache.py` (79 tests covering canonicalize, freshness
  probe, cross-ecosystem share, failure classification, etc.),
  `test_batch_repo_aware.py` (15 tests including no-double-call
  contracts).

## [0.9.0] — 2026-02-15

Baseline release; first to carry CRA-aligned outputs.

### Added

- **CRA-aligned outputs**: SPDX 2.3 SBOM I/O, support period
  estimation, Annex VII conformance documents.
- **INSUFFICIENT_DATA contract**: scorer refuses to produce a final
  score when essential data (registry downloads, repo info) is
  unavailable. Distinguishes hard failures from provisional scores
  where a non-essential signal is degraded.
- **Provisional scoring**: GitHub auxiliary failures (Sponsors,
  orgs, CII, issues) raise the score conservatively but the result
  is flagged for retry via `rescore-invalid`.
- **CHAOSS bus factor + elephant factor + inactive-contributor
  ratio** alongside the existing maintainer concentration metric.
- **Historical reputation reconstruction** from git log + GitHub
  archive lookups so scoring at a past cutoff date doesn't
  over-count current popularity.
- **PyPI PEP 503 name normalisation** in the cache layer so
  `PyYAML` and `pyyaml` resolve to the same row.
- **Multi-page Streamlit dashboard** (Home / Ecosystems / Package
  / Score / Methodology) with openSUSE branding.
- **`ossuary refresh` CLI** for cron-based re-scoring of stale
  rows.
- **Sentiment v6.1** with full reputation/funding/maturity factor
  set.

### Changed

- PyPI repo-URL discovery rewritten with case-insensitive
  `project_urls` matching, URL cleaning, and homepage scanning.
  Removed 7 manual `repo_url` overrides from the seed list.

[0.10.1]: https://github.com/anicka-net/ossuary-risk/releases/tag/v0.10.1
[0.9.0]: https://github.com/anicka-net/ossuary-risk/releases/tag/v0.9.0
