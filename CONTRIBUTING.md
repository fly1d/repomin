# Contributing

New contributors should start with [docs/EXAMPLES.md](docs/EXAMPLES.md) to see
the tool in action, then pick a scoped GitHub issue from
[docs/GOOD_FIRST_ISSUES.md](docs/GOOD_FIRST_ISSUES.md). Comment on the issue
before starting so overlapping work stays visible. Bug reports, usage questions,
feature requests, and benchmark proposals use the repository issue templates.
Support guidance is collected in [SUPPORT.md](SUPPORT.md), and all participation
is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

When diagnosing a new fixture, run `repomin doctor` first (see
[docs/DOCTOR.md](docs/DOCTOR.md)) to confirm the command and selected adapter
before adding reducer or benchmark changes.

Maintainers preparing a GitHub Release should follow
[docs/RELEASING.md](docs/RELEASING.md). It is intentionally separate from the
contributor rules because release artifacts require version, checksum, and
installation verification.

ReproMin is intentionally organized around small extension points:

- manifest adapters for Maven, Gradle, Python/Pipenv, npm, Composer, MSBuild,
  Bundler, Cargo, Go, and additional build systems;
- failure oracles for stack traces, test reports, crashes, and flaky failures;
- source reducers backed by parser or compiler syntax trees;
- execution backends for containers and remote workers;
- public benchmark repositories with known reduction targets.

## Contributor preflight

From the repository root, run the standard local checks with one command:

```sh
python3 scripts/check_contribution.py
```

The preflight checks Markdown files for valid UTF-8, LF line endings, and paired
fenced code blocks, then runs Ruff, byte-compilation, and the complete unit-test
suite without changing tracked source files. The repository's `.gitattributes`
also asks Git to check out common text files with LF endings; the explicit check
catches files imported with a different encoding or line ending. Its Python and
Ruff caches are cleaned up from a temporary directory after each run. The
documentation check can also be run by itself:

```sh
python3 scripts/check_docs.py
```

Install Ruff first when it is not already available
(`python3 -m pip install ruff`). For a documentation-only change, `--skip-lint`
is available; explain skipped checks in the pull request. When a change adds or
updates an offline fixture, include the benchmark regression too:

```sh
python3 scripts/check_contribution.py --with-benchmarks
```

The benchmark command remains network-free and may skip fixtures whose optional
toolchain is not installed. The individual commands below are still useful when
diagnosing a failing check.

## Changing report comparison

The `repomin report compare` command is deliberately smaller than a benchmark
dashboard. Changes must keep the comparison read-only: validate each report,
never read payload contents, execute a recorded command, or contact a service.
Keep the output allow-list free of paths, commands, match expressions, logs,
environment metadata, signatures, fingerprints, and timing fields. Any new
field needs a documented privacy and compatibility reason, a deterministic
renderer test, and an explicit statement of whether it is numeric or
categorical context. Preserve the `descriptive_only` boundary and add a warning
when a changed execution or sampling condition can make adjacent values hard
to compare.

Run the focused checks while iterating:

```sh
PYTHONPATH=src python3 -m unittest tests.test_report_compare tests.test_cli -v
```

Use `docs/REPORT_SCHEMA.md` as the contract source and update the README,
examples, and changelog when the public comparison fields change. Performance
measurements belong in the dependency-free benchmark tooling, not in report
comparison.

## Adding a benchmark

Benchmarks are the most useful way to contribute a reproducible user workflow.
Start with the [benchmark proposal template](.github/ISSUE_TEMPLATE/benchmark_proposal.md)
and get agreement on the oracle contract before writing the fixture. Keep the
fixture self-contained, deterministic, network-free, and small enough to run
in CI. Add a fixture `README.md`, an expected minimized payload, and an
integration assertion in `tests/test_offline_benchmarks.py`. Run the focused
benchmark locally with `python3 benchmarks/run_offline.py --only <name>` and
include its output in the pull request description.

Changes to a reducer must include a test proving both sides of its contract:
the intended failure remains, and a different failure is rejected. Reducers
must never modify the input repository or overwrite an existing output path.
Tool-owned `report.json` and `REPOMIN.md` must remain in the sibling
`OUTPUT.repomin` metadata directory, never inside the exported payload. Tests
that exercise export or reporting must assert both paths and keep payload
file/byte accounting independent of metadata.

An enabled reducer must return at a local fixed point. Add a non-monotonic test
when one accepted edit can unlock an earlier rejected target; the global dirty
worklist relies on that local contract and only requeues components changed by
other reducers.

Hierarchical batching is an oracle-cost optimization, never an acceptance
shortcut. Every batch must run as one ordinary candidate. After acceptance,
rediscover targets from the new tree; after rejection, split deterministically
until the reducer reaches its singleton policy. Multi-file and multi-range
batches must validate all inputs before the first write and roll back a partial
write. A parallel union of independently passing candidates is a new candidate
and must pass its own oracle run before promotion.

Changes to batching, epoch behavior, component ordering, or fixed-point
semantics require a new `REDUCTION_STRATEGY` value. The strategy must remain in
both the report and persistent-session identity so checkpoints cannot silently
resume under different reduction semantics.

Failure signature extractors must normalize unstable presentation details but
retain enough identity to reject a different failure of the same broad class.
Tests must cover repeated baselines, a matching candidate, and a near-match
with a different message or origin frame.

Process failure signatures must come from the observed return code, never from
localized shell output. Keep direct POSIX signals distinct from positive
shell/container exit codes, normalize signed and unsigned Windows statuses to
the same 32-bit value, and exclude timeout/resource results. Tests must include
a real signal-terminated subprocess, exact candidate matching, checkpoint
restore, and tamper rejection.

Execution backends must preserve command output and exit behavior, terminate
child workloads on timeout, avoid promoting command side effects, and report
infrastructure failures separately from the requested failure. Security
defaults and any host resources exposed to a backend must be documented.
Mutable runner references must be resolved before sampling and both the original
reference and immutable identity must be checkpointed. Resume tests must prove
that identity drift is rejected before another oracle command runs.

Changes to repeated candidate sampling must preserve both the fixed-size rule
and the stopping contract. Count-only candidates may accept as soon as their
minimum pass count is observed. Rate-gated early acceptance must require the
count minimum, the Jeffreys beta-binomial mixture anytime lower bound, and a
planned-size exact one-sided gate that still passes when every remaining sample
is treated as a failure. A candidate reaching its maximum sample count uses the
fixed-size exact rule; baseline and final validation must still run all
configured samples. Never use a fixed-time interval as repeated-prefix evidence.

The anytime confidence sequence uses the candidate's allocated alpha. Without
run-wide control this is `1 - confidence` and has per-candidate coverage only.
With `--run-confidence C`, family `j` uses at most
`(1-C)/(j(j+1))`, additionally capped by the base alpha. Tests must exhaustively enumerate small Bernoulli
sample trees to bound the probability of ever crossing, cover count-only and
rate-plus-count early acceptance, prove that the worst-case exact guard keeps
early decisions inside the fixed-`N` acceptance set, and exercise cases where
the count and anytime gates cross at different samples. An observed timeout or
resource exhaustion must win over acceptance at the same prefix. Tests must
also show that skipped suffix runs cannot report a hypothetical future resource
failure and that final validation still performs the full run count.

Run-wide family allocation must happen after confirming a changed tree and
before sampling. No-ops consume nothing; deterministic parallel candidates,
combinations, and changed cache/duplicate candidates consume distinct indices.
Checkpoint the allocated window before commands and never reuse its indices on
resume. Use exact rational comparisons to ensure binary rounding cannot increase
alpha. Tests must cover harmonic sums, adaptive small-family error probability,
unattainable and unrepresentable families, parallel numbering, early acceptance
at the allocated confidence, interrupted resume, tamper rejection, and final
validation at the last accepted family's confidence. A policy change requires a
new `CANDIDATE_FAMILY_CONTROL_POLICY` value.

A baseline rate gate combined with Java, Python, or process signature learning
must keep signature discovery separate from rate evidence. The first
extractable basic-pass fixes the signature; only later planned slots enter the
exact rate gate, while the count threshold still spans all configured baseline
runs. Tests must cover competing signatures and prove that the observed mode is
not tested as though it had been fixed before sampling. Count-only signature
mode must retain its existing stable-mode semantics.

Sampling report and checkpoint tests must cover
`candidate_early_acceptances`, `candidate_early_rejections`, and the combined
`candidate_samples_saved` count. Accepted events must preserve
`oracle_anytime_lower_bound`, `oracle_early_acceptance`, family index,
candidate confidence, and actual binary alpha, including across
resume, while older checkpoints default missing fields safely. Repeated
candidate sampling must continue to use fresh repository copies and bypass the
single-result content cache.

Final holdout certification is a separate statistical contract. Never reuse a
baseline, candidate, combination, or ordinary final-validation sample as
holdout evidence. Freeze the cleaned artifact, failure signature, runner
identity, `N`, target rate, confidence, and policy before the first command; run
all `N` fresh copies without cache or early stopping; and gate with the exact
one-sided binomial test rather than a rounded displayed bound. Timeout and
resource exhaustion are non-passes and hard vetoes. Holdout samples must not
enter candidate or reducer phase counters, and their results must never trigger
another mutation or candidate choice.

Persistent holdout changes must preserve the write-ahead protocol: checkpoint an
in-flight index before starting its command, checkpoint its result before moving
to the next index, and conservatively burn an unresolved in-flight slot on
resume. Successful, unsuccessful, and aborted attempts are terminal within a
session. Tests must cover known Clopper-Pearson values, exact attainability,
small-sample coverage, fresh-copy isolation, full fixed-size execution, resource
vetoes, artifact binding, interrupted resume, terminal idempotence, legacy
checkpoint behavior, and a report that keeps ordinary `final_*` statistics
separate. A policy or decision-rule change requires a new
`HOLDOUT_CERTIFICATION_POLICY` value.

Certified export recovery must never overwrite evidence speculatively. Tests
must cover a crash after payload export, a crash after sidecar creation, exact
payload fingerprint reuse, missing-sidecar reconstruction, and rejection of
partial or mismatched payload/metadata without additional holdout samples.

Documentation and report fields must state the actual claim: a one-sided lower
confidence bound on oracle pass probability in the recorded environment,
conditional on fresh iid samples. Do not describe it as code correctness,
production reliability, proof that a regex identifies one bug, or protection
against repeated attempts in new sessions. Family-wise control may be claimed
only when `--run-confidence` is enabled, and only for the conditional
per-candidate sampling model documented in the architecture.

Phase-statistics changes must preserve both accounting identities:

```text
attempts = no_op + rejected + accepted + superseded + aborted
oracle_sample_uses = oracle_samples + cache_hits
```

Here `superseded` is oracle-positive but unpromoted, and `oracle_samples` counts
actual command samples rather than logical cache uses. Tests must cover no-ops,
rejection, concurrent positives, a passing and failing combination, cache reuse,
candidate early stopping, mutation/oracle interruption, persistent resume, and
an active pass recovered as aborted. Never fabricate unavailable legacy phase
history; report it with partial coverage.

Source analyzers may report candidate ranges, but only `ReductionSession` may
apply and accept mutations. A candidate range must include a content hash so a
stale AST position cannot modify different source text. Replacement candidates
must derive their selected/replacement ranges from the parser and include the
replacement bytes in target identity; source-level regular-expression edits
are not accepted.

Candidates that coordinate multiple ranges, whether in one file or across
files, must validate every path, range, content hash, and overlap constraint
before writing the first file. Apply edits from the highest byte offset to the
lowest within each file, and add a test in which one stale edit rejects the
whole candidate without partially modifying another file.

Symbol-aware candidates must use compiler identity rather than matching names
or textual signatures. Unresolved symbols must never be guessed. A parameter
reference or a method/constructor reference is a blocker for its group, while
anonymous-class construction blocks every source constructor of its base type.
Record constructors and unsupported dispatch families must not produce groups.
Closed source override families must be represented by one atomic change set
covering every source declaration and resolved call argument.
If a source type hierarchy or any method-invocation, constructor-expression,
or member-reference path has an `ERROR` type, disable all coordinated
candidates for that analysis pass rather than skipping only that node;
syntax-only candidates may still be reported.

Keep the Java analysis helper compilable with `javac --release 11`. Do not use
compiler-tree or language-model APIs introduced after Java 11 without an
explicit compatibility design. The Java test matrix must include an actual JDK
11 compiler/runtime lane; compiling on a newer JDK with `--release 11` checks API
and class-file compatibility but not JDK 11 javac model or diagnostic behavior.
Tests for symbol work must cover overload identity, cross-file links, private and
static methods, ordinary constructors, varargs ranges, stale grouped edits,
nested calls, and dispatch eligibility. Instance-method coverage must include an
explicitly `final` method, a method closed by a `final` ordinary top-level or
member class, and a source-local override family with a `final` leaf; current
overrides and interface implementations (including external contracts), prospective
post-removal overrides or implementations, generic-erasure or bridge clashes,
open virtual and native methods, and enum, record, local, and anonymous owners
must remain excluded. Continue to cover anonymous-class and record-constructor
exclusions, global `ERROR`-type fallback, and recoverable unresolved symbols
that must not be guessed.

Node manifest changes must use the strict JSON structure parser rather than
regular-expression replacement. Tests must cover first, middle, and last object
members, array entries, nested `overrides`/`resolutions`, duplicate-key and
non-standard-JSON rejection, stale content hashes, and a real oracle command
that keeps one dependency while removing unrelated package entries. Lockfiles
and runtime metadata must remain outside the adapter's mutation set.

Composer manifest changes must use the same strict JSON structure parser rather
than regular-expression replacement. Tests must cover requirement categories,
scripts, repository array entries, first/middle/last removals, duplicate-key
and non-standard-JSON rejection, stale hashes, and an offline structural oracle.
Autoload maps, arbitrary `extra` metadata, and `composer.lock` must remain
outside the adapter's mutation set.

MSBuild manifest changes must use XML parsing and stable item identity rather
than regular-expression replacement. Tests must cover package, project,
framework, source/content item categories, first/middle/last removals, stale
subtree hashes, namespaced XML, malformed XML fail-closed behavior, and an
entity-declaration rejection, and an offline structural oracle. Property groups,
imports, conditions, arbitrary metadata, and lockfiles must remain outside the
adapter's mutation set.

Ruby manifest changes must use the balanced line scanner and stable content
hashes rather than regular-expression replacement. Tests must cover single-line
and parenthesized calls, grouped declarations, comments/strings, multiline and
block exclusions, first/middle/last removals, stale hashes, and a real Ruby
oracle. Arbitrary Ruby code and `Gemfile.lock` must remain outside the
adapter's mutation set.

Cargo manifest changes must use structured TOML tokens and stable content-hashed
ranges. Tests must cover root, target-specific, development, and build
dependency tables, workspace `members`/`exclude` arrays, first/middle/last
removals, stale hashes, and a real offline `cargo` oracle. `Cargo.lock`, feature
lists, and unrelated metadata must remain outside the adapter's mutation set.

Go manifest changes must use line-structured directive parsing rather than
regular-expression replacement. Tests must cover single-line and block forms
of `require`/`replace`, `exclude` and `retract`, unclosed-block fail-closed
behavior, stale hashes, and an offline `go run` oracle. Workspace `use` and
workspace-level `replace` entries are also eligible targets; `go.sum`, module
declarations, Go version/toolchain directives, and other `go.work` metadata
must remain outside the adapter's mutation set.

Tests for coordinated parameter removal must distinguish static analysis from
the reproduction oracle. Direct method and constructor references are compiler
blockers. Reflection, `MethodHandles.Lookup`, generated or JNI/framework calls,
and precompiled external callers require oracle tests that exercise those paths;
the test command should compile affected source and use `--java-exception` when
exception identity is part of the contract. Do not treat an accepted reproduction
as proof of source compatibility, ABI compatibility, compilation, or unexercised
behavior.

Changes to Java classpath handling must preserve its host-side analysis-only
boundary. Each repeated `--java-classpath PATH` is an atomic entry, including on
platforms where the filename contains the normal path separator character;
never split one value into a path list. Resolve relative entries against the
original source, retain precedence order, and reject missing, unreadable,
unsupported, or duplicate physical entries (including symlink and hardlink
aliases). Tests must cover canonicalization, order, duplicate and invalid paths,
file and directory content fingerprints,
recursive path/type/mode hashing, rejection of nested symlinks and special
files, and resume rejection after either the configured paths or their contents
change. A top-level symlink resolves to its canonical target; regular files do
not require a particular extension or archive format before being passed to
javac.

Classpath tests must also prove that entries are forwarded to the host compiler
without modifying the oracle command or Docker mounts. Include a dependency
that enables otherwise-missing attribution, and assert that an external binary
`ExecutableElement` cannot supply the declaration role of a source group or
become a mutation target. ReproMin must not invoke Maven, Gradle, or another
build tool to discover the classpath.

Run the test suite with:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

If pytest is already available in your development environment, the shorter
`pytest -q` command is also supported. The repository configuration limits
collection to `tests/` and adds the `src/` layout automatically; benchmark
fixture tests are intentionally run through `benchmarks/run_offline.py`.

To inspect branch coverage locally, install the CI-only coverage tool and run:

```sh
python3 -m pip install coverage
PYTHONPATH=src python3 -m coverage run --branch -m unittest discover -s tests
python3 -m coverage report --show-missing
```

The preflight command above also runs linting and byte-compilation in temporary
cache directories. To run only those static checks, use:

```sh
python3 scripts/check_contribution.py --skip-tests
```

Run the network-free benchmark regression with:

```sh
python3 benchmarks/run_offline.py
```

The runner verifies each offline fixture in a disposable output directory,
independently reruns its oracle, and skips fixtures whose toolchain is not
installed. CI runs the same command as the `offline-benchmarks` job.

The repository deliberately ignores `E501` line-length diagnostics while
enforcing Ruff's `E` and `F` correctness rules. Existing long strings and
fixture commands are often clearer when kept intact; formatting-only rewrites
should not obscure reducer behavior.
