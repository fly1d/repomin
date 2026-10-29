# ReproMin quick start: reduce a real failure

This guide applies ReproMin to an existing, trusted repository with a
repeatable failure. It takes you from the original command to a bounded
reduction, verified evidence, and a result another developer can actually use.
If you only want to check that ReproMin runs on your machine, use the
one-command demo in the [README](../README.md#30-second-demo).

ReproMin requires Python 3.9 or newer. The commands below use Bash or Zsh on
macOS or Linux. Windows users can consult the
[PowerShell guide](QUICKSTART.windows.md) for installation and command syntax.

## Before you start

The best first case has all of these properties:

- one local command consistently reaches the target failure;
- the repository is too large or noisy to share or keep as a regression case;
- the command needs no credentials, private service, production data, or
  special hardware; and
- you trust the repository and every command it runs.

ReproMin minimizes what your failure contract accepts. It does not diagnose the
root cause, and a weak contract can produce a small but unusable result.

## 1. Install the published release

For the Python example below, first select the project's existing environment
using its normal setup. Record the interpreter that already has its test
dependencies, before creating ReproMin's environment:

```sh
project_python="$(python -c 'import sys; print(sys.executable)')"
"$project_python" -m pytest --version
```

If that check cannot find pytest, select or prepare the project's environment
first. Installing ReproMin does not install the project's dependencies.

Create a separate environment outside the source repository and install this
exact pre-alpha development version from its reviewed GitHub Release artifact.
Use the tool's explicit path below, keeping the project environment and `PATH`
active.

Use a maintained Python 3.11+ for this tool environment (`python3` below); the
project can keep its own interpreter. The new environment needs pip 26.2.2 or
newer before `pip install`. Update an older bundled pip using the
[Python security notice](https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576).
If the active project selects an older Python, replace `python3` below with
the absolute path to the maintained tool interpreter.

```sh
repomin_env="$HOME/.venvs/repomin"
python3 -m venv "$repomin_env"
REPOMIN_VERSION=0.1.0.dev15
"$repomin_env/bin/python" -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v${REPOMIN_VERSION}/repomin-${REPOMIN_VERSION}-py3-none-any.whl"

repomin_cli="$repomin_env/bin/repomin"
"$repomin_cli" --version
```

The final command should print `repomin 0.1.0.dev15`. Use the matching
[release page](https://github.com/fly1d/repomin/releases/tag/v0.1.0.dev15)
for the wheel's SHA-256 digest.

Other guides write the command name as `repomin`. With this separate tool
environment, use `"$repomin_cli"` in its place and keep the project's environment
active in the same shell.

## 2. Confirm the command and strict failure contract

Run the exact reproduction command from the repository root before involving
ReproMin. Replace this example with your real command:

```sh
cd /absolute/path/to/your/repository
"$project_python" -m pytest -q tests/test_checkout.py
```

Record a message specific to the target failure and its exact exit code.
This guide uses the following example contract:

```text
command:   /absolute/path/to/project/.venv/bin/python -m pytest -q tests/test_checkout.py
match:     AssertionError: checkout total mismatch
exit code: 1
```

Avoid a generic expression such as `FAILED`, `error`, or only a test filename.
An installation error or a different assertion could then pass the oracle.
Check that ordinary setup failures and unrelated test failures do not produce
your chosen marker. When the command generates build output, make cleanup part
of the command so stale files cannot satisfy a candidate.

Use both `--match` and `--exit-code` when both are stable. If output text is
unstable, ReproMin can instead learn a Java exception, Python exception, or
exact process-failure signature; see the [Doctor guide](DOCTOR.md) before
choosing one of those modes.

Use
`--match-text 'FAIL [checkout]: total (12)'` to copy a distinctive failure
message without regex escaping. The equivalent regex is
`--match 'FAIL \[checkout\]: total \(12\)'`. Choose one option for the
reduction and keep the same choice if you later use Doctor. Both options are
available in `v0.1.0.dev14`; the remaining examples use `--match`.
See [literal messages and shell quoting](DOCTOR.md#literal-failure-messages).

<a id="4-run-a-bounded-reduction"></a>

## 3. Run a bounded reduction

Set the real source path and a new sibling output directory, then run the
reduction directly. It first verifies the failure in two fresh copies by
default, then reduces candidates only if that baseline passes.

```sh
source_dir="/absolute/path/to/your/repository"
output_dir="/absolute/path/to/your/repository-repro"
failure_command="$("$project_python" -c 'import shlex, sys; print(shlex.join([sys.executable, "-m", "pytest", "-q", "tests/test_checkout.py"]))')"
failure_match='AssertionError: checkout total mismatch'
failure_exit_code=1

"$repomin_cli" reduce "$source_dir" \
  --command "$failure_command" \
  --match "$failure_match" \
  --exit-code "$failure_exit_code" \
  --max-attempts 25 \
  --max-duration 300 \
  --semantic-reducer none \
  --output "$output_dir"
```

The command uses the saved project interpreter, quoted for Bash/Zsh even when
its path contains spaces. Built-in exclusions omit `.venv` and `venv`, so a
relative command such as `.venv/bin/python -m pytest` cannot find that environment
in fresh copies. Keep using the same `failure_command` for reduction and replay.
See [baseline troubleshooting](DOCTOR.md#when-the-original-command-works-but-the-baseline-fails)
if a command works in the original checkout but not in a fresh copy.
The absolute interpreter path is local to this machine; its environment is not
exported with the payload. Review the [replay requirements](REPLAY.md) before
sharing a result with another developer.

If the project has an editable install, an installed copy of the same package,
or an absolute `PYTHONPATH`, tests can still import code outside the candidate.
Add a [source-origin check](DOCTOR.md#when-a-passing-baseline-uses-the-original-python-source)
to the reproduction command and keep it active for every candidate and replay.
A passing baseline or fingerprint check does not establish which code was tested.

The output path and its `<output>.repomin` sidecar must not already exist. The
reducer refuses to export either one inside the source repository. Each failure
command runs with the access its backend permits; on the host backend that
includes resources available to your user account.

The command runs in temporary copies. It exports the smallest verified state
reached within the limits, even when a larger case needs another, deliberately
configured run to reach a fixed point. Start with the bounded result before
raising either limit.

Automatic reducer selection covers common Maven, Gradle, Python, Pipenv, Node,
Composer, MSBuild, Bundler, Cargo, and Go manifests plus Java and Python source.
Use `--keep RELATIVE_PATH` for an oracle script, license, lock file, or other
file that must survive whole-file reduction. Use `--text-file RELATIVE_PATH`
only when the contract rejects malformed or unusable content in that file. See
the [ecosystem examples](EXAMPLES.md) for reducer-specific choices.

<a id="3-check-readiness-with-doctor"></a>

[Doctor](DOCTOR.md) is available for a separate readiness diagnosis. Its static
scan without `--command` does not execute the failure command.

<a id="5-validate-and-replay-the-result"></a>

## 4. Validate and replay the result

The payload and evidence sidecar are separate:

```text
<output>/                         reduced repository
<output>.repomin/report.json     machine-readable evidence
<output>.repomin/REPOMIN.md      human-readable receipt
```

Validate the report structure and recorded payload fingerprint without
executing the command:

```sh
"$repomin_cli" report validate \
  "${output_dir}.repomin/report.json" \
  --payload "$output_dir" \
  --format markdown
```

Inspect the payload, `REPOMIN.md`, and the full `report.json`. Before replaying,
review the recorded command, then explicitly allow two fresh-copy runs:

```sh
"$repomin_cli" report replay \
  "${output_dir}.repomin/report.json" \
  --payload "$output_dir" \
  --runs 2 \
  --yes
```

A successful summary reports `payload_fingerprint_verified: true`. Also inspect
`payload_fingerprint_mode`: `exact` includes the recorded filesystem metadata.
`content` means paths, entry types, file contents, and symbolic-link targets
match, but metadata equality was not established; artifact transport is one
common reason. Replay shows whether the configured failure still occurs in the
current environment. Neither validation nor replay proves correctness or root
cause.

<a id="6-put-the-reduced-result-to-work"></a>

## 5. Put the reduced result to work

A reduction creates value only when it helps the next task. After human review:

- attach or link a sanitized copy to the relevant bug report;
- add the reproduction to a regression suite; or
- keep it as a small fixture for debugging and dependency upgrades.

Keep the validated payload and sidecar unchanged as the evidence copy. If you
add a README, restore presentation files, or otherwise edit a copy for an
issue, say that it differs from the fingerprinted payload and rerun its failure
command. Never publish credentials, private URLs, proprietary source, customer
data, raw logs, commands containing secrets, or environment values.

Share a useful, blocked, or inconclusive trial in
[Show and tell](https://github.com/fly1d/repomin/discussions/new?category=show-and-tell).
For a public, licensed repository, you can instead offer the command, revision,
and failure signature for a maintainer-assisted
[bounded pilot](https://github.com/fly1d/repomin/issues/11); no ReproMin
installation is required for that path.

## Safety boundary

The default host backend runs the supplied command with your user account. It
is not a sandbox. Only use it with repositories and commands you trust. Docker
can reduce access when configured carefully, but it is not a complete security
boundary. Read [SECURITY.md](../SECURITY.md) before handling third-party code
or sharing any result.

For versioned configuration, GitHub Actions, and report details, continue from
the [documentation index](README.md).
