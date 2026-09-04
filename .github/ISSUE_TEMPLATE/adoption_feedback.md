---
name: User workflow feedback
about: Share what happened when you tried ReproMin, even without a public failure
title: "[Feedback] "
labels: documentation,help wanted
assignees: ""
---

Use this template for a real evaluation of ReproMin when you want to report
value, friction, or a compatibility boundary. Use the [bug report](bug_report.md)
for a reproducible defect, the [feature request](feature_request.md) for a
specific proposed behavior, and the [real-failure pilot](real_failure.md) when
you can share a sanitized CI or dependency failure and its reduction evidence.

Before posting, remove credentials, private URLs, proprietary source, customer
data, and raw logs. The host backend executes commands directly and is not a
security sandbox.

## Workflow

- What were you trying to reduce or make easier?
- Language, build/test system, and runner OS:
- Repository shape (optional):

## Run

- Outcome (`useful`, `inconclusive`, or `could not run`), and where you stopped:
- ReproMin version and install source:
- Optional evidence: backend, adapter/reducer, sanitized oracle type, aggregate
  before/after sizes, or reviewed scalar fields from `repomin doctor`, `report
  validate`, and `report replay` results:

For a path-free validation summary to paste here, pass `--format markdown` to
`repomin report validate`. The JSON forms are useful for automation but include
local report, payload, source, output, or metadata paths depending on the
command; review and redact them before sharing. Validation output omits the
reproduction command, match expression, logs, and environment values. Review
the payload separately before sharing it.

## Value and friction

- What became easier, smaller, or more reproducible, if anything?
- What was the main confusing, slow, or incompatible step?
- What one change would make you try ReproMin again?

## Privacy and redistribution

- [ ] Secrets, credentials, private URLs, customer data, proprietary source, and
      confidential logs were removed.
- [ ] The remaining description may be used to improve documentation,
      compatibility notes, or a public benchmark.
- [ ] I understand that replay and holdout results are current-environment
      oracle evidence, not correctness or production-reliability guarantees.

## Additional context
