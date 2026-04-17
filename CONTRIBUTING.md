# Contributing to Ossuary

Thanks for considering a contribution. Ossuary is open to outside help, but it
has one unusual constraint you need to know about up front: it is also the
practical part of an MBA thesis at VŠE Prague (defense Feb 2027). University
rules require the *core* of the work to be the author's own, and any external
contribution to be (a) supportive rather than core and (b) attributed clearly
enough that examiners can tell who did what.

This document explains where that line falls, how to submit changes, and how
attribution is recorded.

If you are an AI agent, also read [`AGENTS.md`](AGENTS.md) and `spec/` first.
Those rules apply on top of this document.

## What is "core" and what is "supportive"

These are the parts of the repository that constitute the academic core. Outside
contributions to these areas are generally not accepted until after thesis
defense (Feb 2027). Bug reports, questions, and discussion are still welcome.

| Path | Why it is core |
|---|---|
| `src/ossuary/scoring/` | The scoring formula, engine, factors, and reputation logic are the thesis's main technical contribution. |
| `docs/methodology.md` | Public statement of the scoring methodology. |
| `docs/validation.md` | Public statement of validation results, scope, and limitations. |
| `scripts/validate.py` (labels and tiers) | The validation dataset — ground-truth labels and tier classifications — is currently inlined in this script. *Adding* new incidents with primary-source citations is supportive (see below); changing existing tier labels or methodology code in the same file is core. |
| `spec/`, `AGENTS.md` | Contributor and agent contract. |

These are the parts that are open to outside contribution:

| Path | Examples of welcome work |
|---|---|
| `src/ossuary/collectors/` | New ecosystem collectors, fixing rate-limit handling, improving cache behavior, supporting alternate registries (e.g. private mirrors). |
| `src/ossuary/api/` | New endpoints, request validation, OpenAPI polish. |
| `src/ossuary/dashboard/` | UI improvements, accessibility, additional pages, better error states. |
| `src/ossuary/cli.py` | New commands, output formats, ergonomic improvements, shell completion. |
| `src/ossuary/db/`, `migrations/` | Schema migrations, Postgres support, indexing. |
| `src/ossuary/services/`, `src/ossuary/sentiment/` | Caching, batch, integrations. Bug fixes welcome. Behavior changes that move scoring outputs are core — open an issue first. |
| `seeds/` (additions) | New batch-scoring lists (e.g. ecosystem-popular, project-stack). Each list is a curated package set, not a validation dataset. New lists welcome. |
| New incident catalog entries | Submit via PR to `scripts/validate.py` with at least one primary-source link (advisory, post-mortem, or news report) per incident. Tier classification will be reviewed by the author. |
| Distribution & packaging | openSUSE / Debian / Fedora packaging, container images, Helm charts, systemd units. |
| CI/CD | GitHub Actions, release automation, security scanning, SBOM generation. |
| `tests/` | Better coverage of any non-core area. Tests of scoring outputs are accepted but reviewed with care: a test that pins a numeric score acts as a methodology assertion. |
| `examples/` | Real-world dependency-tree case studies, integration recipes. |
| Documentation | Tutorials, deployment guides, ecosystem-specific notes. README copy-edits welcome. |

When in doubt about which side a change falls on, **open an issue first** and
ask. It is cheaper than reworking a PR, and it is the polite thing to do given
the academic constraint.

## How to submit a change

1. **Open an issue** describing the problem or the proposed change. For larger
   pieces of work (new collector, new dashboard page, packaging), please
   discuss the design before implementing.
2. **Fork and branch.** Branch names: `feature/...`, `fix/...`, `docs/...`.
3. **Keep PRs small and reviewable.** One conceptual change per PR.
4. **Run the tests.** `python3 -m pytest -q tests`. The default `[dev]`
   extra is enough; SPDX interop tests in `tests/test_sbom_spdx_interop.py`
   auto-skip unless you also install `[dev-spdx-interop]`, which pulls in
   the heavier `spdx-tools` library.
5. **Match the existing style.** Public-facing text in this repository is
   deliberately plain: short sentences, no marketing language, no emoji.
6. **Update docs in the same PR** if your change affects user-visible behavior.
7. **Sign off your commits** with `git commit -s` (Developer Certificate of
   Origin). This certifies you have the right to submit the contribution.

## Attribution

Attribution is taken seriously here, both as good practice and as a thesis
requirement.

- **`Co-Authored-By` trailers**: when an outside contributor or AI agent makes
  a substantive change, add the trailer to the commit. Existing examples are
  visible in `git log` (Claude Opus, GPT/Codex). Mechanical edits do not need
  trailers.
- **`CONTRIBUTORS.md`**: this file records who has worked on what. When your
  PR is merged, your name and the area of contribution will be added (or
  updated) there. If you would prefer a different attribution name, or no
  attribution, say so in the PR.
- **No CLA**. The DCO sign-off is sufficient.

## Honesty rules

These mirror `AGENTS.md` §3 and apply to humans too:

- Do not submit fabricated incidents, sources, metrics, or package metadata.
  Every new seed entry needs at least one primary-source link.
- Do not change methodology thresholds or validation results without explicit
  discussion in an issue first.
- Do not weaken acknowledged limitations to make the tool look better.
- If you used an AI assistant materially in your contribution, declare it in
  the PR description and add a `Co-Authored-By` trailer naming the model.

## License and review

The project is MIT-licensed. By contributing you agree that your contribution
will be released under the same license.

Reviews come from the author. Turnaround depends on thesis schedule — expect
slower review around kontrolní-den deadlines (June and September 2026) and
faster review afterwards.

## Questions

Open a GitHub issue, or reach out via the contact details on the project
homepage.
