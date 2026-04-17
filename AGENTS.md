# AI Agent Contract

This repository accepts AI agent contributions. This document is the
public gateway contract for how agents should operate here.

Ossuary is both software and academic work. Agents must optimize for:

1. correctness of code and analysis
2. honesty of public and thesis-facing claims
3. reproducibility of scoring, validation, and generated artifacts
4. small, reviewable changes

## 0. Onboarding

Before making substantial changes, read these files in order:

1. `README.md`
2. `AGENTS.md`
3. `docs/methodology.md`
4. `docs/validation.md`
5. `spec/agent-contract.md`
6. `spec/prompt.md`
7. the files directly relevant to the task

If the task touches validation outputs, dashboard methodology pages, or
thesis text, inspect those files too before editing.

## 1. Decision Priority

When goals conflict, follow this order:

1. **Academic honesty** — no overstated claims, no fabricated evidence
2. **Stable interfaces** — CLI/API behavior, scoring semantics, published artifacts
3. **Task completion** — the user's request
4. **Correctness** — tests, validation, reproducibility, data integrity
5. **Quality** — clarity, maintainability, docs
6. **Initiative** — improvements beyond the task

Do not optimize a lower priority at the expense of a higher one.

## 2. Principles

- Be explicit about evidence. If something is measured, say so. If it is
  inferred, estimated, or incomplete, label it plainly.
- Keep code, docs, dashboard text, validation outputs, and thesis-facing
  claims aligned.
- Prefer reversible fixes over broad rewrites unless a rewrite is the task.
- Treat AI use as assistive, not authoritative. Final responsibility stays
  with the human author, and agent output must be reviewable.
- Credit substantial AI-authored work with `Co-Authored-By` trailers.

## 3. Hard Rules

Agents must always follow these rules:

- Do not fabricate references, metrics, incidents, package metadata, or
  validation outcomes.
- Do not present generated prose, summaries, or interpretations as cited
  sources.
- Do not insert uncited or unreviewed AI-generated thesis text into
  academic deliverables.
- Do not overclaim what the scoring model can detect; respect the stated
  detection boundary.
- Do not silently change methodology, thresholds, validation datasets, or
  reported counts without updating the corresponding docs in the same
  workstream.
- Do not commit credentials, API keys, private data, or local machine
  details.
- Do not overwrite another contributor's in-progress work.
- Do not treat unavailable historical data as if it were observed data.
- Do not round partial verification up into a stronger claim than was
  actually tested.

## 4. Stable Interfaces

These are repository contracts. Breaking them without deliberate review is
an error.

1. **CLI Contract**: the `ossuary` commands and their documented behavior
2. **API Contract**: the FastAPI endpoints and their response shapes
3. **Scoring Contract**: public methodology, score interpretation, and
   factor semantics described in `docs/methodology.md`
4. **Validation Contract**: validation scripts, datasets, and reported
   summary metrics must remain internally consistent
5. **Public Claims Contract**: `README.md`, `docs/`, dashboard methodology
   pages, and thesis-facing material must not disagree on scope, counts,
   validation, or limitations

## 5. Definition Of Done

A non-trivial change is done when:

- the affected behavior was verified, or the exact unverified path is stated
- stable interfaces are preserved or intentionally updated
- docs are updated if behavior, methodology, counts, or claims changed
- no claim in the repository is stronger than the evidence now supports
- the change is safe to review and merge

## 6. Verification Expectations

Minimum verification depends on the change:

- CLI/API behavior: run the relevant command path or targeted tests
- scoring logic: run the targeted scoring tests and inspect the affected
  methodology text for consistency
- validation logic or reported results: rerun the relevant validation path
  or state exactly why it was not rerun
- docs/thesis-facing claims: cross-check wording against the current code,
  validation artifacts, and source documents
- docs-only: code tests are not required, but claims must match the repo

Baseline test suite:

```bash
python3 -m pytest -q tests
```

If verification could not be run, say exactly what could not be run and why.

## 7. Academic Honesty Rules

This repository supports academic work. Agents must protect that.

Required behavior:

- distinguish observed data from estimates and proxies
- distinguish current-state scoring from historical reconstruction
- distinguish measured validation results from interpretation
- preserve source traceability for nontrivial factual claims
- keep AI assistance transparent where it materially affected the work
- keep thesis text the author's own unless the user explicitly asks for
  drafting assistance and then reviews it critically

Explicitly forbidden:

- inventing citations or quoting sources not actually checked
- laundering AI output into “common knowledge”
- describing incomplete validation as conclusive
- treating unavailable historical values as reconstructed facts
- hiding material AI assistance where declaration is required

## 8. What Requires Extra Care

- edits to `docs/methodology.md`
- edits to `docs/validation.md`
- edits to validation scripts or validation result files
- edits to dashboard pages that restate methodology or validation
- changes to scoring factors, thresholds, or historical-scoring behavior
- edits to thesis-facing material or academic summaries

If one of these changes, update the corresponding public documentation in
the same workstream or state clearly why it remains unchanged.

## 9. Prompt Layer

For stricter agent wrappers, use:

1. `spec/agent-contract.md`
2. `spec/prompt.md`

Those files restate this contract in a more machine-oriented form.
