# Examples

> **Required before you start:** `--output` must be outside the source
> repository (the directory passed as `SOURCE`). ReproMin rejects an output
> path inside that tree. Use a sibling directory or an absolute temporary
> directory, such as `--output ../example-minimal` or
> `--output "$out_parent/result"`; do not use `--output ./reduced`. ReproMin
> writes the evidence sidecar next to the payload at
> `<output>.repomin/`, so that directory stays outside the source tree too.

The host-backend examples are self-contained and use only Python. Run them from
a scratch directory after installing ReproMin in editable mode. The Docker and
semantic examples near the end use the repository fixtures so their trust
boundaries and provider contract are explicit.

## Shrink a Python failure to its required files

Create a small failing project:

```sh
mkdir example && cd example
cat > reproduce.py <<'PY'
from pathlib import Path
import sys

if not Path("required.txt").exists():
    print("DIFFERENT_FAILURE", file=sys.stderr)
    raise SystemExit(2)
print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(1)
PY
echo keep-me > required.txt
echo noise > unused-a.txt
echo more-noise > unused-b.txt
```

Before reduction the tree is:

```text
reproduce.py
required.txt
unused-a.txt
unused-b.txt
```

Run:

```sh
repomin . \
  --command 'python3 reproduce.py' \
  --match 'ORIGINAL_FAILURE' \
  --source-reducer none \
  --adapter none \
  --output ../example-minimal
```

The reduced tree keeps only the command entry point and the file the oracle
actually needs:

```text
reproduce.py
required.txt
```

The sibling `../example-minimal.repomin/report.json` records the attempts,
accepted mutations, and phase accounting.

## Shrink a requirements include chain

This copy-paste example exercises the Python requirements adapter against a
network-free fixture. It follows a two-level include chain and a shared
constraints file:

```text
requirements.txt
  -r requirements/runtime.txt
  -c constraints.txt
requirements/runtime.txt
  repomin-runtime==1.2.3 \
    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  -r ci.txt
requirements/ci.txt
  repomin-ci-runner==4.5.0
constraints.txt
  repomin-runtime<2
  repomin-ci-runner<5
```

The fixture's oracle requires every link above, including the backslash-
continued hash. Unused requirements and package-index options are deliberately
present as removable noise. From the repository root, run:

```sh
out_parent="$(mktemp -d /tmp/repomin-requirements.XXXXXX)"
PYTHONPATH=src python3 -m repomin benchmarks/python-requirements \
  --command 'python3 reproduce.py' \
  --match 'ORIGINAL_FAILURE' \
  --adapter python \
  --source-reducer none \
  --output "$out_parent/result"
```

The command does not install anything or contact a package index. The
minimized payload should contain exactly the five files below; inspect it with
the following command:

```sh
find "$out_parent/result" -type f -print | sort
```

```text
constraints.txt
reproduce.py
requirements.txt
requirements/ci.txt
requirements/runtime.txt
```

The sidecar at `$out_parent/result.repomin/report.json` can be checked without
rerunning the reducer:

```sh
PYTHONPATH=src python3 -m repomin report validate \
  "$out_parent/result.repomin/report.json" \
  --payload "$out_parent/result" --json
```

The validator checks the report schema and payload fingerprint; the fixture's
oracle only checks the dependency declarations and exits with
`ORIGINAL_FAILURE`.

## Shrink a Pipenv `Pipfile`

For a network-free reproduction that only needs one package declaration, run
the dedicated Pipenv adapter:

```sh
repomin . \
  --command 'python3 reproduce.py' \
  --match 'ORIGINAL_FAILURE' \
  --adapter pipenv \
  --source-reducer none \
  --output ../pipenv-minimal
```

Only direct entries in `[packages]`, `[dev-packages]`, and `[requires]` are
eligible. Pipenv source settings and `Pipfile.lock` are preserved.

## Shrink a Cargo workspace without network access

The repository includes a local-only Rust workspace with one required path
dependency, one unused path dependency, and unrelated workspace members. It
does not contact crates.io when Cargo runs in offline mode:

```sh
out_parent="$(mktemp -d /tmp/repomin-cargo-workspace.XXXXXX)"
PYTHONPATH=src python3 -m repomin benchmarks/cargo-workspace \
  --command 'CARGO_NET_OFFLINE=true cargo run -q -p app' \
  --match 'ORIGINAL_FAILURE' \
  --adapter cargo \
  --source-reducer none \
  --output "$out_parent/result"
```

The command intentionally panics with `ORIGINAL_FAILURE`. The reduced
workspace keeps these files:

```text
Cargo.toml
app/Cargo.toml
app/src/main.rs
required-lib/Cargo.toml
required-lib/src/lib.rs
```

The `app` package and its `required-lib` dependency remain, while the unused
dependency and unrelated workspace members are removed. The sibling
`result.repomin/report.json` records the reduction evidence. This example
requires a local Cargo toolchain; the fixture itself needs no network access.

## Shrink a Go module without network access

From a clean repository checkout, run the existing Go module fixture with the
module proxy disabled:

```sh
out_parent="$(mktemp -d /tmp/repomin-go-module.XXXXXX)"
PYTHONPATH=src python3 -m repomin benchmarks/go-module \
  --command 'GOPROXY=off go run .' \
  --match 'ORIGINAL_FAILURE' \
  --adapter go \
  --source-reducer none \
  --output "$out_parent/result"
```

The fixture is self-contained and does not use the network, but it requires a
local Go toolchain. Its oracle exits non-zero and prints output containing:

```text
panic: ORIGINAL_FAILURE
```

The minimized payload contains four files:

```text
go.mod
main.go
required/go.mod
required/required.go
```

The required local module and its `replace` directive remain in `go.mod`.
The unused module, its replacement, and unrelated `exclude` and `retract`
directives are removed. See the [fixture notes](../benchmarks/go-module/README.md)
for the oracle contract and the [reducer architecture](ARCHITECTURE.md#reducers)
for the Go adapter's supported directives and preservation boundaries.

## Shrink a Gradle multimodule build without network access

The repository includes a Kotlin-DSL Gradle fixture (`benchmarks/gradle-multimodule/`)
with an unrelated subproject, a dependency declaration, removable plugins,
properties, a resource, and documentation. Its task fails without resolving
external dependencies, so it runs with the Docker backend's default network
isolation.

**Required tools:** JDK 17 and Docker (the fixture uses the `gradle:8.10.2-jdk17`
image, which must already be present locally — ReproMin never pulls an image
automatically). No local Gradle installation is needed because the command runs
inside the container.

```sh
docker pull gradle:8.10.2-jdk17

PYTHONPATH=src python3 -m repomin benchmarks/gradle-multimodule   --command 'gradle --no-daemon -q :app:reproduceFailure --stacktrace'   --match 'NoSuchMethodError'   --backend docker   --docker-image gradle:8.10.2-jdk17   --jobs 4   --output /tmp/repomin-gradle-result   --verbose
```

The reduced build must still fail with `NoSuchMethodError`. The unrelated
module, the unused dependency declaration, removable plugins and properties,
the resource, and the documentation are all removed. Validate the report
without rerunning the reducer:

```sh
PYTHONPATH=src python3 -m repomin report validate   /tmp/repomin-gradle-result.repomin/report.json   --payload /tmp/repomin-gradle-result --json
```

Trust boundary: the command runs in Docker with networking disabled by default,
but Docker reduces exposure rather than being a complete security boundary —
see [SECURITY.md](../SECURITY.md) before running an untrusted command.

## Shrink a data file's contents with `--text-file`

Add a data file whose oracle only needs one line:

```sh
cat > read_data.py <<'PY'
from pathlib import Path
import sys

if "NEEDLE" not in Path("data.txt").read_text():
    print("DIFFERENT_FAILURE", file=sys.stderr)
    raise SystemExit(2)
print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(1)
PY
printf 'alpha\nbeta\nNEEDLE\ngamma\ndelta\n' > data.txt
```

Run with the text reducer:

```sh
repomin . \
  --command 'python3 read_data.py' \
  --match 'ORIGINAL_FAILURE' \
  --source-reducer none \
  --adapter none \
  --text-file data.txt \
  --output ../data-minimal
```

`data.txt` reduces to exactly `NEEDLE`, while the command still fails the same
way.

## Keep an unrelated file that the oracle does not need

Use `--keep` to preserve a file such as a license even though deleting it would
not change the failure:

```sh
repomin . \
  --command 'python3 reproduce.py' \
  --match 'ORIGINAL_FAILURE' \
  --keep LICENSE \
  --output ../example-kept
```

## Run the reproduction in Docker

Use the Docker backend when the reproduction needs a controlled filesystem or
you do not want the command to run directly on the host. Images must already
exist locally; ReproMin never pulls an image automatically. From the
repository root, run the included fixture:

```sh
docker pull python:3.11-slim

PYTHONPATH=src python3 -m repomin benchmarks/docker-python \
  --command 'python3 reproduce.py' \
  --match 'ORIGINAL_FAILURE: docker backend fixture(?:\r?\n|$)' \
  --backend docker \
  --docker-image python:3.11-slim \
  --jobs 2 \
  --output /tmp/repomin-docker-example
```

The output should contain only `reproduce.py`; the sibling
`/tmp/repomin-docker-example.repomin/report.json` records the image reference,
resolved image ID, and default `none` network policy. Docker reduces exposure
but is not a complete security boundary. Read [SECURITY.md](../SECURITY.md)
before running an untrusted command.

## Reduce a FastAPI dependency regression

The FastAPI fixture demonstrates a more realistic Docker workflow in which the
same runtime dependency is declared in several Python metadata files:

```sh
docker build -t repomin-fastapi-fixture benchmarks/python-fastapi

PYTHONPATH=src python3 -m repomin benchmarks/python-fastapi \
  --command 'python -m pytest -q' \
  --match 'FastAPI route regression: dependency override leaked' \
  --backend docker \
  --docker-image repomin-fastapi-fixture \
  --adapter python \
  --source-reducer none \
  --output /tmp/repomin-fastapi-example
```

The reduced project keeps the route regression and the dependency declarations
that cause it, while dropping unrelated test and manifest entries. The report
is in `/tmp/repomin-fastapi-example.repomin/report.json`; the Docker image must
already be available locally and Docker networking is disabled by default.
See [the fixture notes](../benchmarks/python-fastapi/README.md) for its oracle
contract and expected payload.

## Exercise the semantic reducer with a local stub

The semantic reducer is opt-in and provider-agnostic. Before connecting a real
model, run the deterministic local stub; it starts an ephemeral
OpenAI-compatible endpoint and still sends the proposed edit through the
ordinary failure oracle:

```sh
python3 benchmarks/semantic-stub/run.py \
  --output /tmp/repomin-semantic-example
```

The reduced `data.txt` should contain exactly `NEEDLE`, and the report should
show `semantic_reducer: "http"`, at least one semantic call, and one accepted
semantic mutation. This fixture does not contact a model or external network.
For a real local or self-hosted endpoint, see
[LLM_REDUCTION.md](LLM_REDUCTION.md) and keep the feature disabled unless the
endpoint and token handling are understood.
