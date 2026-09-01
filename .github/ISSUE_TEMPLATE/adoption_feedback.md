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

- Goal or job you wanted to improve:
- Language and build/test system:
- Repository shape (single project, monorepo, generated sources, or other):
- Runner OS and architecture:

## Run

- ReproMin version and install source:
- Backend (`host` or `docker`):
- Adapter and source/text reducer:
- Sanitized command and oracle type (`match`, `exit-code`, or `process-failure`):
- `repomin doctor` result (optional):
- Reduction outcome (`useful`, `inconclusive`, or `could not run`):
- Payload size before and after (files/bytes, optional):
- `report validate` result (optional):
- `report replay` result (optional):

For a compact, privacy-safe result to paste here, append `--json` to
`repomin report validate`; review the payload separately before sharing it.
The JSON intentionally omits the command, match expression, logs, and
environment values.

## Value and friction

- What became easier, smaller, or more reproducible?
- What was confusing, slow, or unexpectedly difficult?
- Which documentation, command, or output helped most?
- What one change would make you use ReproMin again?
- Known limitations or compatibility boundaries:

## Privacy and redistribution

- [ ] Secrets, credentials, tokens, private URLs, and customer data were removed.
- [ ] Proprietary source and confidential logs were removed.
- [ ] The remaining description may be used to improve documentation,
      compatibility notes, or a public benchmark.
- [ ] I understand that replay and holdout results are current-environment
      oracle evidence, not correctness or production-reliability guarantees.

## Additional context
