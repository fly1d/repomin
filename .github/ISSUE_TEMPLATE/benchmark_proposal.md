---
name: Offline benchmark proposal
about: Propose a deterministic fixture for a missing reducer workflow
title: "[Benchmark] "
labels: enhancement
assignees: ""
---

## Requested outcome

<!-- Ask maintainers to confirm the fixture and oracle scope before implementation. -->

## Context and impact

<!-- Explain the real failure or workflow and why current fixtures do not cover it. -->

- Source workflow:
- Missing coverage:
- Build system or language:

## Evidence and validation

<!--
Describe a deterministic, network-free oracle. State what identifies the target
failure, what near-match must be rejected, required local tools, and the expected
minimal payload. Do not include the completed fixture at proposal time.
-->

- Proposed command and target failure signal:
- Different failure that must be rejected:
- Required tools and versions:
- Expected minimized payload:
- Why the fixture can run without network access:

## Done when

- [ ] Maintainers agree on the oracle and fixture boundary.
- [ ] The eventual fixture has a focused regression test and a short README.
- [ ] `benchmarks/run_offline.py --only <name>` can verify it without network
      access.

## Safety

- [ ] The proposed material can be redistributed and contains no secrets,
      credentials, private URLs, proprietary source, customer data, or
      confidential logs.

## Other decision-relevant context (optional)
