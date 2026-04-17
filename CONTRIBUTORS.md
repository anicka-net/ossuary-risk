# Contributors

This file records who has contributed to Ossuary and in what capacity.
It exists for two reasons: ordinary credit, and the thesis attribution
requirement explained in [`CONTRIBUTING.md`](CONTRIBUTING.md).

If you have contributed and prefer a different name or no listing, open
an issue or note it in your PR.

## Author

- **Anna Maresova** — author and maintainer. Scoring formula, methodology,
  validation framework, dataset curation, all thesis material. All areas of
  the codebase.

## AI co-authors

Declared in the project README and in commit `Co-Authored-By` trailers.
AI assistance is treated as supportive, not authoritative; final
responsibility for all decisions rests with the author.

- **Claude Opus** (Anthropic) — implementation pairing, code review, drafting
  of working notes and analysis scripts. See `git log --grep="Co-Authored-By: Claude"`.
- **GPT / Codex** (OpenAI) — code review, identification of historical-scoring
  leakage (commit `03049a5`) and other correctness issues. See
  `git log --grep="Co-Authored-By: .*GPT\|Codex"`.

## Outside contributors

*(none yet — first entries land here when external PRs merge)*

<!--
When merging an external PR, add an entry like:

- **Name** (Affiliation, optional) — area: short description of what they
  contributed. First contribution: PR #N (YYYY-MM).
-->
