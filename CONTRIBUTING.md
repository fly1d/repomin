# Contributing

Thanks for helping improve ReproMin. This guide keeps the first contribution
path short. Detailed reducer, sampling, persistence, adapter, and compatibility
rules live in the [engineering contracts](docs/ENGINEERING_CONTRACTS.md).

All participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
Never post credentials, proprietary source, private logs, or unpatched security
vulnerabilities. Follow [SECURITY.md](SECURITY.md) for private reports.

## Choose the right place

- Ask usage and troubleshooting questions in [GitHub Discussions][discussions].
- Share results and workflow feedback in [Show and tell][show-and-tell].
- Use an issue template for reproducible bugs, focused improvements, and real
  failure pilots. Benchmark proposals use the improvement template.
- Start from a scoped task in [good first issues](docs/GOOD_FIRST_ISSUES.md) when
  making a first contribution. Comment before starting so overlapping work is
  visible.
- Follow [SUPPORT.md](SUPPORT.md) when unsure where a report belongs.

Maintainers preparing a release should use the separate
[release process](docs/RELEASING.md).

## Community communication

Lead with the conclusion, then include complete decision context: every fact
needed to reproduce the behavior, assess impact and risk, and choose the next
action. Leave out history or raw output that cannot change the decision. Keep
the canonical issue, discussion, or pull request body current instead of adding
routine status comments. A short message may link to detailed evidence; it must
not omit information needed to act.

Contributor messages cover:

- **Requested outcome:** the decision or change you are asking for.
- **Context and impact:** who is affected, the current behavior, and why it
  matters.
- **Evidence and validation:** a minimal reproduction, relevant output, and the
  checks already run.
- **Done when:** observable acceptance criteria and any remaining boundary.

These are questions to answer, not headings that every message must repeat.
Combine them when one concise paragraph carries the same decision context.

Use `N/A` and explain why when a detail is unavailable or does not apply. Do not
guess or leave a required field ambiguous.

A re-review request states what changed since the last review, the new evidence,
and any remaining blocker. Reviewers should not have to reconstruct that state
from earlier comments.

Maintainer replies cover:

- **Decision:** accepted, needs changes, deferred, declined, or another explicit
  outcome.
- **Reason:** the constraint or evidence that determines the decision.
- **Next step:** the specific action required, including relevant files or tests.
- **Owner / done when:** who acts next and the observable completion condition.

A concise maintainer reply can use:

```text
Decision:
Reason:
Next step:
Owner / done when:
```

For a deferred or declined request, state whether new evidence could change the
decision and what that evidence would need to show.

Do not reply with only a link or `please update`. State the deciding constraint
in the reply, then link deeper material when it helps.

When facts change, update the canonical body or acceptance criteria. Add a new
comment only for a decision, new evidence, a blocker, or a handoff.

## Before coding

1. Read the issue's requested outcome and completion criteria.
2. For a new fixture, run `repomin doctor` first; see the
   [Doctor guide](docs/DOCTOR.md).
3. Confirm the intended scope in the issue before making a broad or
   compatibility-sensitive change.
4. Read the [engineering contracts](docs/ENGINEERING_CONTRACTS.md) for any area
   your change touches. Those contracts are review requirements, not optional
   background.

ReproMin's main extension points are manifest adapters, failure oracles, source
reducers, execution backends, and public offline benchmarks. Prefer a focused
change with focused tests over an unrelated cleanup.

## Implement and test

From the repository root, run the standard contributor preflight:

```sh
python3 scripts/check_contribution.py
```

It checks documentation, Ruff rules, byte-compilation, and the complete unit
test suite without modifying tracked source files. Install Ruff first when it
is unavailable (`python3 -m pip install ruff`).

For a documentation-only change, this narrower check is available:

```sh
python3 scripts/check_docs.py
```

If you use `--skip-lint` or another check cannot run, say so in the pull request
and explain why. Changes to an offline fixture or benchmark also require:

```sh
python3 scripts/check_contribution.py --with-benchmarks
```

The benchmark suite is network-free and may skip fixtures whose optional
toolchain is not installed. Focused test commands, coverage steps, benchmark
requirements, and subsystem-specific test matrices are documented in the
[engineering contracts](docs/ENGINEERING_CONTRACTS.md#verification-contracts).

## Pull request checklist

Before requesting review:

- Keep the pull request focused and link its issue or discussion.
- Explain why the change is needed, what changed, its user impact and risks,
  and how it was validated.
- Add or update tests for behavior changes, including rejection and failure
  paths required by the engineering contracts.
- Update user documentation and the changelog when public behavior changes.
- Disclose material LLM, agent, or automation involvement accurately.
- Remove secrets, private source, machine-specific paths, and confidential logs.
- Mark unavailable evidence as `N/A` with a reason.

A pull request is ready to merge when its requested outcome and acceptance
criteria are met, required checks pass, documentation matches behavior, and no
review blocker remains.

## Benchmarks and external projects

Agree on the oracle contract before implementing a benchmark. A fixture must be
self-contained, deterministic, network-free, small enough for CI, documented,
and covered by the offline benchmark regression. Follow the full
[benchmark contract](docs/ENGINEERING_CONTRACTS.md#adding-a-benchmark).

A public fixture or case study does not authorize contacting another project.
Do not use automated cold outreach. Contact another project only after a human
has checked its contribution and AI-assistance policies and the recipient has
opted in. Disclose material agent involvement in the first message, and stop if
the project declines or asks you not to continue. Never claim adoption or
usefulness without an explicit response. The complete rule and its rationale
are in the
[cross-project outreach contract](docs/ENGINEERING_CONTRACTS.md#cross-project-outreach).

[discussions]: https://github.com/fly1d/repomin/discussions/new?category=q-a
[show-and-tell]: https://github.com/fly1d/repomin/discussions/new?category=show-and-tell
