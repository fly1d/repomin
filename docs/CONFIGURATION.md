# Versioned reduction configuration

ReproMin can load the semantic part of a reduction from a strict JSON file.
The same file can drive a local reduction, a Doctor preflight, and the GitHub
Action without translating the failure contract into three sets of options.

The file is intentionally a reduction specification, not a general CLI
configuration file. Repository location, output location, checkpoint state,
verbosity, credentials, and provider-specific semantic-reducer settings stay
outside it so one reviewed specification can be reused in different runners.

## Minimal workflow

Save a file such as `.repomin.json` in the repository:

```json
{
  "schema_version": 1,
  "failure": {
    "command": "python -m pytest -q tests/test_checkout.py",
    "match": "ValueError",
    "signature": "python_exception"
  },
  "execution": {
    "timeout_seconds": 120,
    "jobs": 2,
    "cache": true
  },
  "sampling": {
    "baseline_runs": 2,
    "candidate_runs": 1
  },
  "reduction": {
    "adapter": "python",
    "source_reducer": "python",
    "max_attempts": 500
  },
  "inputs": {
    "ignore_names": [".env"],
    "keep_paths": ["tests/test_checkout.py"],
    "gitignore": true
  }
}
```

Run the read-only preflight and the reduction with the same specification:

```sh
repomin doctor . \
  --config .repomin.json \
  --output /tmp/checkout-repro \
  --json

repomin . \
  --config .repomin.json \
  --output /tmp/checkout-repro
```

The CLI resolves the configuration path from its current working directory;
the path does not become relative to `source`. Both `--config PATH` and
`--config=PATH` are accepted. The option may appear once, before or after the
source argument.

`--config` is supported by reduction and `repomin doctor`. It is not an input
to `report validate`, `report replay`, `report compare`, or `completion`.
ReproMin preserves `failure.command` verbatim; it does not translate shell
syntax or provision its toolchain across operating systems. Reuse a file only
on runners where that command and environment are valid, and let Doctor check
the baseline before reduction.

## Ownership and overrides

There is no precedence or merge layer between a configuration file and
semantic CLI options. When `--config` is present, any other semantic option,
including one for a field omitted from the JSON file, is rejected. This fails
closed instead of silently changing a reviewed failure contract:

```sh
# Invalid: jobs is owned by the configuration file.
repomin . --config .repomin.json --jobs 4
```

Runtime placement and checkpoint controls remain CLI-owned. A reduction may
combine the specification with the positional `source`, `--output`,
`--session`, `--resume`, and `--verbose`. Doctor may combine it with the
positional `source`, `--output`, and `--json`. Help and version requests do not
read the configuration file.

The v1 schema deliberately excludes these CLI-only settings:

- `source`, `output`, `session`, `resume`, and `verbose`;
- explicit environment entries from `--env`;
- `semantic-reducer`, its endpoint, model, and timeout;
- host-side Java attribution paths from `--java-classpath`.

They cannot be added as unknown JSON keys. Semantic CLI-only options such as
`--env`, `--semantic-reducer`, and `--java-classpath` also cannot be layered
beside `--config` in v1. Configuration mode fixes the semantic reducer to
`none` and ignores defaults from `REPOMIN_SEMANTIC_REDUCER`,
`REPOMIN_SEMANTIC_ENDPOINT`, `REPOMIN_SEMANTIC_MODEL`, and
`REPOMIN_SEMANTIC_TIMEOUT`. Use an ordinary flag-based invocation when the
opt-in HTTP semantic reducer is required.

## Document contract

The root value must be a JSON object with exactly these keys:

| Key | Required | Value |
| --- | --- | --- |
| `schema_version` | yes | The JSON integer `1` |
| `failure` | yes | Failure command and oracle object |
| `execution` | no | Runner, Docker, concurrency, and cache object |
| `sampling` | no | Baseline, candidate, confidence, and holdout object |
| `reduction` | no | Reducer selection and budget object |
| `inputs` | no | Ignore, keep, text, and gitignore controls object |

Missing optional fields bind the defaults of the installed ReproMin version.
Important current defaults include a 120-second command timeout, host execution,
one candidate job, enabled result caching, two baseline runs, one candidate run,
`auto` adapter and source-reducer selection, Docker network `none`, a
512-process Docker limit, a 1 GiB Docker `/tmp`, and confidence `0.95`.
Defaults may evolve in a future compatible release. Pin a reviewed ReproMin
release or commit in production, and spell out behavior-critical fields when
the same effective plan must survive an upgrade.

### Failure

| JSON field | Constraint | Equivalent CLI option |
| --- | --- | --- |
| `failure.command` | Required non-empty string without NUL | `--command` |
| `failure.match` | Non-empty regular-expression string | `--match` |
| `failure.exit_code` | JSON integer | `--exit-code` |
| `failure.signature` | `java_exception`, `python_exception`, or `process_failure` | Corresponding signature flag |

At least one of `match`, `exit_code`, or `signature: "process_failure"` is
required. Java and Python exception signatures refine a basic match or exact
exit-code oracle and do not define one alone. Only one signature can be named
because `signature` is a single field. `process_failure` is incompatible with
`exit_code`, but it may be combined with `match`.

`match` is evaluated against combined stdout and stderr and, without an exact
exit code, still requires an ordinary non-zero exit. Java and Python modes
learn the normalized exception class, message, and relevant frames from the
baseline. Process mode instead learns the exact normalized termination
signature. These are the same fail-closed oracle rules described in
[ARCHITECTURE.md](ARCHITECTURE.md#failure-oracle).

### Execution

| JSON field | Constraint | Equivalent CLI option | Doctor |
| --- | --- | --- | --- |
| `execution.timeout_seconds` | Positive finite number | `--timeout` | applied |
| `execution.backend` | `host` or `docker` | `--backend` | applied |
| `execution.jobs` | Positive integer | `--jobs` | validated only |
| `execution.cache` | Boolean; `false` disables caching | `--no-cache` when false | validated only |
| `execution.docker.image` | Non-empty string | `--docker-image` | applied |
| `execution.docker.network` | `none`, `bridge`, or `host` | `--docker-network` | applied |
| `execution.docker.cpus` | Positive finite number | `--docker-cpus` | applied |
| `execution.docker.memory` | Positive byte-size string | `--docker-memory` | applied |
| `execution.docker.pids_limit` | Positive integer | `--docker-pids-limit` | applied |
| `execution.docker.tmpfs_size` | Positive byte-size string | `--docker-tmpfs-size` | applied |
| `execution.docker.workspace_limit` | Positive byte-size string | `--docker-workspace-limit` | applied |

The `docker` object is allowed only with `backend: "docker"`, and Docker
execution requires `docker.image`. Byte-size values use the CLI syntax, for
example `512MiB`, `2GiB`, or `4GB`.

Doctor checks the entire object so the same file cannot hide invalid
reduce-only values, but it expands only the options relevant to preflight.
It does not run candidate jobs or use the result cache. It does apply the
configured Docker CPU, memory, process, temporary-filesystem, and workspace
limits to its optional baseline so that check matches the intended reduction
runner.

### Sampling

| JSON field | Constraint | Equivalent CLI option | Doctor |
| --- | --- | --- | --- |
| `sampling.baseline_runs` | Positive integer | `--baseline-runs` | applied |
| `sampling.min_baseline_passes` | Positive integer, at most `baseline_runs` | `--min-baseline-passes` | applied |
| `sampling.candidate_runs` | Positive integer | `--candidate-runs` | validated only |
| `sampling.min_candidate_passes` | Positive integer, at most `candidate_runs` | `--min-candidate-passes` | validated only |
| `sampling.min_baseline_rate` | Finite number strictly between 0 and 1 | `--min-baseline-rate` | applied |
| `sampling.min_candidate_rate` | Finite number strictly between 0 and 1 | `--min-candidate-rate` | validated only |
| `sampling.confidence` | Finite number strictly between 0 and 1 | `--confidence` | applied |
| `sampling.run_confidence` | Finite number strictly between 0 and 1 | `--run-confidence` | validated only |
| `sampling.holdout.runs` | Required positive integer when `holdout` exists | `--holdout-runs` | validated only |
| `sampling.holdout.min_rate` | Required finite number in `(0, 1)` | `--min-holdout-rate` | validated only |
| `sampling.holdout.confidence` | Optional finite number in `(0, 1)` | `--holdout-confidence` | validated only |

`min_baseline_passes` cannot exceed the configured `baseline_runs`; when the
run count is omitted, the default of two is used for this check. The equivalent
rule uses the default of one for candidate runs. `run_confidence` requires
`min_candidate_rate`. The holdout object requires both `runs` and `min_rate`.
Doctor applies the baseline run count, minimum pass count, minimum rate, and
confidence to its fresh-copy baseline. Candidate, run-wide, and holdout fields
remain reduce-only.

Without an explicit minimum pass count, all configured samples must pass. When
a baseline or candidate minimum rate is present without its corresponding
minimum pass count, the count floor becomes one and the statistical gate
provides the additional requirement. Holdout is disabled when its object is
absent; its confidence defaults to `0.95` when enabled.

The configuration reader validates types, direct dependencies, and the
best-case attainability of every baseline, ordinary candidate, first run-wide
candidate family, and holdout rate plan. Doctor performs this validation even
for reduce-only fields, so it cannot approve a specification that reduction
would reject before sampling. Runtime statistical gates still use the same
exact decisions as an equivalent flag-based invocation; later run-wide
candidate families are checked when their family index is allocated.

### Reduction

| JSON field | Constraint | Equivalent CLI option | Doctor |
| --- | --- | --- | --- |
| `reduction.adapter` | `auto`, `none`, `maven`, `gradle`, `python`, `pipenv`, `node`, `composer`, `dotnet`, `ruby`, `cargo`, or `go` | `--adapter` | applied |
| `reduction.source_reducer` | `auto`, `none`, `java`, or `python` | `--source-reducer` | applied |
| `reduction.max_attempts` | Positive integer | `--max-attempts` | validated only |
| `reduction.max_duration_seconds` | Positive finite number | `--max-duration` | validated only |

Doctor detects and checks the selected reducers but does not consume the
attempt or wall-clock budget because it performs no reduction.

### Inputs

| JSON field | Constraint | Equivalent CLI option |
| --- | --- | --- |
| `inputs.ignore_names` | List of exact portable basenames | Repeated `--ignore` |
| `inputs.ignore_paths` | List of portable repository-relative paths | Repeated `--ignore-path` |
| `inputs.keep_paths` | List of portable repository-relative paths | Repeated `--keep` |
| `inputs.text_files` | List of portable repository-relative paths | Repeated `--text-file` |
| `inputs.gitignore` | Boolean | `--gitignore` when true |
| `inputs.gitignore_files` | List of portable repository-relative paths | Repeated `--gitignore-file` |
| `inputs.gitignore_recursive` | Boolean | `--gitignore-recursive` when true |

All input fields are applied by both reduction and Doctor. List entries must be
non-empty strings. Paths use `/`, are relative to the source repository, and
cannot contain absolute or drive syntax, `.` or `..` components, empty
components, backslashes, glob characters, unsafe control characters, Windows
reserved names, or components ending in a space or period. `ignore_names`
accepts one basename rather than a path.

These are the same exact-path and exclusion contracts as the CLI. In
particular, a protected path can override an exclusion for reachability, while
other enabled reducers may still edit a protected file. See the input-control
sections in the [README](../README.md) and [Doctor guide](DOCTOR.md).

## Strict validation

Schema v1 is fail-closed:

- the file must be readable UTF-8 JSON;
- the root and every section must be objects with only documented keys;
- duplicate keys are rejected at the object where they occur;
- JSON comments, trailing commas, `NaN`, and positive or negative infinity are
  rejected;
- JSON booleans are not accepted where an integer or number is required;
- required strings cannot be empty or contain NUL;
- unknown schema versions and unknown fields are errors.

An invalid configuration or a forbidden CLI combination returns exit code `2`
before a reduction or Doctor baseline begins. The Action fails before it
publishes an artifact.

## GitHub Action

The Action accepts the repository-relative path through `config`:

```yaml
- name: Minimize failure
  id: minimize
  uses: fly1d/repomin@<reviewed-release-or-full-commit>
  with:
    config: .github/repomin.json
    source: .
    artifact-name: minimized-reproduction
    step-summary: true
```

The Action resolves `config` inside `GITHUB_WORKSPACE`. The path must be a
portable repository-relative path, every component must be free of symbolic
links, and the result must be a readable regular file inside the workspace.
This boundary is stricter than the local CLI, which accepts a caller-selected
path and expands `~`.

With `config`, all direct semantic Action inputs must remain at their declared
defaults. The Action does not merge or override them. `source`, `output`,
`python-version`, `artifact-name`, and `step-summary` remain Action-owned and
may be set alongside the file. `source` and an explicit `output` must also be
portable repository-relative paths: symbolic-link components and resolved
locations outside `GITHUB_WORKSPACE` are rejected. See the
[GitHub Action guide](GITHUB_ACTION.md) for the complete conflict list and
artifact contract.

## Security and privacy

Strict JSON and path validation prevent ambiguous configuration; they do not
make the configured command safe. `failure.command` is intentionally executed
by a shell. Treat a configuration change with the same care as a workflow,
build script, or executable code change.

The host backend is not a sandbox. Its command retains the invoking user's
filesystem, environment, credentials, and network access. Docker narrows that
access but still shares the host kernel and Docker daemon trust boundary.
Review the repository, command, image, and network policy before execution and
follow [SECURITY.md](../SECURITY.md) for untrusted inputs.

Do not put credentials or private values in a versioned specification. Schema
v1 intentionally has no environment-value field. The generated report retains
the effective command and optional match expression under the existing report
contract, so review both the report and payload before sharing or uploading an
artifact.

A configuration file below `source` is also an ordinary repository input. It
may remain in the exported payload; never rely on reduction to remove it. When
the file must be excluded, place it outside the selected source tree or list
its source-relative path in `inputs.ignore_paths`.

## Migration and compatibility

Configuration files are opt-in. Existing flag-based invocations keep their
current behavior. To migrate one, move all supported semantic options into a
v1 file in a single change and leave only runtime placement or checkpoint
options on the command line. Do not plan a staged rollout that depends on CLI
values overriding the file; that combination is deliberately rejected.

The file is expanded into ordinary CLI arguments before normal parsing. The
reduction engine, report, and persistent-session identity therefore receive
the same effective values as a flag-based run. The configuration subsystem
does not add the raw path or JSON text to the report or session identity.
Normal source copying remains independent of that metadata rule. Resuming a
session still fails when a session-bound effective value has changed.

`schema_version` belongs to this input format. It is independent of the report,
validation-summary, comparison, and checkpoint schema versions. ReproMin only
accepts configuration schema version 1 today; it never guesses how to interpret
an unknown version or silently ignores a newer field. Upgrade the ReproMin
binary and migrate the file together when a future schema is introduced. An
older release that does not implement `--config` must continue using CLI
options.
