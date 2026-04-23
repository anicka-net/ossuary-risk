# Data reuse design — repo-anchored snapshot cache

**Status:** design proposal, 2026-04-23
**Owner:** Anna Maresova
**Scope:** v0.10 / pre-SUSE-pipeline work

---

## 1. Goal

Make Ossuary cheap enough to run on a SUSE-scale dependency graph
(tens of thousands of packages, repeated nightly) without exhausting
GitHub API rate limits or pipeline budget.

The scoring formula and methodology are not in scope. This is purely
about how we acquire, store, and refresh the raw data the formula
consumes.

**Non-goals:**

- A new scoring methodology
- Distributed cache or multi-tenant storage
- Replacing the existing `Score` cache (it stays — it caches the
  *output* of scoring, which is also worth keeping)

---

## 2. Why now

Two pressures converge on this:

**SUSE pipeline economics.** Even an enterprise GitHub subscription
won't carry a per-package full-history fetch over ~50k openSUSE
source packages. Per-package collection currently makes O(10–100)
GitHub calls (commits paginated, contributors, issues, PRs, releases,
sponsors). Naive scaling: 50k × 50 calls = 2.5M calls per refresh
cycle, vs ~360k/hour ceiling on enterprise tokens. That's a
~7-hour minimum at perfect throughput, before retries, rate-limit
backoff, or non-GitHub registries. Daily refresh is not feasible
without aggressive reuse.

**Thesis chapter currently missing.** Ch. 5 demonstrates
correctness; nothing in the thesis demonstrates *operational
feasibility at CRA Art. 13(5) scale*. Examiners will reasonably ask
"how does this run when a manufacturer has 5,000 dependencies and an
audit cycle next month?" Without an answer, the methodology is
academic. With a snapshot-reuse design and measured reduction
numbers, this becomes a defensible operational contribution and a
clean discriminator vs Bitergia / LFX (neither publishes pipeline
economics).

---

## 3. Current state

**What exists today** (`src/ossuary/services/cache.py`,
`src/ossuary/db/models.py`):

- `ScoreCache` keyed on `(package, cutoff_date)` — caches *final
  scored output*. Working.
- `Commit` and `Issue` tables defined in models but **not written to
  by the current collector path**. Collectors return in-memory
  dataclasses (`CommitData`, `IssueData`) consumed directly by
  `services/scorer.py`. The DB tables are dead code from an earlier
  design.
- Cache freshness TTL: 7 days (env-configurable). Applies only to the
  Score row — does not gate any data fetching.

**What's missing:**

- No persistence of raw collected data. Every rescore =
  full re-fetch.
- Cache is package-keyed. Multiple ecosystem packages resolving to
  the same upstream repo (e.g. `axios` on npm and the
  `axios-http/axios` GitHub repo) cannot share data.
- No incremental fetch. Every refresh pulls full history, even when
  only the last day changed.
- No negative cache. Repeated probes for "no GitHub repo" / "404" /
  "private" return the same empty result every time.
- No freshness SLA documented in methodology — ambiguity about
  what "current score" means at audit time.

---

## 4. Architecture: three-layer cache

```
                ┌─────────────────────────────┐
                │  ScoreCache (existing)      │  ← cached final score
                │  key: (package, cutoff)     │     per cutoff_date
                └──────────────┬──────────────┘
                               │ depends on
                               ▼
                ┌─────────────────────────────┐
                │  PackageView                │  ← per-package derived
                │  key: (package, cutoff)     │     metrics
                └──────────────┬──────────────┘     (concentration,
                               │                    bus factor, etc.)
                               │ derived from
                               ▼
                ┌─────────────────────────────┐
                │  RepoSnapshot (new)         │  ← raw repo data,
                │  key: (canonical_repo_url,  │     repo-keyed,
                │        snapshot_date)       │     incrementally
                └─────────────────────────────┘     refreshed
```

**Why repo-keyed at the bottom:** `axios` on npm, `axios` on github,
`@axios/axios` (hypothetical scoped re-publish), and any vendored
fork all resolve to `axios-http/axios` upstream. The cost of
fetching that history is a property of the *repo*, not the package.
Package-keyed caching duplicates work whenever ecosystem and source
diverge.

**Why a separate PackageView layer:** package-level signals
(downloads, registry deprecation flag, npm/PyPI maintainer list)
*don't* live in the repo and need a per-package row. But they are
much cheaper than repo data and refresh on a different cadence
(npm download stats are weekly aggregates; commit data is
per-event). Keeping them separate avoids re-fetching one when only
the other moved.

---

## 5. Schema sketch

Three new tables, plus a small extension to `Package`:

```python
class CanonicalRepo(Base):
    """Upstream source repo, identified by normalized URL."""
    __tablename__ = "canonical_repos"

    id: int (pk)
    url: str (unique, normalized: lowercase, trailing-slash-stripped,
              .git-stripped, github.com/owner/repo form)
    host: str  # "github", "gitlab", "codeberg", ...
    last_synced_at: datetime  # max(snapshot.collected_at)
    last_known_default_branch: str
    is_dead: bool  # archived / 404 / private — sticky negative cache
    dead_reason: str  # "404", "archived", "redirected", "private"
    dead_checked_at: datetime  # for negative-cache TTL


class RepoSnapshot(Base):
    """Point-in-time blob of raw repo data."""
    __tablename__ = "repo_snapshots"

    id: int (pk)
    repo_id: fk -> canonical_repos
    collected_at: datetime  # server clock when the fetch ran
    coverage_until: datetime  # latest authored_date in the snapshot
                              # — this is what cutoff_date compares against

    # Raw blobs. JSON for now; can split into structured tables if
    # query patterns demand it (e.g. for per-author concentration
    # we'll likely want indexed commit rows).
    commits: JSON   # [{sha, author_email, authored_date, message}, ...]
    contributors: JSON
    issues: JSON
    releases: JSON
    sponsors: JSON
    repo_meta: JSON  # stars, archived flag, default branch, etc.

    # Provenance
    fetcher_version: str  # collector code version, for cache invalidation
                          # when the collector schema changes


class PackageRepoLink(Base):
    """Package → canonical repo resolution, with confidence."""
    __tablename__ = "package_repo_links"

    package_id: fk -> packages
    repo_id: fk -> canonical_repos (nullable — package may have no repo)
    resolved_at: datetime
    resolution_method: str  # "registry_metadata", "manual", "heuristic"
    confidence: str         # "verified", "likely", "speculative"
```

**Extension to `Package`:**

```python
class Package(Base):
    # ... existing fields ...
    last_repo_resolution_at: datetime
    no_repo_reason: str  # "registry_has_no_repo_field", "resolved_to_dead_repo"
                         # — negative cache for "this package has no upstream"
```

---

## 6. Refresh protocol

### 6.1 First fetch (cold)

1. Collector fetches everything (full commit history, all open + closed
   issues from issue tracker, contributors, releases).
2. Write a `RepoSnapshot` row with `coverage_until = max(authored_date)`.
3. Write a `PackageRepoLink` if the package resolved to a repo.

### 6.2 Refresh (warm)

For each repo with an existing snapshot:

1. Look up `last_synced_at` for the repo.
2. **Commits, issues**: fetch `since=last_synced_at` only.
3. **Contributors, releases, repo metadata**: cheap full re-fetch (one
   page each, generally).
4. **Sponsors**: separate API call, refresh on its own cadence (weekly
   is fine — sponsorship state doesn't move in days).
5. Merge new data into the most recent snapshot blob.
6. Write a *new* `RepoSnapshot` row (don't mutate the old one — we
   want point-in-time queryability for any historical cutoff).
7. Update `last_synced_at`.

The append-only snapshot history is what enables historical-cutoff
reuse: a query for `cutoff_date = 2026-03-30` reads the snapshot
with `coverage_until ≥ 2026-03-30` and the smallest `collected_at`
≥ that — i.e., the earliest snapshot that contains all data up to
the cutoff.

### 6.3 Negative caching

- **Repo-not-found / 404**: write `is_dead=true`,
  `dead_reason="404"`, set `dead_checked_at`. Re-probe after 90 days
  (TTL). Skip on every refresh in between.
- **Package-has-no-repo** (registry metadata gives no source URL):
  set `Package.no_repo_reason`, re-probe after 30 days. (Shorter
  than repo TTL because a package may grow a repo field later more
  often than a 404 repo will resurrect.)
- **Rate-limit failures**: do *not* negative-cache. Retry on the
  next run.

### 6.4 Cache invalidation

The only events that should invalidate cached data:

- **Collector code change**: `fetcher_version` mismatch in
  `RepoSnapshot` invalidates the row. New snapshot triggered.
- **Methodology version change**: invalidates the **`Score`** cache
  but **not** the `RepoSnapshot` cache. (A new formula reads the
  same raw data.) This is the big payoff: methodology iterations
  during research/thesis work no longer cost API calls.
- **Manual** `ossuary cache evict <package|repo>`.

---

## 7. Freshness SLA — methodology contract

Add to `docs/methodology.md` a new section ("Operational SLA") that
makes the freshness contract explicit:

> Ossuary scores reflect repository data **as of the most recent
> snapshot** for the package's canonical repo. Governance signals
> (bus factor, concentration, contributor attrition, organizational
> backing) are structural and move on the timescale of weeks to
> quarters, not hours — the freshness bands reflect that:
>
> - **Fresh** (≤ 30 days): green, suitable for routine audit-time
>   evidence. Aligned with typical release-boundary review cadence
>   under CRA Art. 13(5).
> - **Stale** (30–90 days): amber, still defensible for
>   point-in-time judgments but warns the operator. Re-score
>   recommended before formal sign-off.
> - **Expired** (> 90 days): red, score is informational only;
>   refresh required before relying on it for an attestation.
>
> Scores never silently use data older than 90 days for the
> "current" view. Historical-cutoff scores are exempt from the
> freshness contract by definition.
>
> **First-fetch carve-out.** A package with no prior snapshot is
> always fetched on demand — the SLA bands apply to *refresh
> latency*, not to first-time scoring of an unfamiliar dependency.
> An operator pasting a new package into the CLI gets a Fresh score
> immediately; the bands govern the cache hit path.

This converts "is this score current?" from an implicit question
into an explicit field on every output. CRA-grade due diligence
needs this — a scored package without a defensible freshness
timestamp is not audit evidence.

**Why these bands and not tighter.** Two reasons:

1. **Governance signals are structural.** A bus factor of 1 doesn't
   become a bus factor of 4 overnight; an organisation doesn't
   appear or disappear in days. Scoring on data ≤ 30 days old
   captures every meaningful change in the underlying signal class.
2. **Tight bands invite flapping.** Day-to-day rescore noise (rate
   limit retries returning different paginated slices, sentiment
   analyser variance, transient API errors) is observable on the
   current code path. Monthly SLA enforces that any score change
   between two refreshes reflects a real shift in the project, not
   a measurement artefact. If two refreshes 30 days apart give
   different scores on the same underlying state, that's a *bug to
   investigate* (see §9 open question 6), not noise the SLA should
   tolerate.

---

## 8. Migration path

The existing `Commit` / `Issue` tables in `models.py` are dead
code. They can either be repurposed or dropped:

- **Drop** them and add the new tables clean. Cleaner schema.
- **Repurpose** if the structured-row form turns out to be needed
  for indexed queries (e.g. per-author concentration on the
  database side, instead of in-Python over a JSON blob).

Recommendation: drop them initially, store everything in
`RepoSnapshot.commits` JSON. Promote to structured tables later if
profiling shows the JSON parse cost is real. (Premature
optimization to design for query patterns we don't have yet.)

**Validation script compatibility:** `scripts/validate.py`
currently calls `collect_package_data()` directly. Wrap that call
behind a `cached_collect()` helper that consults the snapshot
cache first and falls through to the collector on miss. Validation
gets faster transparently; no signature changes.

**Rollout sequence:**

1. Schema + migration (Alembic or manual).
2. `cached_collect()` wrapper. Snapshot writes on every call.
3. Wire validation through it. Confirm a second validation run
   reuses cache (should be ~minutes, not ~hour).
4. Add the freshness SLA documentation + per-score freshness
   field on `Score`.
5. Add `since=` incremental fetching for repo refresh.
6. Add negative caching for dead repos.
7. **Measure**: single-package score from cold cache vs warm cache,
   full validation re-run from cold vs warm, vs methodology
   change. These numbers go into the thesis operational section.

Steps 1–4 are the minimum viable cache and unblock everything
else. Steps 5–7 are the SUSE-scale features.

---

## 9. Open questions

1. **Registry data freshness.** Snapshot cache covers GitHub. What
   about npm/PyPI weekly download counts, registry deprecation flags?
   Probably a separate, shorter-TTL cache layered on the
   PackageView. Don't conflate with repo snapshots.

2. **Storage growth.** A snapshot per repo per refresh is
   append-only. At SUSE scale (~50k repos × daily refresh × N years)
   that grows unboundedly. Need a compaction policy: keep daily for
   N days, weekly for M months, monthly forever — or similar.
   Decide before SUSE pilot, not during.

3. **Repo identity edge cases.** Forks, mirrors, redirects (GitHub
   repo renamed). The canonical URL normalization is the chokepoint;
   need explicit rules for "what counts as the same repo" — probably:
   follow GitHub redirects, prefer the canonical URL the API
   resolves to, treat forks as distinct.

4. **Concurrent refresh.** Two scoring requests for the same
   package racing to refresh the same repo. Use a per-repo
   advisory lock (Postgres) or accept that we may waste an
   occasional fetch. Probably the latter for v0.10; revisit.

5. **GraphQL vs REST.** GitHub's GraphQL API is more efficient for
   the multi-resource fetch we do per repo. Worth a measurement —
   but separate from this design. The cache architecture above is
   API-shape-agnostic.

6. **Score-flapping investigation** (carried over from SLA
   discussion). We have observed score shifts between two runs of
   the same package on (apparently) the same underlying state.
   Likely culprits: sentiment analyser non-determinism,
   paginated-API result ordering, transient signal failures
   degrading to provisional scores. The monthly SLA is *intentionally*
   wide enough that an honest tool would not produce different
   scores within the band — so any observed flap inside 30 days
   under the new cache is a bug. Track as a follow-up once the
   cache is in place; the cache will make the bug observable.

---

## 10. Decisions

- **A.** *(approved)* Drop `Commit` / `Issue` tables, store as JSON
  on `RepoSnapshot`. Promote to structured tables only if
  profiling justifies.
- **B.** *(approved)* SLA bands set at **30 / 90 days** (Fresh /
  Stale / Expired) with a first-fetch carve-out. Rationale:
  governance signals are structural; tight bands invite flapping;
  CRA Art. 13(5) review cadence is release-boundary, not daily.
- **C.** *(approved)* Phased rollout — steps 1–4 first (schema +
  cached_collect + validation rewire + freshness SLA doc), then
  5–7 (incremental fetch, negative caching, measurement) once we
  have hard numbers from step 4 to justify them.
