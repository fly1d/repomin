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

Create and activate an isolated environment, then install the current
development release from its reviewed GitHub Release artifact:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip

REPOMIN_VERSION=0.1.0.dev13
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v${REPOMIN_VERSION}/repomin-${REPOMIN_VERSION}-py3-none-any.whl"

repomin --version
```

The final command should print `repomin 0.1.0.dev13`. The
[release page](https://github.com/fly1d/repomin/releases/tag/v0.1.0.dev13)
publishes SHA-256 digests for users who need to verify the wheel.

## 2. Confirm the command and strict failure contract

Run the exact reproduction command from the repository root before involving
ReproMin. Replace this example with your real command:

```sh
cd /absolute/path/to/your/repository
python -m pytest -q tests/test_checkout.py
```

Run it at least twice. Record a message specific to the target failure and its
exact exit code. This guide uses the following example contract:

```text
command:   python -m pytest -q tests/test_checkout.py
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

## 3. Check readiness with Doctor

Set the real source path. The suggested output is a new sibling directory, so
it is outside the source repository as required:

```sh
source_dir="/absolute/path/to/your/repository"
output_dir="/absolute/path/to/your/repository-repro"
failure_command='python -m pytest -q tests/test_checkout.py'
failure_match='AssertionError: checkout total mismatch'
failure_exit_code=1

repomin doctor "$source_dir" \
  --command "$failure_command" \
  --match "$failure_match" \
  --exit-code "$failure_exit_code" \
  --output "$output_dir"
```

Doctor checks the source, selected reducers, output path, and backend without
exporting to the configured output path. ReproMin prepares each baseline in a
fresh copy, but the failure command itself can still modify anything its
backend permits. On the host backend, that includes resources available to
your user account. Continue only when Doctor exits with status `0` and reports
a passing baseline. If it reports a failed check, fix that check and rerun it;
the
[Doctor guide](DOCTOR.md) explains each result.

The output path and its `<output>.repomin` sidecar must not already exist. The
reducer refuses to export either one inside the source repository.

## 4. Run a bounded reduction

Run the same contract with an attempt and wall-clock limit for the first trial:

```sh
repomin reduce "$source_dir" \
  --command "$failure_command" \
  --match "$failure_match" \
  --exit-code "$failure_exit_code" \
  --max-attempts 25 \
  --max-duration 300 \
  --output "$output_dir"
```

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

## 5. Validate and replay the result

The payload and evidence sidecar are separate:

```text
<output>/                         reduced repository
<output>.repomin/report.json     machine-readable evidence
<output>.repomin/REPOMIN.md      human-readable receipt
```

Validate the report structure and recorded payload fingerprint without
executing the command:

```sh
repomin report validate \
  "${output_dir}.repomin/report.json" \
  --payload "$output_dir" \
  --format markdown
```

Inspect the payload, `REPOMIN.md`, and the full `report.json`. Before replaying,
review the recorded command, then explicitly allow two fresh-copy runs:

```sh
repomin report replay \
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

## 6. Put the reduced result to work

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
