# Benchmarks

Run the complete network-free set from the repository root with:

```sh
python3 benchmarks/run_offline.py
```

List fixture names without executing them with:

```sh
python3 benchmarks/run_offline.py --list
```

During fixture development, run a focused subset with repeatable exact names:

```sh
python3 benchmarks/run_offline.py --only python-pyproject --only text-lines
python3 benchmarks/run_offline.py --exclude native-process
```

Unknown names and a name supplied to both `--only` and `--exclude` are rejected.
Without filters, every benchmark runs as usual.

For CI or tooling that needs a stable machine-readable summary, provide an
output path:

```sh
python3 benchmarks/run_offline.py --json-output /tmp/repomin-benchmarks.json
```

The JSON uses schema version `1` and records the ReproMin and Python/platform
identity, aggregate pass/skip/fail counts, one status plus elapsed time per
fixture, and the exact `only`/`exclude` filters plus final `selected` names.
This makes a filtered artifact distinguishable from a full run when using
`compare.py`, and makes cross-version comparisons auditable.
Failures include only a bounded error summary; the full reproduction output
remains in the command logs.

The runner executes each offline fixture in a fresh temporary output directory,
validates the generated report and payload fingerprint, and independently
reruns the oracle. It
skips toolchain-dependent fixtures (`cargo`, `go`, `ruby`) when their command is
absent and exits non-zero on any failure. Docker, Maven, Gradle, PHP, and .NET
SDK fixtures are intentionally outside this runner.

The `python-fastapi` fixture is a Docker-only integration example. It exercises
the Python manifest reducer against a pinned FastAPI/pytest image and is
documented in [its README](python-fastapi/README.md); it is intentionally kept
outside the network-free runner.

To compare several saved summaries, use the standard-library comparison tool:

```sh
python3 benchmarks/compare.py \
  /tmp/repomin-benchmarks-before.json \
  /tmp/repomin-benchmarks-after.json \
  --json-output /tmp/repomin-benchmarks-comparison.json
```

For a strict repeated-run comparison, require every input to carry identical
selection metadata:

```sh
python3 benchmarks/compare.py \
  --require-same-selection \
  /tmp/repomin-benchmarks-run-1.json \
  /tmp/repomin-benchmarks-run-2.json
```

This rejects legacy summaries without selection metadata and summaries created
with different `--only` or `--exclude` filters. The default comparison remains
backward-compatible and still reports missing fixtures explicitly.

The text output aligns fixture names across runs and shows the ReproMin version,
status, and elapsed time. The JSON output includes per-run counts and
descriptive min/median/max durations. Missing fixtures are shown explicitly.
This is a regression and diagnostic aid, not a performance claim; use repeated
runs in the same environment before drawing speed conclusions.

`maven-multimodule` is a real Maven reactor containing one failing JUnit test,
one unrelated module, an unused dependency, an unused resource, and unrelated
documentation. Its failing Java test also contains an unused import, field,
method, and local statements for the native JDK AST reducer. A required helper
is called from another source file and contains removable annotations, a
fixed-arity unused parameter, a reducible conditional expression, and a JUnit
`TestInfo` parameter. That external type deliberately makes coordinated symbol
analysis unavailable without the explicit test classpath. With the staged
classpath, removing the unused parameter requires the reducer to remove its
linked call argument in the same candidate. This fixture covers a cross-file,
fixed-arity instance method whose dispatch is closed by its ordinary `final`
helper class; unit tests cover explicitly `final`, `static`, and `private`
methods, ordinary constructors, varargs, overloads, hierarchy and generic
bridge exclusions, stale grouped ranges, global `ERROR`-type fallback, and
symbol blockers. Anonymous-class construction and record constructors are
deliberately outside the current coordinated-constructor support.

`node-package` is a network-free npm-compatible `package.json` fixture. Its
Python oracle requires one dependency and one workspace while allowing the Node
manifest reducer to remove unrelated dependencies, scripts, workspace entries,
and overrides. Run its self-contained command from
`benchmarks/node-package/README.md`.

`composer-package` is a network-free Composer `composer.json` fixture. Its
offline Python oracle requires one package and the autoload map while allowing
unrelated requirements, scripts, repositories, and replacement metadata to
disappear. See `benchmarks/composer-package/README.md`.

`pipenv-package` is a network-free Pipenv `Pipfile` fixture. Its oracle keeps
one package while allowing unrelated runtime, development, and interpreter
option entries to disappear; source settings remain unchanged. See
`benchmarks/pipenv-package/README.md`.

`python-pyproject` is a network-free Python `pyproject.toml` fixture. Its
oracle keeps the project identity and one required PEP 621 dependency while
allowing optional, build-system, Poetry, PDM, uv, and dependency-group entries
to disappear. See `benchmarks/python-pyproject/README.md`.

`python-requirements` is a network-free requirements-chain fixture modelled on
CI dependency regressions. Its top-level `requirements.txt` includes a runtime
file, that file includes CI-only requirements, and a sibling constraints file
pins both packages. The oracle requires the complete include/constraint chain
and a multiline hash-pinned runtime dependency while allowing unused packages
and index options to disappear. See
`benchmarks/python-requirements/README.md` for the reduction command and
independent oracle check.

`dotnet-project` is an MSBuild `.csproj` fixture. Its offline Python oracle
requires selected package/project references and the target framework while
allowing unrelated item entries to disappear. See
`benchmarks/dotnet-project/README.md`.

`dotnet-directory-build-props` is an MSBuild `Directory.Build.props` fixture. Its
offline Python oracle requires selected shared package/project references and the
target framework while allowing unrelated shared item entries to disappear. See
`benchmarks/dotnet-directory-build-props/README.md`.

`ruby-gemfile` is a network-free Bundler `Gemfile` fixture. Its Ruby oracle
requires one gem declaration while allowing unrelated complete single-line
declarations to disappear. See `benchmarks/ruby-gemfile/README.md`.

`cargo-workspace` is a local-only Rust workspace benchmark. Its `cargo run`
oracle requires the `app` and `required-lib` members while allowing an unused
path dependency and unrelated workspace members to disappear. See
`benchmarks/cargo-workspace/README.md`.

`go-module` is a network-free Go module benchmark. Its `go run` oracle requires
one local module and its replacement while allowing an unused module and
unrelated directives to disappear. See `benchmarks/go-module/README.md`.

Run this benchmark with Maven and JDK 11 or newer. ReproMin compiles its Java
analysis helper with `javac --release 11`; this compatibility setting applies
to the helper, not to the benchmark project's source level. Stage the test
dependencies in a host directory outside the benchmark before reduction. The
dependency plugin is pinned, and its console output is not parsed as a
classpath:

```sh
REPOMIN_CP_DIR="$(mktemp -d)"
mvn -q -f benchmarks/maven-multimodule/app/pom.xml \
  org.apache.maven.plugins:maven-dependency-plugin:3.8.1:copy-dependencies \
  -DincludeScope=test \
  -DoutputDirectory="$REPOMIN_CP_DIR"

set --
for repomin_jar in "$REPOMIN_CP_DIR"/*.jar; do
  set -- "$@" --java-classpath "$repomin_jar"
done
```

Run the reducer from the project root:

```sh
PYTHONPATH=src python3 -m repomin benchmarks/maven-multimodule \
  --command 'mvn -q -pl app test' \
  --match 'NoSuchMethodError' \
  --java-exception \
  --jobs 4 \
  --output /tmp/repomin-maven-result \
  --verbose \
  "$@"
```

Each generated option is one atomic entry, and the glob's order is retained.
The entries are consumed by the host-side analyzer only; they do not alter the
Maven oracle command. Use a durable dependency directory instead of `mktemp`
when adding `--session`, because entry paths, order, and content fingerprints
must still match on resume.

The reduced project must still fail with `NoSuchMethodError`. The unrelated
module, dependency, resource, documentation, and Java elements should be
absent. The required helper should remain while its annotations, unused
parameter, linked cross-file call argument, and conditional branch are reduced.

### Maven acceptance envelope

The reference run for `hierarchical-fixed-point-v2` used macOS arm64, Python
3.14.7, Maven 3.9.16, OpenJDK 17.0.20, `--jobs 4`, two baseline runs, and one
candidate/final run. It produced 4 project files totaling 1525 bytes, with 130
logical attempts, 103 actual candidate oracle samples, 20 accepted mutations,
and 27 cache hits. The preceding one-target strategy produced the same project
files in 175 logical attempts and 160 actual candidate commands. This is a
25.7% attempt reduction and 35.6% command reduction on this fixture; it is not
a general wall-clock speed claim. A speed claim requires alternating at least
three runs per strategy in the same environment and comparing median wall time.

Use the following gate after a run. The logical-attempt guard remains below the
old 175-attempt baseline; the current reference is 130. Actual reducer-phase
oracle samples must not exceed 103, and the project bytes and hashes must remain
identical:

```sh
REPOMIN_RESULT=/tmp/repomin-maven-result
python3 - "$REPOMIN_RESULT" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
metadata = root.with_name(root.name + ".repomin")
report = json.loads((metadata / "report.json").read_text(encoding="utf-8"))
expected = {
    "app/pom.xml": "b643f82ed027991ea7eef45b2303bcae39a6b9152023a5131e29eebec97724eb",
    "app/src/test/java/dev/repomin/FailureMessage.java": "cc164f6eaed292266e073e83b708aa043718be63794553420954e4c931e9e91c",
    "app/src/test/java/dev/repomin/TriggerTest.java": "cdcb0e2ea632750efda7730d49fd87e31a38fb586f5695743b7fbe304d8c050e",
    "pom.xml": "bcce9cef2212b4db8172ff06fa5420a49f76efdeb5de5ebff43c7f2a1ea3d8a9",
}
assert report["output"] == {"files": 4, "bytes": 1525}
assert report["attempts"] <= 130
assert sum(p["oracle_samples"] for p in report["phase_statistics"]["phases"]) <= 103
assert report["phase_statistics"]["coverage"] == "complete"
assert report["execution"]["reduction_strategy"] == "hierarchical-fixed-point-v2"
assert report["holdout_certification"]["status"] == "not_requested"
assert report["java_exception_signature"] == {
    "class": "java.lang.NoSuchMethodError",
    "message": "demo.Target.missing()",
    "frames": [
        "dev.repomin.TriggerTest.preservesTheOriginalFailure",
        "java.lang.reflect.Method.invoke",
        "java.util.ArrayList.forEach",
    ],
}
for relative, digest in expected.items():
    assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest
print("benchmark report and project hashes accepted")
PY
```

Finally, run Maven independently of ReproMin and inspect the newly generated
Surefire evidence. This proves that the exported tree itself exits 1 with the
same exception identity rather than relying only on the saved report:

```sh
python3 - "$REPOMIN_RESULT" <<'PY'
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
run = subprocess.run(
    ["mvn", "-q", "-pl", "app", "test"],
    cwd=str(root),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
assert run.returncode == 1, run.returncode
reports = root / "app/target/surefire-reports"
evidence = run.stdout + run.stderr + "".join(
    path.read_text(encoding="utf-8", errors="replace")
    for path in reports.glob("*")
    if path.is_file()
)
for needle in (
    "java.lang.NoSuchMethodError",
    "demo.Target.missing()",
    "dev.repomin.TriggerTest.preservesTheOriginalFailure",
    "java.lang.reflect.Method.invoke",
    "java.util.ArrayList.forEach",
):
    assert needle in evidence, needle
print("independent Maven reproduction accepted")
PY
```

`native-process` is a POSIX-only process-signature fixture. With
`required.txt`, its Python process terminates directly with `SIGABRT`; without
that file it terminates with `SIGTERM`. Neither path needs diagnostic output,
and `unused.txt` is unrelated noise. Disabling core dumps keeps command side
effects small and deterministic.

Run from the project root. `exec` makes the shell expose the direct signal to
the host runner instead of a shell-encoded `128 + signal` exit code:

```sh
PYTHONPATH=src python3 -m repomin benchmarks/native-process \
  --command 'exec python3 crash.py' \
  --process-failure \
  --source-reducer none \
  --baseline-runs 3 \
  --candidate-runs 2 \
  --jobs 2 \
  --holdout-runs 5 \
  --min-holdout-rate 0.5 \
  --session /tmp/repomin-native-process-session \
  --output /tmp/repomin-native-process-result
```

The reduced payload must contain `crash.py` and `required.txt`, must not contain
`unused.txt`, and must still terminate with `SIGABRT` when invoked independently.
The report's `process_failure_signature` must be
`{"kind":"posix_signal","code":SIGABRT,"name":"SIGABRT"}` and the holdout
must certify all five samples. A positive shell/container exit code is a
different signature even when it happens to equal `128 + SIGABRT`.

`docker-python` exercises the container execution backend using a local Python
image. It requires the image to exist before ReproMin starts; the backend never
pulls images automatically.

```sh
docker pull python:3.11-slim

PYTHONPATH=src python3 -m repomin benchmarks/docker-python \
  --command 'python3 reproduce.py' \
  --match 'ORIGINAL_FAILURE: docker backend fixture(?:\r?\n|$)' \
  --backend docker \
  --docker-image python:3.11-slim \
  --jobs 2 \
  --output /tmp/repomin-docker-result
```

The resulting repository should contain only `reproduce.py`; the Python source
reducer can remove the guard together with `required.txt`. The line-bounded
pattern is intentional: a token-only pattern can match a traceback source line
after the intended print fails. The sibling `OUTPUT.repomin/report.json` should
record the Docker image reference, resolved immutable image ID, and the default
`none` network policy.

Any benchmark can be made resumable by adding `--session PATH`. For the Docker
backend, `PATH` must be in a host directory shared with the selected daemon;
Colima and Docker Desktop may not expose host `/tmp`, so use a shared path such
as one under `/Users` on macOS when required. After an interrupt, repeat the
command with the same options plus `--resume`; the checkpoint keeps the last
accepted repository tree and learned failure signature while the exported
output remains separate.

For a failure that is intermittent by design, add for example
`--baseline-runs 5 --min-baseline-passes 3 --candidate-runs 5
--min-candidate-passes 3`. A count-only candidate accepts when its third pass is
observed, while baseline and final validation still run all five samples. An
observed timeout or resource limit rejects before acceptance. Runs skipped after
a stopping decision never execute, so resource failures they might have
produced cannot be observed.

The same benchmark can use a statistical rate gate. For example,
`--baseline-runs 10 --min-baseline-rate 0.6 --candidate-runs 10
--min-candidate-rate 0.6 --confidence 0.95 --run-confidence 0.95` applies the
fixed-size exact gate to
the complete baseline and final samples and to a candidate that reaches its
tenth sample. If the minimum pass counts are not specified alongside the rate
options, ReproMin uses a count minimum of one; specifying both makes both
constraints mandatory.

With Java, Python, or process signature matching, the first extractable
basic-pass discovers the baseline signature and does not enter the rate
evidence. Only later baseline slots test that fixed signature, so configure at
least one additional baseline run beyond the evidence count you need.

A rate-gated candidate may accept before its tenth sample only after meeting the
count minimum, clearing the Jeffreys beta-binomial mixture anytime lower bound,
and retaining a passing planned-size exact gate when every remaining sample is
treated as a failure. With run-wide confidence, family `j` uses at most
`(1 - run-confidence)/(j(j+1))` alpha, capped by `1-confidence`; without that
option coverage remains per candidate. Candidates that cannot reach the
terminal thresholds reject early. Baseline and final validation always run all
configured samples, and a candidate that reaches the limit uses the fixed-size
exact rule. Wilson lower bounds remain available as descriptive report metrics.

The JSON report includes the configured rates, confidence level, observed rates,
and Wilson lower bounds. `candidate_early_acceptances`,
`candidate_early_rejections`, and `candidate_samples_saved` expose both stopping
directions and their combined savings. An accepted event records
`oracle_anytime_lower_bound` and `oracle_early_acceptance` so a prefix decision
is distinguishable from a full-size decision. Run-wide reports additionally
record the family policy/count/alpha upper bound and each accepted event's family
index, confidence, and actual binary alpha.

To certify the frozen benchmark artifact with evidence that was not used during
reduction, add `--holdout-runs 29 --min-holdout-rate 0.9
--holdout-confidence 0.95`. This runs 29 additional commands only after the
ordinary final validation passes. Every command receives a fresh copy of the
same cleaned payload; no candidate cache or early stopping is used. The
top-level `holdout_certification` report should then have `status=certified`,
`passes=29`, `required_passes=29`, and an exact lower bound at least `0.9`.

Do not add those 29 commands to reducer-phase `oracle_samples` or compare the
resulting total command count with the default acceptance envelope above. That
envelope intentionally measures reduction work with holdout disabled. The
certificate is conditional on iid oracle outcomes in the recorded environment;
filesystem copies do not isolate shared build caches or external services, and
repeating whole benchmark sessions until one certifies invalidates the
single-attempt interpretation.

`gradle-multimodule` is a Kotlin DSL build with an unrelated subproject,
plugins, a dependency declaration, properties, a resource, and documentation.
Its task fails without resolving external dependencies, so it can run with the
Docker backend's default network isolation.

```sh
docker pull gradle:8.10.2-jdk17

PYTHONPATH=src python3 -m repomin benchmarks/gradle-multimodule \
  --command 'gradle --offline --no-daemon -q -Prequired.flag=true :app:reproduceFailure --stacktrace' \
  --match 'NoSuchMethodError: demo\.Target\.missing\(\)' \
  --backend docker \
  --docker-image gradle:8.10.2-jdk17 \
  --docker-network none \
  --jobs 1 \
  --output /tmp/repomin-gradle-result \
  --verbose
```

The reduced build must still fail with `NoSuchMethodError`. The unrelated
module, dependency declaration, removable plugins and properties, resource,
and documentation should be absent.

This Gradle benchmark does not need an attribution classpath. If a Docker
benchmark does use `--java-classpath`, its entries must exist on the host;
ReproMin does not translate container-only paths or add Docker mounts for them.

`python-fastapi` is a real FastAPI application with a failing pytest regression,
PEP 621 dependencies, optional dependencies, a nested requirements include,
constraints, options, and unrelated source and test behavior. Build its local
image once; reduction itself runs with Docker networking disabled.

```sh
docker build -t repomin-python-fastapi:local benchmarks/python-fastapi

PYTHONPATH=src python3 -m repomin benchmarks/python-fastapi \
  --command 'python -m pytest' \
  --match 'FastAPI route regression: dependency override leaked' \
  --python-exception \
  --backend docker \
  --docker-image repomin-python-fastapi:local \
  --docker-cpus 2 \
  --docker-memory 512MiB \
  --docker-workspace-limit 64MiB \
  --docker-tmpfs-size 256MiB \
  --jobs 4 \
  --output /tmp/repomin-python-result \
  --verbose
```

The reduced project must retain the checkout source, the FastAPI dependency in
`pyproject.toml`, and the requirements include chain. Unused dependencies,
constraints, index options, the unrelated test, and documentation should be
absent.

`input-controls` is a network-free fixture for the input-control knobs rather
than any single language adapter. Its Python oracle requires `required.txt`
and exits with the exact code `7` when `exit-sentinel.txt` is also present,
so `--exit-code 7` must keep both sentinels without a `--match` expression.
The repository adds removable noise files, a root `.gitignore`, a nested
`nested/.gitignore`, and a root-ignored `ignored-dir/` whose nested `.gitignore`
must not be read. `--keep` protects `keep-me.txt` and the whole `kept-dir/`.

Run from the project root:

```sh
PYTHONPATH=src python3 -m repomin benchmarks/input-controls \
  --command 'python3 reproduce.py' \
  --exit-code 7 \
  --gitignore-recursive \
  --keep keep-me.txt \
  --keep kept-dir \
  --source-reducer none \
  --adapter none \
  --output /tmp/repomin-input-controls-result
```

The reduced tree must contain exactly `reproduce.py`, `required.txt`,
`exit-sentinel.txt`, `keep-me.txt`, and `kept-dir/keep-nested.txt`. The root
and nested `.gitignore` files are applied during the initial snapshot and are
then removable themselves, so they are not part of the expected payload.

```sh
REPOMIN_RESULT=/tmp/repomin-input-controls-result
python3 - "$REPOMIN_RESULT" <<'PY'
import hashlib
import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
metadata = root.with_name(root.name + ".repomin")
report = json.loads((metadata / "report.json").read_text(encoding="utf-8"))
expected = {
    "reproduce.py": "c6c4a7602ff59faae336da3603a6e9e7eae6f408b0c3dcea1401695ace71428f",
    "required.txt": "a5b0a56fd8d2fcf82092fb779244729989674b90787530cbf7946f4d00da53a6",
    "exit-sentinel.txt": "9577a8bfed904bd55390ba203f1a233be1981b36fa945537eb3be5b2446de031",
    "keep-me.txt": "60d8b5ad876211494154448e80a187a80db2bfc9a5b608fce966dfb286d9ec77",
    "kept-dir/keep-nested.txt": "0b1b192dc855ea4524ebb9dc20553b1874e71a9b173fc0cd7c26f0ff0ff110e0",
}
actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
assert actual == set(expected), sorted(actual)
for relative, digest in expected.items():
    assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest
assert report["output"] == {"files": 5, "bytes": 385}
assert report["baseline_exit_code"] == 7
assert report["final_exit_code"] == 7
assert report["failure_match"] is None
assert report["execution"]["gitignore_recursive"] is True
assert report["execution"]["gitignore_files"] == [
    ".gitignore",
    "nested/.gitignore",
]
assert report["execution"]["keep_paths"] == ["keep-me.txt", "kept-dir"]
assert report["execution"]["budget_exhausted"] is False
run = subprocess.run(
    ["python3", "reproduce.py"],
    cwd=str(root),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
assert run.returncode == 7, (run.returncode, run.stderr)
assert "INPUT_CONTROLS_FAILURE" in run.stderr
print("input-controls benchmark report, files, and reproduction accepted")
PY
```

To exercise the attempt budget, cap the same run with `--max-attempts 2`. The
exported tree must still reproduce exit code `7` while the report records the
exhausted budget; removable noise may remain because the reducer stops early.

```sh
PYTHONPATH=src python3 -m repomin benchmarks/input-controls \
  --command 'python3 reproduce.py' \
  --exit-code 7 \
  --gitignore-recursive \
  --keep keep-me.txt \
  --keep kept-dir \
  --source-reducer none \
  --adapter none \
  --max-attempts 2 \
  --output /tmp/repomin-input-controls-budget-result
```

```sh
REPOMIN_RESULT=/tmp/repomin-input-controls-budget-result
python3 - "$REPOMIN_RESULT" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
metadata = root.with_name(root.name + ".repomin")
report = json.loads((metadata / "report.json").read_text(encoding="utf-8"))
assert report["execution"]["budget_exhausted"] is True
assert report["execution"]["max_attempts"] == 2
assert report["attempts"] == 2
for name in (
    "reproduce.py",
    "required.txt",
    "exit-sentinel.txt",
    "keep-me.txt",
    "kept-dir/keep-nested.txt",
):
    assert (root / name).is_file(), name
run = subprocess.run(
    ["python3", "reproduce.py"],
    cwd=str(root),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
assert run.returncode == 7, run.stderr
print("input-controls budget run accepted")
PY
```

`semantic-stub` is a network-free benchmark for the opt-in `--semantic-reducer
http` seam. It does not call a real model; `run.py` starts a local
OpenAI-compatible chat-completions stub on an ephemeral port and drives the
full CLI end to end. The stub proposes replacing `data.txt` with the minimal
`NEEDLE` content, which the oracle still accepts, proving that a provider can
propose a content edit that is promoted only after ordinary oracle validation.

```sh
python3 benchmarks/semantic-stub/run.py
```

The runner asserts the report records the `http` backend, the benchmark model,
the stub endpoint, at least one semantic call, and exactly one accepted
semantic mutation, and that the reduced tree contains `reproduce.py` and the
minimized `data.txt`.

`text-lines` is a network-free fixture for the opt-in `--text-file` line
reducer. Its Python oracle requires `NEEDLE` in `data.txt` while the other
lines are removable noise:

```sh
PYTHONPATH=src python3 -m repomin benchmarks/text-lines \
  --command 'python3 reproduce.py' \
  --match 'ORIGINAL_FAILURE' \
  --source-reducer none \
  --adapter none \
  --text-file data.txt \
  --output /tmp/repomin-text-lines-result
```

The reduced `data.txt` must be exactly `NEEDLE`, and the independent command
must still exit `1` with `ORIGINAL_FAILURE`.
