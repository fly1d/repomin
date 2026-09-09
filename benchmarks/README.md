# Benchmarks

ReproMin ships small, deterministic fixtures for checking reducer behavior and
release regressions. The default suite is network-free and runs every fixture
in a fresh temporary output directory.

## Run the suite

From the repository root:

```sh
python3 benchmarks/run_offline.py
```

List or select checks without running unrelated fixtures:

```sh
python3 benchmarks/run_offline.py --list
python3 benchmarks/run_offline.py --only python-pyproject --only text-lines
python3 benchmarks/run_offline.py --exclude native-process
```

Unknown names, overlapping `--only` and `--exclude` values, and an empty
selection are rejected. Toolchain-dependent fixtures are skipped when their
required command is unavailable; any failed check makes the runner exit
non-zero.

Each completed check validates the generated report and payload fingerprint,
then runs an independent oracle against the exported payload. This verifies a
known fixture contract; it is not a performance claim or proof that every real
repository in that ecosystem can be reduced.

## Machine-readable results

Write a schema-versioned JSON summary for CI or repeated comparisons:

```sh
python3 benchmarks/run_offline.py \
  --json-output /tmp/repomin-benchmarks.json
```

The summary records the ReproMin and Python/platform identity, aggregate
pass/skip/fail counts, elapsed time and status per check, and the exact
selection. Full reproduction output remains in the command logs; errors in the
JSON are bounded.

Compare runs made with the same selection and environment:

```sh
python3 benchmarks/compare.py \
  --require-same-selection \
  /tmp/repomin-benchmarks-run-1.json \
  /tmp/repomin-benchmarks-run-2.json
```

Add `--json-output PATH` when another tool will consume the comparison. The
comparison is descriptive and reports missing fixtures explicitly. Use
repeated runs before interpreting elapsed-time differences.

## Fixture catalog

`python3 benchmarks/run_offline.py --list` is the source of truth for runnable
check names. Several checks intentionally share one fixture.

| Fixture | Contract | Extra requirement |
| --- | --- | --- |
| [`input-controls`](input-controls/) | Exact exit code, ignore rules, protected paths, and bounded reductions | Python |
| [`semantic-stub`](semantic-stub/) | Local OpenAI-compatible proposal endpoint behind the ordinary oracle | Python |
| [`text-lines`](text-lines/) | Line reduction of one selected UTF-8 file | Python |
| [`report-replay`](report-replay/) | Reduction, report validation, replay, and mismatch classification | Python |
| [`python-requirements`](python-requirements/) | Nested requirements and constraints includes | Python |
| [`python-pyproject`](python-pyproject/) | Dependencies across common `pyproject.toml` tables | Python |
| [`node-package`](node-package/) | `package.json` dependencies, scripts, workspaces, and overrides | Python oracle; Node is not invoked |
| [`pipenv-package`](pipenv-package/) | Runtime and development entries in `Pipfile` | Python oracle; Pipenv is not invoked |
| [`composer-package`](composer-package/) | Composer requirements, scripts, repositories, and autoload metadata | Python oracle; PHP is not invoked |
| [`dotnet-project`](dotnet-project/) | SDK-style project properties and item groups | Python oracle; .NET is not invoked |
| [`dotnet-directory-build-props`](dotnet-directory-build-props/) | Shared MSBuild properties and item groups | Python oracle; .NET is not invoked |
| [`ruby-gemfile`](ruby-gemfile/) | Bundler source and gem declarations | Ruby |
| [`cargo-workspace`](cargo-workspace/) | Workspace members and local path dependencies | Cargo, offline mode |
| [`go-module`](go-module/) | Requirements, replacements, exclusions, and local modules | Go, proxy disabled |
| [`native-process`](native-process/) | Direct POSIX signal preservation and replay | POSIX host with `SIGABRT` |

The repository also contains integration fixtures that are deliberately kept
outside the default runner:

| Fixture | Why it is separate |
| --- | --- |
| [`maven-multimodule`](maven-multimodule/) | Exercises Maven and attributed Java reduction with prepared dependencies |
| [`gradle-multimodule`](gradle-multimodule/) | Requires a prepared Gradle image/cache and multi-module build |
| [`python-fastapi`](python-fastapi/) | Uses a locally built Docker image; the initial image build needs network access |
| [`docker-python`](docker-python/) | Exercises the Docker execution boundary rather than an offline adapter |

Per-fixture READMEs describe exact commands, expected retained files, and trust
boundaries where those details are useful. User-facing workflows belong in the
[examples guide](../docs/EXAMPLES.md); reducer guarantees belong in the
[architecture reference](../docs/ARCHITECTURE.md).

## Adding or changing a fixture

Keep a fixture small, deterministic, and network-free unless its README clearly
documents why it is an integration-only exception. Every runnable fixture must:

1. Preserve one distinctive failure contract and reject a nearby different
   failure.
2. Assert the expected minimized payload, not only a successful ReproMin exit.
3. Validate the report and rerun an independent oracle against the payload.
4. Avoid credentials, private data, mutable remote resources, and ambient user
   configuration.

Run the focused check while developing, then run the complete suite:

```sh
python3 benchmarks/run_offline.py --only NAME
python3 scripts/check_contribution.py --with-benchmarks
```

See [CONTRIBUTING.md](../CONTRIBUTING.md#benchmarks-and-external-projects) for
the proposal and review workflow.
