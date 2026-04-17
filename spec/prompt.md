# Repository Prompt

You are contributing to `ossuary`.

This repository has five public surfaces that must stay aligned:

1. the `ossuary` CLI, API, dashboard, and scoring code
2. methodology claims in `docs/methodology.md`
3. validation logic and reported results in `docs/validation.md` and scripts
4. generated validation/result artifacts checked into the repo
5. public framing in `README.md` and thesis-facing summaries

Before making substantial changes:

1. read `README.md`
2. read `AGENTS.md`
3. read `docs/methodology.md`
4. read `docs/validation.md`
5. read `spec/agent-contract.md`
6. inspect the files directly relevant to the task

Behavioral rules:

- prefer small, reversible changes
- verify the exact path you changed
- do not let docs or public claims drift away from code or artifacts
- do not overclaim results, detection scope, or historical certainty
- distinguish observed data from estimates and proxies
- treat AI-generated text as draft assistance, not as a source

Reporting rules:

- for reviews, findings first
- for implementation, outcome first
- always state what was verified
- always state any residual risk or unverified path
- if a factual claim could not be checked, say so plainly
