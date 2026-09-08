---
name: Benchmark or real reproduction
about: Propose a deterministic fixture that improves reducer coverage
title: ""
labels: enhancement
assignees: ""
---

## User workflow

What real failure or reduction workflow does this fixture represent? Explain
why the existing fixtures do not cover it.

## Fixture scope

- Build system or language:
- Required local tools and versions:
- Network required: yes/no
- Expected minimized payload:

Fixtures in `benchmarks/` must be deterministic and network-free. Do not add
credentials, private source, or dependencies that cannot be redistributed.

## Oracle contract

What command is run, what output or exit behavior identifies the original
failure, and what different failure should be rejected?

## Acceptance criteria

- [ ] A self-contained fixture and `README.md` are included.
- [ ] `benchmarks/run_offline.py --only <name>` passes without network access.
- [ ] The expected payload and report invariants are asserted by a test.
- [ ] The benchmark documentation and changelog are updated.

## Additional context
