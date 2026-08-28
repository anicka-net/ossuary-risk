# Changelog

All notable changes to Ossuary are documented in this file. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions track [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Release-candidate work for the next stable release. The scoring methodology
advances to v6.4.3 and the final thesis-facing validation cutoff is 2026-08-15.

Scores can differ from 0.11.0 for the same package. v6.4.1 corrected the
weighted-concentration numerator and historical maintainer-identity reuse;
v6.4.2 prevents current-only evidence from entering historical scores and
tightens maintainer identity checks; v6.4.3 separates human stewardship from
automation and contributor-authored commit text. The major-version boundary
records those changes rather than presenting 1.0 as a packaging-only release.

### Validation — final public checkpoint

- Collector-version-5 evidence was freshly collected at the August checkpoint;
  v6.4.3 re-scored the retained snapshots across all 184 cases with zero errors
  and zero provisional rows. The deleted `pyphetools` repository is the single
  disclosed pinned-evidence fixture.
- Scope B (n=162): accuracy 92.6%, precision 94.1%, recall 76.2%, F1 0.842
  (32 TP / 10 FN / 2 FP / 118 TN).
- All cases: accuracy 83.7%, precision 94.7%, recall 56.2%, F1 0.706
  (36 TP / 28 FN / 2 FP / 118 TN).
- v6.4.2 matched the 0.11.0 Scope-B aggregate with changed membership:
  `es5-ext` and `is-promise` entered detection while `moment` and `fsnotify`
  left it. v6.4.3 then moved `telnyx` into detection by removing its bot-driven
  activity bonus. Control false positives changed from `rxjs`/`rayon` to
  `jsonwebtoken`/`Newtonsoft.Json` for the recorded factor-level reasons.
- Out-of-scope bonus detections changed as historical isolation moved
  `eslint-scope`, `eslint-config-prettier`, and `chalk` above threshold while
  removing the post-incident `trivy-action` detection. Out-of-scope cases do
  not contribute to Scope-B recall.

### Methodology — v6.4.1 to v6.4.3

- Select tapered concentration by the largest weighted contributor total,
  rather than selecting by raw commit count and then applying weights.
- Historical git evidence now requires both author and committer timestamps
  at or before the cutoff. Named incident dates include the full UTC day.
- Historical scoring neutralizes rolling current downloads, stars, Sponsors,
  issue/comment samples, and bounded current merge-author samples.
- Maintainer reputation requires a resolved human GitHub `User` bound to the
  relevant Git contributor. Bots, organizations, and changed or unproven
  identities receive no reputation reduction.
- The 12-month activity modifier excludes standard GitHub `[bot]` authors.
  This moves `telnyx` 55→85 because 502/513 window commits were automation;
  five changed controls remain true negatives.
- Commit frustration is attributed to the top non-`[bot]` contributor. False
  technical-phrase matches in React, Next.js, pandas, and Terraform disappear
  without changing their floored score or classification.
- The retained v6.4.3 evidence comes from a second same-day collector-v5 pass.
  Separate from the six methodology movements, corrected Guzzle evidence moves
  `guzzlehttp/guzzle` 10→45 through activity (754→41 commits) and concentration
  (71.6%→91.0%) changes; it remains a true negative.

### Data and cache integrity

- Score-cache rows carry their methodology version, so a methodology bump
  cannot reuse an incompatible persisted score.
- Snapshot and negative-cache reuse is collector-versioned; refresh controls
  bypass reads without disabling writes.
- Current validation checkpoints and git evidence use UTC consistently.
- Per-row validation artifacts now populate reputation-factor detail and are
  rejected unless every case is complete and non-provisional.

### Reliability

- CLI, API, dashboard, dependency-tree, SBOM, and support-period paths preserve
  `INSUFFICIENT_DATA` instead of converting missing evidence into favourable
  output.
- Repository-aware cache reuse preserves package-specific registry data and
  donor snapshot timestamps.
- CI runs the complete test suite on pushes and pull requests. The release
  candidate passes all 624 tests.

## [0.11.0] — 2026-06-13

Scoring-correctness release. Methodology advances to v6.4, and a
full-repo review (three independent passes, June 2026) fixed several
bugs that fed wrong data into scoring. Scores produced by this version
can differ from 0.10.1 for the same package — hence a minor bump, not
a patch. Validation after the sweep reproduced the prior Scope-B
result exactly (accuracy 92.0%, precision 93.9%, recall 73.8%,
F1 0.827 at n=162), confirming the fixes corrected mechanism without
moving the calibrated outcome.

### Methodology — v6.4

- **Sole-maintainer burnout escalation**: a burnout signal on a
  single-maintainer project is escalated, since burnout is more
  dangerous where there is no second maintainer to absorb it.
- **Takeover tenure two-mode check**: suspect-contributor tenure now
  distinguishes the xz-utils fast-trust pattern from ordinary
  governance concentration. Tenure spans the full commit history up to
  the cutoff (previously the historical window only, systematically
  short by the recent-window length, which could mislabel contributors
  near the 3-year boundary).
- **Merge-author bus factor** via GitHub GraphQL: bus factor now
  accounts for who actually merges pull requests, not only who commits.

### Fixed — score-corrupting collector bugs

- **Stale git history**: `clone_or_update` fast-forwarded remote refs
  but never moved `HEAD`, so previously cloned repos served history
  frozen at first-clone time and `commits_last_year` decayed toward
  zero as a function of clone age. The local branch ref is now
  fast-forwarded after fetch (via `update-ref`, preserving blobless
  clones).
- **Oldest-100 merged PRs**: the merged-PR GraphQL query used
  `last: 100` with DESC ordering, which returns the *oldest* merged
  PRs — so the v6.4 merge-author signals were computed from
  founding-era data on any repo with >100 merged PRs. Now `first: 100`.
- **Org-member proxy**: `?role=admin` is only honoured for
  org-member tokens; other callers silently got public members capped
  at 30. Public members are now counted explicitly so the signal is
  deterministic across tokens, with the proxy semantics documented.
- **T-1 historical leaks**: merge-author aggregates were computed at
  collection time with no cutoff awareness, leaking current-day data
  into historical (`--cutoff`) scores. The collector now stores the
  raw per-PR `(login, merged_at)` sample and re-derives aggregates from
  PRs merged before the cutoff. `COLLECTOR_VERSION` bumped to 2.
- **Score-row tagging**: a `--cutoff` run written inside the freshness
  window was served as the package's *current* score, and accumulated
  current rows masqueraded as a monthly-history series. Score rows now
  carry an `is_historical` tag; `get_current_score` and
  `get_historical_scores` filter on it, and `--cutoff` runs no longer
  touch `last_analyzed`.
- **Burnout-escalation cache loss**: `_rebuild_breakdown` dropped the
  burnout-escalation factor on every cache hit (the only protective
  factor not reconstructed from the breakdown JSON), so affected
  packages showed a breakdown that no longer summed to the stored
  score.
- **Tenure timezone drift**: `maintainer_account_created` is now
  normalized to naive UTC on both fresh-collect and cache-rehydrate
  paths; previously fresh collects produced tz-aware values that made
  the reputation scorer compute tenure to *today*, giving the same
  package different scores depending on cache state.
- **Cross-package error bleed**: repo-share cache hits no longer
  inherit the donor package's registry `fetch_errors` (which
  mislabeled the borrower `INSUFFICIENT_DATA`).
- **History misalignment**: `get_historical_scores` paired stored
  breakdowns with cutoff dates positionally, so one failed month
  misaligned every subsequent row. Breakdowns now travel with their
  score.
- **Rate-limit handling**: a 200 response with
  `X-RateLimit-Remaining: 0` is no longer discarded; only 403/429 are
  treated as blocking. Deleted-account (`"user": null`) issues no
  longer crash collection.
- **Token rotation**: rotate GitHub tokens on HTTP 401 as well as 403.

### Fixed — SBOM / support-period / surfaces

- **Annex VII support horizon**: an SBOM whose components all failed
  scoring returned the most favourable verdict from zero evidence; zero
  scored components now yields an explicit indeterminate verdict. A
  genuinely empty dependency set stays vacuously supportable.
- **purl parsing**: scoped npm purls without a version
  (`pkg:npm/@babel/core`) no longer mis-split into empty name; SPDX
  2.2 `PACKAGE_MANAGER` refs and `DESCRIBES` root relationships are
  now honoured.
- **API**: `max_age=0` ("force re-score") now persists the fresh score
  instead of computing it and discarding the write; ecosystem path
  params are lowercased to match CLI behaviour.
- **CLI `diff`**: reports containing `INSUFFICIENT_DATA` (null-score)
  rows no longer crash the formatters; data-state transitions are now
  displayed.
- **Concurrency**: git clone/log moved off the event loop so a large
  clone no longer freezes concurrent API requests (including
  `/health`).
- **Dashboard**: additional HTML-escaping at render sites; failed
  scoring results are no longer pinned in cache for the full hour;
  re-score feedback survives `st.rerun`.

### Added

- **`is_historical` column** on `scores`, with idempotent
  auto-migration that backfills the tag from the
  cutoff/calculated-date distance on existing databases.
- **LFX Insights comparison benchmark** (`scripts/lfx_comparison.py`,
  20 packages) with a saved baseline, for external cross-check of
  governance scoring against Linux Foundation Health Score.

### Changed

- Methodology and validation surfaces reconciled to v6.4 / n=184
  (64 incidents + 120 controls), with drift tests pinning every
  published number to `validation_results.json`.

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
