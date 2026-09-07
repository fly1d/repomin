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
- A reusable GitHub Action with explicit output, exit-code, Java/Python
  exception-signature, process-signature, holdout, and privacy-exclusion
  inputs, plus validated report summary outputs.
- A strict `schema_version: 1` JSON reduction specification shared by the CLI,
  Doctor, and GitHub Action, with explicit field ownership, portable Action
  paths, and fail-closed override and forward-compatibility rules.
- A read-only `repomin doctor` preflight for reducer/toolchain discovery, output
  collision checks, and optional fresh-copy baseline verification.
- A privacy-conscious `repomin report replay` workflow with exact modern
  failure contracts, payload fingerprints, and isolated fresh-copy samples.
- A privacy-safe `repomin report compare` workflow for ordered, descriptive
  comparison of validated reduction evidence, with context warnings and no
  payload or command execution.
- A structured real-failure issue template and a short claim-to-PR workflow for
  contributors who can share sanitized CI or dependency failures.

## Current: independent workflow validation

The current north star is five non-maintainer, real failure workflows. Demo
runs, synthetic benchmarks, stars, asset downloads, CI clones, and contributor
pull requests do not count. The initial adoption focus is repeatable
Maven/Gradle and Python test failures where a maintainer has asked for a small
reproduction.

- Freeze new reducers and adapters for at least two weeks after the development
  release. Fix only blockers observed in a real trial.
- Help users run Doctor, establish a strict oracle, complete a bounded
  reduction, and decide whether the artifact was actually useful in an issue or
  regression test.
- Record the funnel from invited participant through demo, Doctor, baseline,
  reduction, artifact use, and repeat use. Successful, inconclusive, and blocked
  trials all count as feedback, but not as successful adoption.
- Seek at least three user-confirmed useful results, two artifacts used by a
  recipient or test suite, and one repeat user or public downstream integration.
- Implement product work only after the same blocker appears in at least two
  independent trials. If most trials fail at oracle design, prioritize an
  `init` or capture workflow instead of another reducer.
- Use the [tsdown](CASE_STUDY_TSDOWN_979.md) technical pilot to improve oracle
  design and the
  [pydoctor outreach postmortem](CASE_STUDY_PYDOCTOR_728.md) to enforce
  authorship disclosure and recipient boundaries. The pydoctor artifact was
  not adopted and is not evidence of community value.
- Do not use automated cold outreach. Contact another project only after a
  human has checked its policies and the recipient has opted into the pilot;
  disclose material agent involvement in the first message.

After five independent trials, continue the current direction only if at least
three users report concrete value. If none do, narrow or change the product
before investing in more reduction capabilities.

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
