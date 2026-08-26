# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A runnable, network-free Go module reduction workflow in the examples guide,
  including its oracle output and expected minimized payload.
- GitHub issue configuration, a usage-question template, a benchmark proposal
  template, and a support guide to make community reports and first
  contributions easier to start.
- `repomin report validate` for dependency-free report schema, phase/holdout
  accounting, and optional certified payload fingerprint verification.
- A concise Chinese quick start covering installation, a runnable reduction,
  oracle semantics, host execution risk, and report locations.
- PowerShell completion generation through `repomin completion powershell`,
  including enum-value suggestions for the structured adapter and backend
  options.
- Shared `Directory.Build.props` reduction in the MSBuild adapter, reusing the
  hardened XML parser and stable subtree identity used for project files, with
  property groups, imports, conditions, and arbitrary metadata preserved.
- A network-free `dotnet-directory-build-props` benchmark and MSBuild adapter
  documentation coverage.
- A dependency-free benchmark summary comparison tool for aligning fixture
  status and descriptive duration trends across runs.
- Runnable Docker-backend and local semantic-stub workflows in the examples
  guide, including their trust-boundary and report checks.
- A documented Docker-only FastAPI dependency-regression fixture with an
  end-to-end reducer command and expected minimized payload.
- Structured Pipenv `Pipfile` reduction for direct runtime, development, and
  interpreter requirement entries, with source settings and `Pipfile.lock`
  deliberately preserved.
- A network-free `pipenv-package` benchmark and Pipenv CLI/documentation
  coverage.
- A network-free `python-pyproject` benchmark covering supported dependency
  declaration forms in `pyproject.toml`.
- A `benchmarks/run_offline.py --list` mode for discovering fixtures without
  executing the benchmark suite.
- Repeatable `--only` and `--exclude` filters for running a focused benchmark
  subset with strict unknown-name and empty-selection validation.
- Benchmark JSON summaries now record the exact filters and selected fixture
  names, and comparison output preserves that selection metadata.
- `benchmarks/compare.py --require-same-selection` can enforce identical
  benchmark filters across repeated summary comparisons.
- `REPOMIN.md` now records the execution backend and Docker image/network
  context needed to reproduce the same execution boundary without exposing
  configured environment values.
- Added a versioned [report schema guide](docs/REPORT_SCHEMA.md) covering
  machine-readable fields, phase accounting, holdout evidence, and consumer
  limitations.
- Optional machine-readable offline benchmark summaries with per-fixture status
  and elapsed time, uploaded as a CI artifact.
- `repomin completion bash|zsh|fish` for shell-native option, enum, and path
  completion without adding runtime dependencies.
- Opt-in `--semantic-reducer http` seam with a provider-agnostic
  OpenAI-compatible backend, default-off and with no third-party runtime
  dependency.
- Opt-in `--text-file RELATIVE_PATH` reducer for line-level shrinking of
  explicitly selected UTF-8 text files.
- Repeatable input controls: recursive gitignore, exact `--keep`, exact
  `--exit-code`, `--max-attempts`, and `--max-duration`.
- `benchmarks/run_offline.py` regression runner and an `offline-benchmarks` CI
  job.
- `input-controls`, `semantic-stub`, and `text-lines` benchmark fixtures.

### Changed

- Fixed report validation to accept the one-based contiguous holdout sample
  indexes emitted by certified reduction reports.
- Report validation now checks holdout outcome, timeout/resource, duration, and
  observation-digest evidence when those fields are present.
- Report validation now cross-checks holdout timeout, resource-exhaustion, and
  interruption aggregates against complete sample records.
- Report validation now checks holdout terminal statistic types, completeness,
  and observed-rate accounting.
- Benchmark and report numeric validation now turns oversized integer values
  into ordinary validation errors instead of leaking `OverflowError` tracebacks.
- Offline benchmark fixtures now validate their generated report and payload
  fingerprint before independently rerunning the oracle.
- Report validation now checks optional event probabilities, candidate-family
  evidence, and boolean early-acceptance state for finite, consistent values.
- Report validation now checks event records and rejects impossible oracle
  pass/run counts in the audit trail.
- Report validation now checks holdout sample structure, contiguous indexes, and
  pass-count accounting instead of validating only the sample list length.
- Generated `REPOMIN.md` commands now use a safe Markdown fence when the command
  itself contains backticks.
- Packaging CI now verifies that wheel and source-distribution entry points
  report the expected installed version, matching the release checklist.
- Added pytest configuration for the `src/` layout and repository test boundary,
  so `pytest -q` does not collect benchmark fixture tests accidentally.
- Refreshed the contributor roadmap and published scoped good-first issues for
  PowerShell completion, a Chinese quick start, and shared MSBuild props.
- Updated GitHub Actions to their Node.js 24-based major versions.
- Explicit `--keep` paths now fail before baseline execution when the path is
  missing or is not a regular file/directory; source and output path errors now
  include the next corrective action.
- Updated the Maven benchmark acceptance envelope for
  `hierarchical-fixed-point-v2`.
- Recorded `text_files` in reports and persistent-session identity.
- Stabilized Windows process, text, Java classpath, and test-fixture behavior;
  Windows is now a required CI matrix lane.

## [0.1.0.dev1] - 2026-08-24

Cross-platform pre-alpha feasibility build with semantic and text reducers,
persistent sessions, holdout certification, offline benchmarks, and contributor
documentation.

## [0.1.0.dev0] - Unreleased

Initial pre-alpha feasibility build.
