# Roadmap

ReproMin is a pre-alpha project. This roadmap describes priorities, not release
dates. Concrete contribution tasks live in
[GOOD_FIRST_ISSUES.md](GOOD_FIRST_ISSUES.md).

## Delivered foundation

- Fail-closed oracle matching, repeated sampling, run-wide confidence control,
  final holdout certification, and auditable reports.
- Persistent checkpoint/resume sessions and host/Docker execution on Linux,
  macOS, and Windows.
- Structured manifest reduction across Maven, Gradle, Python/Pipenv, Node,
  Composer, MSBuild, Bundler, Cargo, and Go.
- Native Java and Python source reducers, an opt-in HTTP semantic reducer, and
  explicit text-file reduction.
- Network-free regression benchmarks, branch coverage artifacts, installable
  wheel/source-distribution tests, issue templates, and contributor guidance.
- A requirements-chain benchmark covering nested includes, constraints, and
  hash-pinned CI dependencies, with an independent oracle check.
- A dependency-free contributor preflight that isolates lint and bytecode
  caches, plus release metadata and isolated wheel/source-install checks.
- A reusable GitHub Action with explicit output, exit-code, process-signature,
  holdout, and privacy-exclusion inputs, plus validated report summary outputs.
- A read-only `repomin doctor` preflight for reducer/toolchain discovery, output
  collision checks, and optional fresh-copy baseline verification.
- A privacy-conscious `repomin report replay` workflow with exact modern
  failure contracts, payload fingerprints, and isolated fresh-copy samples.
- A structured real-failure issue template and a short claim-to-PR workflow for
  contributors who can share sanitized CI or dependency failures.

## Current: contributor feedback

- Keep a small, accurate set of good-first issues with explicit acceptance
  criteria and maintainers' scope notes.
- Collect reproducible reductions from real repositories and turn failures into
  minimized public fixtures when licensing permits.
- Tighten benchmark report assertions and publish comparable trend summaries
  without making unsupported performance claims.
- Keep the dependency-free `benchmarks/compare.py` summary comparison useful
  for fixture regressions and environment-to-environment diagnostics.
- Improve runnable Java, Python, Docker, and semantic workflow examples.
- Make the [real failure pilot guide](REAL_FAILURE_PILOT.md) easy to discover
  so sanitized user workflows can be turned into fixtures and compatibility
  notes.
- Collect successful, inconclusive, and blocked trial feedback through the
  user-workflow template, then turn repeated friction into focused examples,
  compatibility notes, or starter issues.

## Next: release readiness

- Resolve feedback from the GitHub development release before choosing the next
  version or publishing channel.
- Keep the repeatable [release checklist](RELEASING.md) current as artifact and
  verification workflows evolve.
- Define stable adapter/exporter interfaces only after at least two independent
  integrations need the same contract.
- Add optional report exporters and remote execution boundaries without
  changing the core reduction/oracle acceptance model.
- Evaluate additional language analyzers only when they preserve hashed,
  parser-backed edits and deterministic rediscovery.

## Explicit non-goals

- Replacing a build system, test runner, or dependency resolver.
- Claiming code correctness from a passing oracle or holdout result.
- Running untrusted commands as a security sandbox on the host backend.
- Adding network-dependent tests to the offline benchmark suite.

Roadmap changes should be discussed in an issue or pull request and should
include the user workflow, oracle contract, test fixture, and documentation
impact.
