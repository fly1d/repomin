# Replay a recorded failure

`repomin report replay` executes the command and failure contract recorded in
`report.json` against a reduced payload. It is a current-environment
verification tool: it tells you whether the configured oracle still matches,
not whether the payload is correct or whether the original root cause has been
found.

The command stored in a report is executable input. Review the report and the
payload before acknowledging execution with `--yes`.

This command is included in the `v0.1.0.dev7` pre-release. For a pilot report,
record the installed version and (when installing from source) the Git SHA as
described in the [pilot guide](REAL_FAILURE_PILOT.md).

## Minimal replay

```sh
repomin report replay \
  /tmp/payment-repro.repomin/report.json \
  --payload /tmp/payment-repro \
  --yes
```

The payload directory is copied to a tool-owned temporary directory before the
command runs. The original payload is not used as the command's working tree.

## Multiple fresh runs

```sh
repomin report replay REPORT \
  --payload PAYLOAD \
  --runs 3 \
  --yes
```

Every requested run starts from a new payload copy. Replay succeeds only when
all requested runs match the recorded oracle. Replay never uses the reduction
cache, and it checks that the input payload did not change before or after the
runs.

## Validation before and after execution

Before executing the command, replay validates the report schema and its
accounting, checks the payload's reported file and byte counts, and verifies
the available payload tree fingerprint. After the runs, it verifies the
fingerprint again. A local export normally gets an `exact` match against the
complete tree fingerprint. If an archive transport rewrote filesystem
metadata such as `mtime`, replay can use the recorded content fingerprint and
reports `content` mode; that proves the paths, entry kinds, file contents, and
symlink targets match, while metadata equivalence is no longer claimed. A
mismatch or an invalid report, payload, or runner setup stops replay with an
invocation error rather than producing a misleading failure result.

Reports generated before payload fingerprints were recorded can still be
replayed, but the result identifies the fingerprint as unavailable.

## Environment policy

Explicit environment values are intentionally not stored in a report. The
report records the names and a SHA-256 digest of the values supplied for the
original run. During replay:

- Re-supply every recorded name with one or more `--env NAME=VALUE` options.
- The set of names must match exactly, and the supplied values must match the
  recorded digest.
- Values are never printed in normal output or in `--json` output.
- Ambient process environment variables are not pinned or reproduced exactly.

For example:

```sh
repomin report replay REPORT \
  --payload PAYLOAD \
  --env API_MODE=staging \
  --env FEATURE_FLAG=on \
  --yes
```

Do not put secrets in shell history or issue trackers. Use the same secret
handling practices you would use for the original command.

## Backend policy

By default, replay uses the backend recorded in the report. `--backend host`
or `--backend docker` deliberately changes that execution boundary; treat such
an invocation as environment drift from the original run.

For Docker replay, the selected image must already be available locally. The
runner never pulls an image, and the network policy defaults to `none` even if
the original report used another policy. `--docker-image` and
`--docker-network` can explicitly select a local image and one of `none`,
`bridge`, or `host`.

Without `--docker-image`, replay requires the immutable image ID stored by a
modern Docker report. A legacy Docker report without that identity must supply
an explicit local image instead of silently resolving an old tag.

The host backend is not a sandbox. Host commands retain the filesystem,
credential, and network access available to the invoking user. Docker provides
defense in depth, not a complete security boundary. For untrusted reports or
payloads, use a disposable VM or container and account for possible access to
external services, ports, caches, clocks, and network state.

## Legacy reports

Older reports may not contain `failure_spec`, a recorded timeout, or a payload
fingerprint. Replay infers the contract it can from the legacy fields:

- A report with `failure_match` can be replayed using the inferred nonzero
  exit-and-match contract.
- A report with a recorded Java, Python, or process signature uses that
  signature as the identity check.
- A legacy report with neither a match nor a signature cannot distinguish an
  “any nonzero exit” contract from an exact exit-code contract. Supply the
  intended code explicitly with `--exit-code N`.

`--exit-code` is rejected for modern reports with an explicit `failure_spec`,
and cannot override a recorded process-failure signature.

## Security and scope

`--yes` is an explicit acknowledgement that the command embedded in the report
will execute. Treat both the report and payload as untrusted input. A replay
can run arbitrary code and may interact with local files, credentials,
services, ports, caches, clocks, and network state.

Replay is not holdout certification. It does not establish correctness,
production reliability, root cause, compatibility, or statistical confidence.
It only reports whether the configured oracle matched in the current
environment and execution boundary.

## Output and exit codes

Normal output summarizes the reproduction result, fresh-run counts, backend,
fingerprint status, and (for each failed run) the exit-code comparison. With
`--json`, the result is machine-readable and includes per-run scalar evidence
and output digests, but omits raw stdout, stderr, command output, and
environment values. A failed sample also includes `expected_exit_code` and
`actual_exit_code`; the expected value is `null` when the oracle does not pin an
exact exit code. The existing `returncode` field is retained for compatibility.
The JSON `fingerprint_mode` field is `exact`, `content`, or `unavailable`;
`metadata_drift_possible` is true only for content mode.

| Exit code | Meaning |
| --- | --- |
| `0` | Every requested replay run matched the recorded oracle. |
| `1` | Runs completed, but at least one run did not reproduce the oracle. |
| `2` | The report, payload, options, environment, or runner setup was invalid. |
| `130` | Replay was interrupted. |

An exit code of `0` is evidence of an oracle match only; it is not a general
correctness claim.

## Option reference

| Option | Description |
| --- | --- |
| `REPORT` | Path to the recorded `report.json`. |
| `--payload DIR` | Reduced payload directory to copy for each run. Required. |
| `--yes` | Acknowledge execution of the command stored in the report. Required. |
| `--runs N` | Number of fresh runs; every run must pass. Default: `1`. |
| `--timeout SEC` | Per-run timeout override. Defaults to the recorded value, or `120` seconds for legacy reports. |
| `--env NAME=VALUE` | Explicit environment value; repeatable. Names and values must satisfy the report policy. |
| `--backend recorded\|host\|docker` | Use the recorded backend or deliberately select a host/Docker boundary. Default: `recorded`. |
| `--docker-image IMAGE` | Use an already-local Docker image; no pull is performed. |
| `--docker-network none\|bridge\|host` | Docker network policy. Default: `none`. |
| `--exit-code N` | Exit-code contract for an ambiguous legacy report only. |
| `--json` | Emit machine-readable evidence without raw output or environment values. |
