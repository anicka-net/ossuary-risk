# ossuary Agent Contract Spec

This document is the machine-facing contributor contract for `ossuary`.
It defines how an AI contributor should inspect, change, verify, and
report work in this repository.

If this file conflicts with higher-priority repository policy, follow:

1. `AGENTS.md`
2. this file
3. `spec/prompt.md`
4. the current repository state

## 1. Scope

This contract governs work across the repository's public surfaces:

- `src/ossuary/` for package, CLI, API, dashboard, and scoring behavior
- `docs/` for methodology and validation claims
- `scripts/` for validation and analysis workflows
- validation JSON outputs and comparable generated artifacts in the repo root
- `README.md` and thesis-facing summaries for public framing

This is not just a code repository. It also contains empirical claims.

## 2. Source Of Truth

For agent behavior, the source of truth is:

1. `README.md`
2. `AGENTS.md`
3. `docs/methodology.md`
4. `docs/validation.md`
5. this contract
6. `spec/prompt.md`
7. the current repository state

Agents must not import assumptions from other repositories or stale chat
context when the current repository says otherwise.

## 3. Required Work Phases

All non-trivial work follows this order:

1. Inspect
2. Decide
3. Change
4. Verify
5. Report

### Phase 1: Inspect

Before editing:

- inspect the current repository state
- read the files that define the touched behavior
- identify whether the task touches code, methodology, validation, docs,
  dashboard text, thesis-facing claims, or a mix
- identify whether any public count, score, or claim may move

### Phase 2: Decide

Before changing files, determine:

- which stable interfaces might move
- what exact tests or commands will verify the change
- whether documentation updates are required in the same workstream
- whether the task involves measured claims, estimates, or unavailable data

Prefer explicit tradeoffs over silent fallback.

### Phase 3: Change

While editing:

- keep diffs small and reviewable
- preserve deterministic behavior where practical
- prefer surfacing uncertainty over hiding it
- do not silently relax validation to make outcomes look better
- do not let docs drift away from code or generated artifacts
- do not change thresholds or methodology text casually

### Phase 4: Verify

Verification is mandatory.

| Change type | Minimum verification |
|---|---|
| CLI/API behavior | affected command path or targeted tests |
| scoring logic | targeted scoring tests; check docs for consistency |
| validation scripts/results | rerun affected validation path or explain why not |
| dashboard methodology/validation text | compare against current docs and artifacts |
| docs-only | no code tests required, but factual consistency must be checked |

Baseline suite:

```bash
python3 -m pytest -q tests
```

### Phase 5: Report

Final reporting must include:

- what changed
- what was verified
- any residual uncertainty or unverified path

For reviews, findings come first. For implementation work, outcome comes first.

## 4. Stable Interfaces

These are hard boundaries:

1. CLI contract: documented `ossuary` command behavior
2. API contract: documented endpoint shapes and semantics
3. Scoring contract: factor meanings, score interpretation, and limitations
4. Validation contract: scripts, result structure, and reported metrics
5. Public claims contract: README, docs, dashboard, and thesis-facing text

Changes that affect any stable interface require targeted verification.

## 5. Academic Honesty Rules

This repository supports academic work and empirical claims.

Agents must:

- distinguish facts from interpretations
- distinguish observed historical data from present-day proxies
- distinguish reproducible measurements from exploratory notes
- keep limitations visible when data are incomplete
- preserve citation and provenance discipline when asked to summarize sources
- state uncertainty explicitly when a claim cannot be fully verified

Agents must not:

- invent citations, references, datasets, incidents, or result values
- generate thesis prose that pretends to be independently sourced
- convert estimated or proxy values into factual historical claims
- overstate detection scope beyond the documented methodology
- suppress contradictory evidence just because it complicates the story

## 6. VSE-Aligned GenAI Discipline

The VSE guidance is clear on three points that apply here:

- responsibility for the output remains with the student
- GenAI use should be transparent when it materially shapes written work
- outputs must be critically verified rather than accepted at face value

In this repository, agents should therefore:

- treat generated text as draft assistance, not authority
- avoid writing uncited literature or source summaries as if they were checked
- preserve enough context for the human author to review and disclose
  material AI assistance appropriately

## 7. Required Documentation Updates

When behavior or claims change, update the repository-visible documents that
define them if they would otherwise drift:

- `README.md` for user-facing behavior and project framing
- `docs/methodology.md` for scoring semantics and limitations
- `docs/validation.md` for validation counts, metrics, and interpretation
- dashboard methodology pages when they restate these claims
- `AGENTS.md` / `spec/` when contributor process changes

## 8. Non-Goals

This contract does not authorize claim inflation.
It does not replace human review.
It does not allow confidence to substitute for verification.
