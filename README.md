# ReproMin

![CI](https://github.com/fly1d/repomin/actions/workflows/ci.yml/badge.svg)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License Apache-2.0](https://img.shields.io/github/license/fly1d/repomin)
[![Discussions](https://img.shields.io/github/discussions/fly1d/repomin)](https://github.com/fly1d/repomin/discussions)

ReproMin reduces a failing repository while continuously checking that the
original failure still occurs. Its output is intended to be a small,
standalone reproduction that can be attached to an issue or turned into a
regression test.

> Status: pre-alpha feasibility build. The default host backend executes the
> supplied shell command directly and is not a sandbox. The optional Docker
> backend reduces access but is not a complete security boundary.

## Why

Source reducers such as C-Reduce and Perses minimize programs or individual
inputs. Environment tools such as ReproZip and containers capture what is
needed to rerun a command. ReproMin targets the layer between them: project
files, modules, build manifests, dependencies, and source symbols.

## Who benefits

- **CI and application teams** get a small failure artifact instead of a full
  checkout when a regression needs to be reported or reviewed.
- **Library and build-tool maintainers** can isolate the exact dependency,
  manifest entry, source symbol, or module that keeps a failure reproducible.
- **Test and benchmark authors** can turn a reduced tree into a deterministic
  regression fixture with an auditable `report.json`.
- **AI-assisted debugging workflows** can use the optional semantic reducer to
  propose edits while the ordinary oracle remains the acceptance gate.

The result is evidence for one configured reproduction in one recorded
environment. It is not a proof of code correctness, production reliability, or
a security sandbox. For automatic CI artifacts, see the
[GitHub Action guide](docs/GITHUB_ACTION.md).

## Quick start

ReproMin requires Python 3.9 or newer and has no runtime dependencies.

For a five-minute, copy-paste workflow, start with the
[English quick start](docs/QUICKSTART.md). It installs the current release,
creates a tiny network-free failing project, reduces both files and text, and
validates the exported payload fingerprint. The larger
[examples guide](docs/EXAMPLES.md) covers individual languages and build tools.
Windows users can run the same complete workflow with explicit virtual
environment paths in the [PowerShell quick start](docs/QUICKSTART.windows.md).

Before a long reduction, run the read-only [doctor preflight](docs/DOCTOR.md)
to detect supported reducers and verify that an optional failure command passes
its baseline checks in fresh copies:

```sh
repomin doctor . \
  --command 'python -m pytest -q' \
  --match 'FAILED tests/test_regression.py' \
  --output /tmp/project-repro
```

After a reduction, use the [replay command](docs/REPLAY.md) to check the
recorded failure contract against fresh copies of the exported payload:

```sh
repomin report replay /tmp/project-repro.repomin/report.json \
  --payload /tmp/project-repro \
  --yes
```

The replay and transport-fingerprint workflow is included in the current
`v0.1.0.dev9` pre-release. The [real-failure pilot guide](docs/REAL_FAILURE_PILOT.md)
describes the report and privacy boundaries.

The [public tsdown pilot](docs/CASE_STUDY_TSDOWN_979.md) shows an actual
`14 -> 8` file reduction, a strengthened executable oracle, exact payload
validation, and `3/3` fresh-copy replays without making a root-cause claim.

The [public pydoctor pilot](docs/CASE_STUDY_PYDOCTOR_728.md) shows how a strict
late-failure oracle turned a long-standing upstream request into a nine-file
fixture with green public CI, exact validation, and `3/3` fresh-copy replay.

Trying ReproMin on a real workflow? A successful, inconclusive, or blocked run
is useful feedback. Use the [pilot issue](https://github.com/fly1d/repomin/issues/11)
for a sanitized CI/dependency failure, or the [user workflow feedback
template](https://github.com/fly1d/repomin/issues/new?template=adoption_feedback.md)
when you want to report value, friction, or compatibility without publishing a
failure. Review the payload and report first; do not upload credentials,
private URLs, proprietary source, raw logs, commands, or environment values.

中文用户可以先阅读[中文快速开始](docs/QUICKSTART.zh-CN.md)，其中包含一个
可直接运行的最小缩减示例、安全边界和报告说明。

## Install

The current pre-alpha release is distributed from GitHub Releases; it is not
published to PyPI yet. Use an isolated virtual environment so the `repomin`
command does not modify your system Python:

```sh
python3 -m venv .venv
. .venv/bin/activate                         # macOS/Linux
python -m pip install --upgrade pip
REPOMIN_VERSION=0.1.0.dev9
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v${REPOMIN_VERSION}/repomin-${REPOMIN_VERSION}-py3-none-any.whl"
python -m repomin --version
```

Windows PowerShell users can create and activate the same kind of environment
with `py -3 -m venv .venv` and `.venv\Scripts\Activate.ps1`, then install the
wheel with PowerShell's environment-variable syntax:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
$env:REPOMIN_VERSION = "0.1.0.dev9"
python -m pip install "https://github.com/fly1d/repomin/releases/download/v${env:REPOMIN_VERSION}/repomin-${env:REPOMIN_VERSION}-py3-none-any.whl"
python -m repomin --version
```

If PowerShell blocks `Activate.ps1` because of its execution policy, leave the
environment unactivated and replace `python` above with
`.venv\Scripts\python.exe`. The [complete PowerShell quick
start](docs/QUICKSTART.windows.md) uses that explicit interpreter for every
install, preflight, reduction, validation, and replay command.

The [release page](https://github.com/fly1d/repomin/releases/tag/v0.1.0.dev9)
includes SHA-256 checksums for the wheel and source archive; verify the
downloaded asset there when supply-chain verification is required. The wheel is
preferred for a quick install because it needs no build step.

To install the source archive instead, keep the same `REPOMIN_VERSION` value:

```sh
REPOMIN_VERSION=0.1.0.dev9
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v${REPOMIN_VERSION}/repomin-${REPOMIN_VERSION}.tar.gz"
```

Pip will create an isolated build environment for the declared build tools.

If your shell cannot find `repomin`, activate `.venv` again or invoke
`python -m repomin` with the same interpreter used for installation. Confirm
the selected environment with `python -m pip show repomin`; remove it with
`python -m pip uninstall repomin` when needed.

For development, clone the repository and install the optional tooling extras in
editable mode:

```sh
git clone https://github.com/fly1d/repomin.git
cd repomin
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m repomin --version
```

The plain `python -m pip install -e .` form is also sufficient when you only
need the package and do not plan to run the lint, coverage, build, or release
checks. A version-matched source archive is available on the same [release
page](https://github.com/fly1d/repomin/releases/tag/v0.1.0.dev9) for users who
need to inspect or build from source.

### Shell completion

ReproMin can print completion definitions for Bash, Zsh, Fish, and PowerShell.
Evaluate the script for the current shell, or install it using that shell's normal
completion directory:

```sh
# Bash
eval "$(repomin completion bash)"

# Zsh
eval "$(repomin completion zsh)"

# Fish
repomin completion fish | source

# PowerShell
Invoke-Expression (repomin completion powershell | Out-String)
```

The completion includes the supported adapter, source reducer, semantic
backend, Docker policy, and other enum values. Path-bearing options fall back
to the shell's file completion.

## Reduce a project

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

repomin /path/to/project \
  --command './mvnw test -Dtest=PaymentServiceTest' \
  --match 'NoSuchMethodError' \
  --java-exception \
  --jobs 4 \
  --output /path/to/payment-repro
```

The source repository is copied before the baseline command runs. Accepted
mutations are made only in temporary copies, and an existing output directory
is never overwritten. Export freezes the accepted tree's complete fingerprint,
copies into a unique staging path under the output's parent, verifies the
staged fingerprint, and then publishes with the platform's atomic no-replace
rename primitive. Copy, verification, and publication failures remove staging
without exposing a partial output. A platform without a provable no-replace
directory rename fails the export instead of using a racy fallback.

The output directory contains only the reduced repository payload. ReproMin
writes `report.json` and `REPOMIN.md` to the sibling metadata directory
`OUTPUT.repomin`, so its own evidence cannot change the tree that passed the
oracle. `REPOMIN.md` also records the execution backend and, for Docker runs,
the image reference, immutable image ID, and network policy. Environment
variable values are never written; only their names are listed when configured.
Neither an existing output nor an existing metadata directory is overwritten.

Use repeatable `--ignore NAME` options to exclude an exact file or directory
basename before the first baseline run. The name applies recursively, in
addition to ReproMin's built-in generated/dependency directories:

```sh
repomin /path/to/project \
  --command './run-repro.sh' \
  --match 'ORIGINAL_FAILURE' \
  --ignore '.env' \
  --ignore 'fixtures-large' \
  --output /tmp/repro
```

Ignored entries are never copied into candidate workspaces or exports unless
they are explicitly named by `--keep`. The effective sorted basename set is
recorded in `report.json` and persistent session identity, so changing it on
`--resume` is rejected. `--ignore` accepts one ordinary basename at a time; it
is not a shell glob or a `.gitignore` parser.

For a monorepo where only one same-named subtree should be excluded, use an
exact repository-relative path:

```sh
repomin /path/to/project \
  --command './run-repro.sh' \
  --match 'ORIGINAL_FAILURE' \
  --ignore-path 'services/api/private' \
  --output /tmp/repro
```

`--ignore-path` removes that path and all descendants wherever they occur in
the reduction workspace. It rejects absolute paths, `..` segments, and glob
syntax; it is intentionally not a `.gitignore` parser.

To reuse an existing rule file without converting every line to an exact flag,
apply the repository `.gitignore` explicitly:

```sh
repomin /path/to/project \
  --command './run-repro.sh' \
  --match 'ORIGINAL_FAILURE' \
  --gitignore \
  --output /tmp/repro
```

`--gitignore` adds the repository root `.gitignore`. Add other rule files with
repeatable `--gitignore-file PATH`; relative paths are resolved against the
repository. These rules are evaluated after the built-in and exact exclusions,
so a negated `!` rule can restore a path excluded by an earlier rule file entry
but can never restore a built-in or exact exclusion. An explicit `--keep` path
takes precedence over all of these exclusions. The supported subset
handles comments, blank lines, negation, trailing-slash directory rules,
leading-slash anchoring, `*`, `**`, `?`, and `[...]` character classes. Escaping
and per-directory precedence are not a full git implementation; ambiguous files
should use exact `--ignore-path` entries instead. The rule-file paths and a
SHA-256 digest of their contents are recorded in the report and persistent
session identity, so a changed rule file is rejected on `--resume`.
Trailing-slash rules are type-aware: they exclude the matching directory and
its descendants, but do not exclude a same-named regular file.

For repositories with per-directory rule files, use `--gitignore-recursive`.
It applies the root `.gitignore` plus every nested `.gitignore`, each relative
to its own directory, in top-down order:

```sh
repomin /path/to/project \
  --command './run-repro.sh' \
  --match 'ORIGINAL_FAILURE' \
  --gitignore-recursive \
  --output /tmp/repro
```

Directories excluded by the built-in, exact ignore, or already-applied
gitignore rules are not descended into, so their nested rule files are not
collected. The recorded `gitignore` file list and digest include every rule file
actually used, and `gitignore_recursive` is part of the report and session
identity.

To keep a file or directory that the oracle does not otherwise require, use
repeatable `--keep RELATIVE_PATH`. The file reducer will not delete the exact
path or any descendant of a kept directory:

```sh
repomin /path/to/project \
  --command './run-repro.sh' \
  --match 'ORIGINAL_FAILURE' \
  --keep 'LICENSE' \
  --keep 'fixtures/golden' \
  --output /tmp/repro
```

`--keep` uses the same exact relative-path grammar as `--ignore-path` and does
not accept glob syntax. It protects only file/directory deletion; manifest,
source, and other reducers may still edit files inside a kept directory. The
keep declaration also wins over an active ignore rule for the target and the
parent directories needed to reach it. The sorted keep paths are recorded in
the report and session identity, so a changed set is rejected on `--resume`.

To shrink the contents of a specific UTF-8 text file rather than deleting the
whole file, pass repeatable `--text-file RELATIVE_PATH`. The reducer applies
hierarchical line-range deletion and still requires every accepted edit to
preserve the configured failure:

```sh
repomin /path/to/project \
  --command './run-failing-command' \
  --match 'ORIGINAL_FAILURE' \
  --text-file 'fixtures/input.txt' \
  --text-file 'config/plain.ini' \
  --output /tmp/repro
```

`--text-file` uses the same exact relative-path grammar as `--keep`. Before the
first baseline run, every explicitly selected path must be a readable UTF-8
regular file (not a symbolic link) in the effective source tree. Missing,
non-text, or ignored targets are rejected before any output or session is
created. A valid target can still be removed later when the file reducer proves
that the whole file is unnecessary. The selected paths are part of the session
identity, so a changed set is rejected on `--resume`.

Use repeatable `--env NAME=VALUE` options when the reproduction needs explicit
environment flags. The same values are passed to host and Docker runners, while
reports and checkpoints retain only sorted variable names and a SHA-256 digest
of the name/value mapping:

```sh
repomin /path/to/project \
  --command './run-repro.sh' \
  --match 'ORIGINAL_FAILURE' \
  --env CI=1 \
  --env FEATURE_GATE=disabled \
  --output /tmp/repro
```

Values are intentionally omitted from `report.json`, `REPOMIN.md`, and
`state.json`; review the command and the reproduction environment separately
before sharing a metadata sidecar. `REPOMIN` is reserved for ReproMin's
internal marker and cannot be overridden.

For a native process crash that may not print stable text, learn its termination
instead of supplying an output regular expression:

```sh
repomin /path/to/project \
  --command 'exec ./reproduce-crash' \
  --process-failure \
  --output /path/to/crash-repro
```

`--process-failure` learns the baseline's exact process-level signature and
requires every accepted candidate to retain it. A directly observed POSIX
termination is stored as its signal number and name, and a Windows status is
normalized to its unsigned 32-bit value; common exception statuses are named in
the report. Every other non-zero result remains an exact exit-code signature.
The mode does not need `--match`; when `--match` is also supplied, both the
termination and output must match. It cannot be combined with `--exit-code`,
which is the manual alternative when the expected code is already known.

`--exit-code` can also be the sole failure criterion, without a `--match`
regular expression:

```sh
repomin /path/to/project \
  --command './run-failing-command' \
  --exit-code 7 \
  --output /path/to/exit-code-repro
```

In this mode only the exact exit status is preserved; captured stdout/stderr
may change freely.

Use `exec` in a host shell command when the direct POSIX signal matters. Shells
and container runtimes often expose a child signal as the ordinary exit code
`128 + signal`; ReproMin preserves that exact code but deliberately does not
guess that it was a signal. Timeouts and resource exhaustion never become
process-failure signatures.

`--jobs N` evaluates up to N candidates concurrently and consumes results in
candidate order, independent of completion order. It normally commits the
lowest-index successful candidate. A reducer may instead propose the compatible
union of multiple successful candidates, but that union is committed only after
its own oracle run passes. The default is `1` because separate working
directories do not isolate ports, databases, services, or other external state
used by the command.

Use `--max-attempts N` to bound a long reduction. ReproMin stops preparing new
candidate attempts after `N` logical attempts, exports the latest accepted
tree, and sets `budget_exhausted` in the report:

```sh
repomin /path/to/project \
  --command './mvnw test -Dtest=PaymentServiceTest' \
  --match 'NoSuchMethodError' \
  --max-attempts 500 \
  --output /tmp/payment-repro-bounded
```

The budget applies only to reduction candidates, not baseline, final
validation, or optional holdout samples. A bounded run is still a valid,
reproducible tree; it is simply not necessarily a global fixed point. The
budget is part of the session identity, so changing it on `--resume` is
rejected.

Use `--max-duration SECONDS` to bound the reduction by wall-clock time instead.
The same final-validity and identity rules apply:

```sh
repomin /path/to/project \
  --command './mvnw test -Dtest=PaymentServiceTest' \
  --match 'NoSuchMethodError' \
  --max-duration 120 \
  --output /tmp/payment-repro-timed
```

On timeout, resource exhaustion, Ctrl-C, or an exception in a parallel window,
ReproMin cancels every active candidate command before discarding its trial
directories. Host commands run in a managed process tree: POSIX uses a gated
process group and Windows uses a suspended process assigned to a Job Object
before it starts. A command that returns while leaving ordinary background
children is cleaned up before its result is consumed. Combined stdout and
stderr capture is limited to 64 MiB; exceeding the limit is a resource failure
and can never satisfy the oracle.

Candidate results are cached by repository content for the duration of one
ReproMin process. Use `--no-cache` when the failure depends on time, network
responses, or other state not represented by files in the candidate.

An opt-in semantic reducer seam can propose source edits from a
provider-agnostic OpenAI-compatible endpoint. It is disabled by default, has no
third-party runtime dependency, and never binds a default provider:

```sh
export REPOMIN_SEMANTIC_ENDPOINT=http://localhost:8000/v1/chat/completions
export REPOMIN_SEMANTIC_MODEL=your-local-model
export REPOMIN_SEMANTIC_TOKEN=optional-bearer-token

repomin /path/to/project \
  --command './run-failing-command' \
  --match 'ORIGINAL_FAILURE' \
  --semantic-reducer http \
  --output /path/to/repro
```

`--semantic-endpoint`, `--semantic-model`, and `--semantic-timeout` can also be
passed directly; the bearer token is read only from `REPOMIN_SEMANTIC_TOKEN` so
it never appears in `argv` or reports. The backend responds with a JSON `edits`
array of either `{"path": "...", "replace": "..."}` or
`{"path": "...", "delete": true}`; markdown code fences around that JSON are
tolerated. Every returned candidate still goes through the complete failure
oracle and is discarded if it stops reproducing the configured failure. Reports
record `semantic_reducer`,
`semantic_model`, `semantic_endpoint`, `semantic_calls`, and
`semantic_accepted`; see `docs/LLM_REDUCTION.md`.

Structured reducers start with deterministic multi-target batches and split a
rejected batch into smaller batches. Batching changes how much is proposed, not
the acceptance rule: every batch is materialized in its own repository copy and
must pass the complete oracle before it is promoted. When multiple compatible
Java candidates pass in one parallel window, ReproMin may also test their union;
the union is promoted only after an additional oracle run.

For intermittent failures, `--baseline-runs N --min-baseline-passes K` allows
up to `N-K` non-passing baseline samples while still requiring a reproducible
failure often enough to start. `--candidate-runs N --min-candidate-passes K`
runs every candidate in fresh copies and accepts it only when at least `K`
samples satisfy the complete oracle. Timeouts and resource-exhausted runs are
never counted as passes and reject that candidate's sample set. Candidate
content caching is disabled automatically when `--candidate-runs` is greater
than one, because replaying one old sample would invalidate the statistical
check. Defaults remain strict: all baseline runs must pass and each candidate
is run once.

Repeated candidate sampling can stop in either direction. It rejects early when
the remaining planned samples cannot possibly satisfy the count/rate criteria,
or as soon as an observed timeout or resource exhaustion makes the sample set
invalid. Without a rate gate, a candidate accepts as soon as it has `K` passing
samples because later ordinary failures cannot undo that count.

With a candidate rate gate, early acceptance requires all three of these
conditions: the count minimum is met, a Jeffreys beta-binomial mixture
confidence-sequence lower bound for the observed prefix is at least `R`, and the
planned-`N` exact one-sided binomial gate would still pass if every unexecuted
sample were a failure. The mixture uses `alpha = 1 - confidence`, so inspecting
it after every sample remains valid under optional stopping. The worst-case
exact check ensures that early acceptance does not enlarge the fixed-`N`
pass/fail acceptance set. Observed timeout and resource failures are checked
before acceptance; samples skipped after a stopping decision never run, so a
resource failure they might have produced cannot be observed.

For a less brittle statistical gate, add `--min-baseline-rate R` and/or
`--min-candidate-rate R`. Full sample sets use an exact one-sided binomial test,
equivalent to requiring a Clopper-Pearson lower confidence bound of at least
`R`; the default confidence level is 95% and can be changed with `--confidence`.
Rates must satisfy `0 < R < 1`; use an all-runs count threshold for strict 100%
behavior. When a rate is specified without its corresponding
`--min-*-passes`, the count minimum is one and the rate is the meaningful gate.
If both are specified, both constraints must pass.

When a baseline rate gate is combined with `--java-exception`,
`--python-exception`, or `--process-failure`, the first basic-passing sample
with an extractable signature is a discovery sample. It fixes the signature but
is not reused as rate evidence; only the remaining planned slots test that
fixed signature.
The count minimum still applies to matching samples across the complete `N`
runs. The CLI therefore checks rate attainability with at most `N-1`
post-discovery samples. Without a baseline rate gate, signature selection keeps
the existing stable-mode count semantics.

For example:

```sh
repomin /path/to/project \
  --command 'pytest -q tests/test_regression.py' \
  --match 'checkout failed' \
  --baseline-runs 10 --min-baseline-rate 0.6 \
  --candidate-runs 10 --min-candidate-rate 0.6 \
  --run-confidence 0.95
```

Baselines and final validation always run all configured samples, and a
candidate that reaches `N` uses the ordinary fixed-size count and exact gates.
The anytime bound is an additional early-acceptance gate, not a replacement for
that terminal rule. Without `--run-confidence`, its coverage is per candidate.

`--run-confidence C` adds run-wide control for the adaptively selected candidate
families and requires `--min-candidate-rate`. Candidate family `j` receives
`alpha_j = (1-C)/(j(j+1))`, capped by the ordinary `1-confidence` alpha. The
terminal exact gate and anytime bound both use that candidate's allocated
confidence. Since the harmonic allocation sums to at most `1-C`, a union bound
controls the probability of accepting any candidate whose true oracle pass rate
is at most `R`, conditional on each candidate's fresh samples satisfying the
documented iid model given prior reduction history. Cross-candidate independence
is not required.

Only candidates that actually change the tree consume a family index. Parallel
windows allocate indices in deterministic candidate order; combinations and
changed cache/duplicate candidates consume separate indices. Persistent sessions
checkpoint allocated indices before sampling, so an interrupted allocation is
never reused. As alpha shrinks, a fixed `--candidate-runs` eventually cannot pass
even with all samples successful; ReproMin then fails closed and asks for more
runs or a lower rate/run confidence instead of reporting a fixed point.

The report records configured rates, confidence, observed rates, and Wilson
lower bounds. For a baseline rate gate, `baseline_rate_evidence_runs` and
`baseline_rate_evidence_passes` identify the exact-test sample set, while
`baseline_exact_lower_bound`, `baseline_exact_p_value`, and
`baseline_exact_rate_gate_passed` expose its one-sided Clopper-Pearson result.
These fields are `null` when no baseline rate gate is configured. The ordinary
`baseline_runs`, `baseline_passes`, `baseline_rate`, and
`baseline_lower_bound` fields continue to describe all baseline samples, so a
signature discovery split does not silently change their meaning. A third
party can reproduce the baseline gate from the evidence counts,
`min_baseline_rate`, and `confidence`.
On resume, an older non-signature checkpoint that predates these fields is
upgraded from its full-sample baseline counts. An older signature checkpoint
without the post-discovery counts is rejected because its discovery position
cannot be reconstructed safely.
`candidate_sampling_policy` identifies the stopping-rule version.
When run-wide control is enabled, the report also records `run_confidence`,
`candidate_family_control_policy`, the number of allocated families, the
cumulative nominal alpha upper bound, and each accepted event's family index,
binary confidence, and actual alpha.
It also records `candidate_early_acceptances`,
`candidate_early_rejections`, and `candidate_samples_saved`; the saved count
includes both stopping directions. Accepted events expose
`oracle_anytime_lower_bound` and `oracle_early_acceptance` so early decisions can
be distinguished from full-size decisions.

### Final holdout certification

Candidate, baseline, and ordinary final-validation samples participate in
selection or consistency checking. They are not independent evidence about the
one final artifact. An optional holdout runs a new, fixed-size sample only after
the reducer reaches its global fixed point and the ordinary final validation
passes:

```sh
repomin /path/to/project \
  --command 'pytest -q tests/test_regression.py' \
  --match 'checkout failed' \
  --holdout-runs 29 \
  --min-holdout-rate 0.9 \
  --holdout-confidence 0.95 \
  --session /tmp/checkout-repro-session \
  --output /tmp/checkout-repro
```

`--holdout-runs` and `--min-holdout-rate` must be supplied together; holdout
confidence is separate from `--confidence` and defaults to `0.95`. ReproMin
rejects an unattainable plan before reduction. At 95% one-sided confidence, for
example, certifying a minimum rate of 0.90 requires at least 29 runs even when
every run passes.

The cleaned payload, oracle signature, runner configuration, sample count,
target rate, and confidence are frozen before the first holdout command. Every
sample starts from a fresh copy of that same payload. ReproMin does not use its
candidate cache, does not stop early, and does not include any earlier sample in
the holdout count. Certification uses a fixed-`N`, one-sided exact
Clopper-Pearson lower bound; the gate itself is an exact binomial upper-tail
comparison. A timeout or resource-exhausted sample is a failure and vetoes
certification even if the remaining pass count would clear the rate gate.

The certificate says that, assuming the recorded holdout runs are independent
and identically distributed, the oracle pass probability in that runner
environment has the reported lower confidence bound. It does not say that the
code is correct, that the match identifies the intended bug, or that the bound
is a production failure rate. Fresh repository copies do not isolate ports,
services, host caches, time, or network state. Docker with a pinned local image
and `--docker-network none` improves environmental control, but ReproMin cannot
verify the iid assumption.

Persistent sessions write the holdout plan before each command and record each
result before advancing. An interrupted in-flight slot is permanently counted
as a non-pass on resume instead of being selectively retried. A completed
success or failure is terminal for that session; `--resume` never samples it
again. A failed holdout exits `3` and does not create the output directory.
Starting new sessions until one passes is multiple testing and is outside the
single-attempt confidence guarantee.

The top-level `holdout_certification` block in `OUTPUT.repomin/report.json`
records the versioned policy,
attempt ID, payload and oracle digests, exact bound and p-value, required and
observed pass counts, veto counters, resume state, and per-sample summaries.
When no holdout is requested its status is explicitly `not_requested`, and the
default command count is unchanged.

Reports can be checked without rerunning the reproduction command. The command
validates the schema and phase/holdout accounting; `--payload` additionally
checks the exported tree fingerprint and payload size. If an artifact store
rewrites filesystem metadata such as modification times, validation can report
a content-only fingerprint match and explicitly mark metadata drift:

```sh
repomin report validate /tmp/checkout-repro.repomin/report.json \
  --payload /tmp/checkout-repro
```

Use `--json` for a compact result suitable for CI checks.
The result also includes a versioned, privacy-safe adoption summary: oracle
type, source/output sizes and retention ratios, holdout counts, budget state,
and reduction counts. It intentionally omits the reproduction command, match
expression, command output, environment names, and environment values, so the
JSON can be pasted into a feedback report after
reviewing the payload separately.

For a deterministic human-readable summary, use
`--format markdown`. It renders the same deliberately limited evidence fields
as an escaped table and omits report/payload paths, commands, match expressions,
logs, and environment metadata:

```sh
repomin report validate /tmp/checkout-repro.repomin/report.json \
  --payload /tmp/checkout-repro --format markdown
```

When a reduction is repeated, compare two or more validated reports in the
order you provide them:

```sh
repomin report compare \
  /tmp/baseline.repomin/report.json \
  /tmp/candidate.repomin/report.json \
  --label baseline --label candidate \
  --format markdown
```

The comparison is a privacy-safe evidence view, not a performance dashboard.
It validates each report locally, reads no payload, executes no recorded
command, and does not access the network. The JSON form has its own
`comparison_schema_version` and `descriptive_only: true`; it reports only
aggregate sizes, retention ratios, reduction counts, budget/holdout state,
phase coverage, and adjacent numeric deltas. Context warnings call out changes
such as version provenance, input selection, backend, jobs/timeout, oracle
identity, source size, sampling, or holdout configuration. Private paths and
oracle expressions are compared by opaque internal digests only. Labels only
name rows for display and must be short unique ASCII identifiers. Use the
offline benchmark tools for performance history; do not infer causality,
correctness, or production reliability from this comparison.

To execute the report's failure command, first review the unsigned report and
payload, then opt in explicitly. Replay validates the payload before execution
and runs every sample in a separate temporary copy:

```sh
repomin report replay /tmp/checkout-repro.repomin/report.json \
  --payload /tmp/checkout-repro \
  --runs 2 \
  --yes
```

Replay exit code `0` means only that every current-environment run matched the
recorded oracle. It is not holdout certification or a correctness/root-cause
proof. See [docs/REPLAY.md](docs/REPLAY.md) for legacy reports, explicit
environment values, Docker behavior, privacy, and exit codes.

## Resumable sessions

Long reductions can persist their accepted tree and progress in a directory
outside the source repository:

```sh
repomin /path/to/project \
  --command './mvnw test -Dtest=PaymentServiceTest' \
  --match 'NoSuchMethodError' \
  --session /tmp/payment-repro-session \
  --output /tmp/payment-repro
```

If the process is interrupted, the latest accepted mutation is already stored
in the session. Run the same command with `--resume` to continue:

```sh
repomin /path/to/project \
  --command './mvnw test -Dtest=PaymentServiceTest' \
  --match 'NoSuchMethodError' \
  --session /tmp/payment-repro-session \
  --resume \
  --output /tmp/payment-repro
```

The version-3 checkpoint contains the current tree, learned exception signature,
oracle baseline, reduction statistics, events, completed phases, and any
write-ahead holdout state. ReproMin checks
the source tree fingerprint and all failure, runner, resource, and reducer
options before resuming; changed inputs are rejected instead of silently
continuing from an incompatible state. The session directory is a working
state store, not part of the exported reproduction, and can be removed after
the output has been verified.

Tree fingerprints use the domain-separated `tree-sha256-v2` policy. Its
canonical encoding length-prefixes each path, permission mode, modification
time, entry type, regular-file or symlink payload, filesystem flags exposed by
the runtime's `stat_result.st_flags`, and enumerable extended attributes. It
also includes the payload root's own metadata. Access times are deliberately
excluded and normalized after every repository copy and command-tree
fingerprint so fresh commands see the same value. A representable repository
tree contains only directories, regular
files, and relative symbolic links whose resolved targets remain inside the
repository. Regular files with a link count greater than one are rejected,
including files with a hardlink alias outside the repository, because copying
cannot preserve that topology safely. FIFOs, sockets, and block or character
devices are also rejected before copying, fingerprinting file contents, or
running a command. BSD `st_flags` values that make an entry immutable,
append-only, or non-removable (`UF_IMMUTABLE`, `UF_APPEND`, `SF_IMMUTABLE`,
and `SF_APPEND`, plus `UF_NOUNLINK` or `SF_NOUNLINK` on systems that enforce
them) are rejected because the copied tree could not be normalized, reduced,
and removed reliably. Other exposed and copied flags remain supported and part
of the fingerprint. Linux inode flags managed by `chattr` are not exposed by
Python's `stat_result` and are outside this guarantee. On Windows, non-symlink
reparse points such as junctions are rejected before directory traversal.
Commands run only in private copies. During cleanup, ReproMin clears copied
flags only on directories and entries with one hardlink; it will report a
cleanup failure rather than alter a multiply-linked inode whose other name may
be outside the private tree.
Checkpoints from schema 1 or 2 used the ambiguous v1 encoding and are
intentionally rejected; start a new session rather than relabeling their saved
fingerprints.

If a process stops after a certified payload was exported, `--resume` reuses
the existing output only when its complete tree fingerprint matches the frozen
certificate. It can then recreate a wholly missing `OUTPUT.repomin` sidecar or
accept a complete sidecar whose contents still match the certificate. A partial
or changed payload/sidecar is rejected without running another holdout sample.
Existing output remains an error for sessions without a successful holdout,
because those checkpoints do not contain a certified export fingerprint.

The versioned reduction strategy is part of that session identity. A checkpoint
created without the current strategy version, or by a different strategy, is
rejected because its completed-phase claims are not interchangeable with the
current fixed-point contract.

The effective ignored-basename set is also part of session identity. This keeps
the source fingerprint and the copied workspace semantics aligned when a
session is resumed.

The sorted exact ignored-path set is part of the same identity and is recorded
in the report. Changing either exclusion option on `--resume` is rejected
before another oracle command runs.

Explicit environment overrides are part of session identity through their
sorted names and digest. Resuming with a changed value or name is rejected
before another oracle command runs, without exposing the value in the
checkpoint.

For the host backend, every baseline, candidate, final-validation, and holdout
command runs in a fresh directory whose leaf name is exactly `OUTPUT.name`.
The versioned `host-output-basename-v1` policy and that basename are part of the
session identity and report, so changing the output basename on resume is
rejected. Unique internal parent directories still vary between samples. A
command that depends on an absolute path, parent-directory name, inode, or
device is outside this host guarantee; use the Docker backend, whose working
directory is always `/workspace`, when a fixed complete path is required.

`--match` searches the complete captured stdout and stderr. Build tools and
tracebacks may echo source lines or command arguments, so a short token can
remain present after the intended failure has changed. Prefer a pattern that
includes stable failure context and an end-of-line boundary; for exception
failures, also enable the corresponding structural signature option below.
Holdout certification measures the configured oracle, so it cannot repair an
underspecified match expression.

`--java-exception` learns the root Java exception from the repeated baseline
runs and requires every accepted candidate to preserve its class, normalized
message, and first three method frames. Source line numbers and Java module
prefixes are ignored. ReproMin reads both console stack traces and Maven
Surefire `TEST-*.xml` failures, so quiet Maven output can still be identified.
Leave this option off when exception messages intentionally contain unstable
request IDs, timestamps, or other per-run values.

`--python-exception` provides the equivalent guard for Python tracebacks. It
preserves the exception class, whitespace-normalized message, and three
innermost frames while ignoring absolute working directories and source line
numbers. Standard tracebacks, chained exceptions, Python 3.11 exception groups,
and pytest-rendered failures are supported. When one run reports multiple
failures, ReproMin prefers the exception whose class, message, or frames match
`--match`. The Java and Python signature options are mutually exclusive.

## Java attribution classpath

The Java source reducer can use pre-resolved dependencies during compiler
attribution. Pass one classpath entry per `--java-classpath PATH` occurrence:

```sh
repomin /path/to/project \
  --command './mvnw test' \
  --match 'NoSuchMethodError' \
  --java-classpath /opt/repro-deps/api.jar \
  --java-classpath /opt/repro-deps/classes
```

Each `PATH` is one atomic entry. ReproMin does not split an occurrence on `:`
or `;`, so a Unix filename containing a colon remains representable. Entries
are kept in argument order because classpath precedence is significant; two
entries that identify the same physical file or directory, including symlink
or hardlink aliases, are rejected rather than deduplicated. A relative entry is
resolved once against the original `SOURCE`
directory, never against the shell working directory or a temporary candidate
copy. Every entry must resolve to a readable existing regular file or
directory. A top-level symlink is fixed to its canonical target; a classpath
directory containing a nested symlink or special file is rejected. Regular
files are passed to javac without an extension or archive-format policy. Pass
dependency archives separately; a directory entry contributes compiled classes
but does not recursively add archives stored below it.

`--java-classpath` affects only the compiler API used by the host-side Java
structure analyzer. It does not modify `--command`, the oracle's runtime
classpath, or the contents and mounts of a Docker container. This remains true
with `--backend docker`: the entries must be readable on the host, and a path
that exists only in the image is invalid. ReproMin does not invoke Maven or
Gradle to discover dependencies. Resolve or stage the exact entries before
starting reduction.

For Maven test sources, dependencies can be staged outside `SOURCE` with a
pinned plugin goal and then supplied one archive at a time:

```sh
REPOMIN_CP_DIR="$(mktemp -d)"
./mvnw -q -f app/pom.xml \
  org.apache.maven.plugins:maven-dependency-plugin:3.8.1:copy-dependencies \
  -DincludeScope=test \
  -DoutputDirectory="$REPOMIN_CP_DIR"

set --
for repomin_jar in "$REPOMIN_CP_DIR"/*.jar; do
  set -- "$@" --java-classpath "$repomin_jar"
done

repomin . \
  --command './mvnw test' \
  --match 'NoSuchMethodError' \
  "$@"
```

For Gradle, use a trusted external task or init script to copy
`testCompileClasspath` (or `compileClasspath` for main-only source) into an
external directory, then build the same repeated argument list. ReproMin does
not parse build-tool console output as a classpath.

The ordered canonical paths and a content fingerprint of every file or
directory entry are part of persistent-session identity and are revalidated
before each Java analysis pass. A change detected before a Java analysis pass
aborts analysis; `--resume` rejects a changed path, order, or entry content.
Classpath entries are not specially copied into the checkpoint. Entries
outside `SOURCE` must remain host-readable; use a durable staging directory
instead of `mktemp` for a session that may be resumed. A directory fingerprint
recursively covers relative paths, entry types, permission bits, and
regular-file contents.

## Docker backend

ReproMin can run every baseline and candidate in a fresh container. Images
must already exist locally; it never pulls one automatically.

```sh
repomin /path/to/project \
  --command './mvnw test -Dtest=PaymentServiceTest' \
  --match 'NoSuchMethodError' \
  --backend docker \
  --docker-image eclipse-temurin:17-jdk \
  --output /path/to/payment-repro
```

Docker runs with networking disabled by default, a read-only container root,
all Linux capabilities dropped, no privilege escalation, a 512-process limit,
and a bounded temporary filesystem. Only the disposable candidate repository
is mounted writable. Use `--docker-network bridge` or `host` only when the
reproduction genuinely requires external services.

Each run receives a deterministic unique container name before the Docker CLI
starts. Timeout, resource, and interruption cleanup can therefore issue
`docker rm -f` even if Docker has not written its cidfile yet; after stopping
the client, ReproMin retries removal for a bounded settling period.

Before the first reproduction command, ReproMin resolves the supplied image
reference to Docker's immutable `sha256:...` image ID. Every container in that
process runs the ID rather than the possibly mutable tag. Reports record both
`image` and `image_id`; persistent sessions bind both values, so `--resume`
rejects a tag that now resolves to different image contents before sampling.

Resource limits can also be set per container:

```sh
repomin /path/to/project \
  --command 'python -m pytest' \
  --match 'RuntimeError: checkout failed' \
  --backend docker \
  --docker-image project-tests:local \
  --docker-cpus 2 \
  --docker-memory 1GiB \
  --docker-workspace-limit 2GiB \
  --docker-tmpfs-size 512MiB
```

CPU and memory use Docker's native limits; swap is capped at the same value as
memory. `--docker-workspace-limit` monitors the total logical size of the
writable repository and destroys a container that crosses it. The monitor is
sampled, so it is a practical runaway-write guard rather than a filesystem
quota. PID and `/tmp` limits default to 512 and 1 GiB and can be changed with
`--docker-pids-limit` and `--docker-tmpfs-size`. Resource-exhausted runs are
never accepted by the failure oracle, even if they printed matching output.

When no persistent `--session` is supplied, Docker Desktop sessions are
created beside the source repository so the path is inside the same shared
host directory. That disposable session is removed when the process exits
normally. The source parent, or the explicit `--session` path when one is
used, must be available to the selected Docker daemon as a bind-mount source.
Docker Desktop and Colima can have different shared-path settings; in
particular, do not assume that host `/tmp` is shared. A rejected bind mount is
an environment configuration error and no oracle sample is accepted from it.

## Current reduction components

1. Run the baseline more than once and require the configured minimum number
   of samples to have a non-zero exit plus the output regular expression and,
   optionally, a stable Java exception, Python exception, or process
   termination signature. Process mode can omit the output expression.
2. For Maven projects, try removing modules, dependencies, plugins, and
   properties using an XML parser.
3. For Gradle Groovy and Kotlin DSL builds, lex balanced syntax and try removing
   included modules, dependencies, plugins, repositories, configurations, and
   properties.
4. For Python projects, structurally reduce PEP 621, Poetry, PDM, dependency
   group, and uv declarations in `pyproject.toml`, plus logical lines and local
   include chains in `requirements*.txt`.
5. For Node.js projects, structurally reduce dependency, script, workspace,
   bundle/file, `resolutions`, and `overrides` entries in each valid
   `package.json`. Lockfiles and runtime/exports metadata remain untouched.
6. For PHP Composer projects, structurally reduce package requirement,
   replacement/conflict/provide, script, and repository entries in each valid
   `composer.json`. Autoload, arbitrary extra metadata, and `composer.lock`
   remain untouched.
7. For .NET projects, structurally reduce selected MSBuild item entries in
   `.csproj`, `.fsproj`, and `.vbproj` files plus shared `Directory.Build.props`
   files, including package, project, and framework references plus content
   items. Property groups, imports, and arbitrary build metadata remain
   untouched.
8. For Ruby/Bundler projects, structurally reduce complete single-line `gem`
   declarations in `Gemfile`, `gems.rb`, and `Gemfile.*` manifests. Multiline
   Ruby calls, arbitrary Ruby code, and `Gemfile.lock` remain untouched.
9. For Rust projects, structurally reduce dependency tables and workspace
   `members`/`exclude` arrays in each `Cargo.toml`. `Cargo.lock`, features, and
   arbitrary metadata remain untouched.
10. For Go projects, structurally reduce `require`, `replace`, `exclude`, and
   `retract` entries in each `go.mod`, plus `use` and workspace-level `replace`
   entries in `go.work`. `go.sum`, module declarations, and Go version/toolchain
   directives remain untouched.
11. Apply hierarchical delta debugging to directories and files.
12. When JDK 11 or newer is available, compile the analysis helper with
   `javac --release 11` and use the JDK syntax tree API to remove Java imports,
   type members, statements, annotations, parameters, and call arguments, then
   simplify binary, conditional, cast, unary, and literal expressions. When
   attribution is safe, compiler symbols let ReproMin remove an unused
   constructor parameter or an unused parameter on an eligible non-native
   method together with every resolved source call argument as one atomic
   candidate. Eligible methods include `static` and `private` methods plus
   closed-dispatch instance methods declared in an ordinary top-level or member
   class when either the method or its class is `final`.
10. For Python projects, use the standard-library AST to remove imports,
   definitions, and statements with UTF-8 range validation.
11. Requeue components dirtied by other accepted changes and run the ordinary
   final consistency validation.
12. If configured, freeze the payload and run the one-time fixed-size holdout,
   then export the exact certified tree and emit the sibling metadata directory
   only after certification succeeds.

An opt-in semantic reducer seam (`--semantic-reducer http`) lets a
provider-agnostic backend propose source edits through the same oracle
pipeline. It is disabled by default, has no third-party runtime dependency,
and never binds a default provider. See `docs/LLM_REDUCTION.md`.

Every baseline, candidate, and final command runs in a disposable copy. Files
created or changed by the command are never promoted into the reduced output.

Each reducer runs to a local fixed point. Maven, Gradle, Python manifest, Node
manifest, Cargo manifest, Go manifest, and Python source reducers repeatedly
discover targets after accepted hierarchical
batches. The file reducer alternates directory and file minimization until
neither can unlock the other. Java uses epochs: a stable target rejected in one
epoch is deferred while other Java edits are accepted, then reconsidered in the
next epoch. It stops only after a complete epoch accepts nothing.

The global scheduler is a dirty worklist. When one locally stable reducer
accepts a mutation, every other reducer is queued again because that change may
unlock its previously rejected targets. Termination means every enabled reducer
has reached local stability after the last change made by another reducer.

## Reduction accounting

The JSON report identifies this algorithm as
`hierarchical-fixed-point-v2` and contains additive `phase_statistics`. Per
phase, `attempts` counts logical candidates, including no-ops and combination
candidates. `oracle_sample_uses` counts logical sample uses, while
`oracle_samples` counts samples that actually executed the command;
`cache_hits` is the difference. Therefore:

```text
attempts = no_op + rejected + accepted + superseded + aborted
oracle_sample_uses = oracle_samples + cache_hits
```

`superseded` means a candidate passed the oracle but was not promoted because a
lower-index passing candidate or a separately validated combination was chosen.
`samples_saved` counts planned repeated samples skipped by early stopping.
`oracle_seconds` sums command durations and can exceed phase wall time under
parallel execution. Byte counts are net regular-file changes per reducer pass.

`phase_statistics.coverage` is `complete` only when the checkpoint contains the
whole phase-statistics history and no phase was interrupted. Compatible older
checkpoints without these counters, and sessions resumed from an active phase,
report `partial`; ReproMin does not invent missing historical measurements. A
hard-interrupted active pass is classified as aborted when it is restored.

## Deliberate v0 limits

- Host execution remains the default for compatibility.
- Additional input exclusions are exact recursively applied basenames supplied
  with repeatable `--ignore NAME`, exact repository-relative subtrees supplied
  with repeatable `--ignore-path RELATIVE_PATH`, or explicit gitignore-style
  rule files supplied with `--gitignore`, `--gitignore-file PATH`, or
  `--gitignore-recursive`. The rule file reader implements the documented
  subset above, not git's full escaping semantics. Repeatable `--keep
  RELATIVE_PATH` protects an exact path from file deletion without disabling
  content-level reducers.
- Explicit reproduction environment overrides use repeatable `--env
  NAME=VALUE`; values are injected into host/Docker commands but are represented
  only by names and a SHA-256 digest in reports and checkpoints.
- Docker images must contain `/bin/sh` and all required build dependencies.
- Structured manifest adapters currently support Maven, Gradle, Python and
  Pipenv,
  npm-compatible `package.json` files, Composer `composer.json` files, MSBuild
  project files and shared `Directory.Build.props` files, Ruby/Bundler Gemfiles,
  Cargo manifests, and Go
  module/workspace manifests. Node lockfiles, `composer.lock`, `Gemfile.lock`,
  `Cargo.lock`, `Pipfile.lock`, and `go.sum` are intentionally not rewritten by
  their adapters.
  Composer autoload and arbitrary `extra` metadata remain unchanged. MSBuild
  property groups, imports, and arbitrary metadata remain unchanged. Ruby
  multiline calls and arbitrary Ruby code remain unchanged. Go workspace
  manifests are limited to `use` and workspace-level `replace` entries; other
  `go.work` metadata remains unchanged.
- Failure matching is based on exit status and, unless `--exit-code` or
  `--process-failure` is enabled, a required regular expression.
- Learned failure signatures support Java exceptions, Python tracebacks, direct
  POSIX signals, Windows status codes, and exact non-zero exit codes.
- Sampling uses maximum run counts and fixed-size exact one-sided rate gates;
  Wilson lower bounds remain descriptive report metrics. Candidate prefixes can
  also use a Jeffreys beta-binomial mixture
  confidence sequence for conservative, anytime-valid early acceptance without
  enlarging the fixed-size pass/fail acceptance set. Coverage is per candidate
  by default; `--run-confidence` adds harmonic alpha spending across the
  candidate families in one reduction session. This guarantee is conditional
  on the documented fresh-sample model and is not a correctness guarantee.
- Native source reducers support Java declarations and expressions (requires
  JDK 11+) and Python statements (standard library AST). The Java analysis
  helper itself is compiled with `javac --release 11`; that setting does not
  select the source release of the project being reduced.
- Coordinated Java parameter removal supports constructors, non-native `static`
  and `private` methods, and closed-dispatch instance methods. An instance
  method is eligible only when it is declared by an ordinary top-level or member
  class and either the method or that class is `final`; a source-local override
  family may instead be coordinated when it has one package-visible root and a
  `final` leaf method or owner. Current overrides and interface implementations
  against external declarations, open virtual methods, methods whose reduced
  signature would newly override or implement an inherited contract, and
  generic-erasure or bridge clashes are excluded. Enum, record, local, and
  anonymous-class instance methods are also excluded. The parameter must remain
  unreferenced in the executable body, the executable must not be used by a
  direct method or constructor reference, and the group must contain at least one
  source declaration and at least one resolved direct call-site argument edit.
  Closed source override families include every declaration in the atomic
  change set.
  Anonymous-class construction blocks all constructor groups for its source
  base type, and record constructors are not supported.
- Java compiler attribution uses the remaining source files and any explicit
  `--java-classpath` entries; build classpaths are never discovered
  automatically. External binary types can improve resolution, but an external
  executable never forms a source mutation group. If attribution throws,
  produces an unrecoverable compiler diagnostic, or finds an `ERROR` type in a
  source type hierarchy or anywhere on a method-invocation,
  constructor-expression, or member-reference path, coordinated candidates are
  disabled globally for that analysis pass; syntax-only Java candidates remain
  available. Otherwise unresolved links are never guessed. Reflection,
  string- or `MethodType`-based `MethodHandles.Lookup` calls, generated callers,
  JNI and framework entry points, precompiled external callers that retain the
  old descriptor, and other unobserved call sites are left to the reproduction
  oracle rather than rewritten speculatively. The oracle preserves only the
  configured command,
  exit behavior, output match, and optional failure signature; it does not prove
  source or ABI compatibility, successful compilation, or behavior outside the
  exercised path. Use a command that compiles and tests the affected code, and
  enable `--java-exception` when the exception identity is part of the
  reproduction.
- Parallel jobs isolate repository files, not external process side effects.
- On POSIX, a command can deliberately call `setsid()` or otherwise leave its
  assigned process group. Output capture remains bounded and cannot hang the
  reducer, but that escaped process may survive; use Docker or a disposable VM
  for commands that daemonize or are not trusted.
- Persistent sessions do not checkpoint external services, databases, network
  responses, or processes started by the reproduction command.
- Holdout confidence is a single-attempt guarantee conditional on fresh iid
  samples. Repeating failed attempts in new sessions, retaining only successful
  CI jobs, or changing the artifact after seeing holdout results invalidates
  that interpretation.

These limits keep mutations auditable while the benchmark and failure-oracle
coverage expands.

## Development

```sh
python3 scripts/check_contribution.py
python3 scripts/check_contribution.py --with-benchmarks
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for extension points and project rules,
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for reducer invariants, and
[SECURITY.md](SECURITY.md) before running commands from an untrusted project.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - core invariants, reducer
  behavior, and report/checkpoint schema.
- [docs/REPORT_SCHEMA.md](docs/REPORT_SCHEMA.md) - versioned `report.json`
  fields, accounting identities, holdout evidence, and consumer guidance.
- [docs/REPLAY.md](docs/REPLAY.md) - fresh-copy replay, environment checks,
  security boundaries, and machine-readable evidence.
- [docs/LLM_REDUCTION.md](docs/LLM_REDUCTION.md) - optional semantic reducer
  seam and its provider-agnostic contract.
- [docs/DOCTOR.md](docs/DOCTOR.md) - read-only toolchain and baseline preflight.
- [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md) - use ReproMin in CI to upload
  a minimized failure reproduction and its report.
- [docs/QUICKSTART.md](docs/QUICKSTART.md) - complete a self-contained first
  reduction and validate its evidence in five minutes.
- [docs/QUICKSTART.zh-CN.md](docs/QUICKSTART.zh-CN.md) - install and run a
  minimal reduction with Chinese guidance.
- [docs/ROADMAP.md](docs/ROADMAP.md) - current priorities, future directions,
  and explicit non-goals.
- [benchmarks/README.md](benchmarks/README.md) - real fixtures and acceptance
  gates.
- [CONTRIBUTING.md](CONTRIBUTING.md) - project rules and extension points.
- [CONTRIBUTORS.md](CONTRIBUTORS.md) - community contributions and the complete
  GitHub contributors graph.
- [SUPPORT.md](SUPPORT.md) - where to ask questions and how to prepare useful
  issue reports.
- [docs/REAL_FAILURE_PILOT.md](docs/REAL_FAILURE_PILOT.md) - how to share a
  sanitized real CI or dependency failure.
- [docs/CASE_STUDY_TSDOWN_979.md](docs/CASE_STUDY_TSDOWN_979.md) - a public
  upstream pilot, its reduction evidence, and oracle-design lessons.
- [CHANGELOG.md](CHANGELOG.md) - notable changes by release.
- [docs/RELEASING.md](docs/RELEASING.md) - GitHub Release checklist and artifact verification.

## Contributing

ReproMin accepts bug reports, feature requests, reducer adapters, and
documentation improvements. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) and the repository
[issue templates](.github/ISSUE_TEMPLATE). Participation is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md). Security reports should follow
[SECURITY.md](SECURITY.md).

For general usage questions and design conversations, use [GitHub
Discussions](https://github.com/fly1d/repomin/discussions). New contributors can
pick a scoped task from [Good first issues](docs/GOOD_FIRST_ISSUES.md), and
support details are collected in [SUPPORT.md](SUPPORT.md). Users with a real
CI or dependency failure can follow the [pilot guide](docs/REAL_FAILURE_PILOT.md)
before sharing a sanitized workflow in issue #11. Users who tried ReproMin but
do not have a publishable failure can use the [user workflow feedback
template](https://github.com/fly1d/repomin/issues/new?template=adoption_feedback.md)
to report what was useful, confusing, or incompatible.

## License

Apache-2.0.
