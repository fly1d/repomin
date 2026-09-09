---
name: User workflow feedback
about: Share value or friction from a real trial without filing a bug
title: "[Feedback] "
labels: documentation,help wanted
assignees: ""
---

<!--
Use this for experience feedback, not a reproducible product defect. Choose the
bug report for incorrect behavior and the real-failure pilot when you want a
maintainer to evaluate or reduce a sanitized failure.
-->

## Requested outcome

<!-- Say what response would help: acknowledgement, documentation change, or compatibility investigation. -->

## Context and impact

- Goal and workflow:
- Outcome (`useful`, `inconclusive`, or `could not run`):
- What became easier, smaller, or more reproducible:
- Main confusing, slow, or incompatible step:
- Language, build/test system, and runner OS:
- ReproMin version and install source:

## Evidence and validation

<!--
Share aggregate, sanitized evidence only. `repomin report validate --format
markdown` creates a path-free summary. JSON output can contain local report,
payload, source, output, or metadata paths; review and redact it before sharing.
Review the payload separately. Validation output omits commands, match
expressions, logs, and environment values.
-->

- Sanitized command shape (optional):
- Backend, adapter, or reducer:
- Before/after files or bytes:
- Relevant `repomin doctor`, `report validate`, or `report replay` result:
- What you already tried:

## Done when

- One change that would make you try ReproMin again:
- Follow-up you want from maintainers:

## Safety and reuse

- [ ] I removed secrets, credentials, private URLs, customer data, proprietary
      source, and confidential logs.
- [ ] The remaining description may be used to improve public documentation,
      compatibility notes, or benchmarks.
- [ ] I understand replay and holdout results are current-environment oracle
      evidence, not correctness or production-reliability guarantees.

## Other decision-relevant context (optional)
