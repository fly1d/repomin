# Report Schema

`report.json` is the machine-readable evidence sidecar for one reduction. It is
written next to the exported payload at `OUTPUT.repomin/report.json`; keeping
it outside the payload means report writes cannot change a tree that already
passed the oracle.

The current top-level schema version is `1`. Consumers should reject an
unsupported `schema_version`, tolerate additional fields within a supported
version, and never infer code correctness from a passing oracle.

## Top-level fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer report format version. Current value: `1`. |
| `repomin_version` | ReproMin version that generated the report. Optional in legacy reports. |
| `command` | Exact reproduction command passed to the runner. |
| `failure_match` | Configured output regular expression, or `null` for process/exit-code modes. |
| `failure_spec` | Exact match, exit-code, and signature-mode flags used for replay. Optional in legacy reports. |
| `baseline_exit_code` | Return code observed during baseline validation. |
| `final_exit_code` | Return code observed during final validation of the accepted tree. |
| `source` | File/byte counts for the copied source tree before reduction. |
| `output` | File/byte counts for the exported payload, excluding the sidecar. |
| `attempts` | Logical candidate attempts, including no-ops and cache uses. |
| `accepted_mutations` | Number of promoted candidate mutations. |
| `cache_hits` | Session-local content-cache uses. These are not oracle executions. |
| `execution` | Runner, sampling, ignore-rule, and resource configuration. |
| `phase_statistics` | Per-phase accounting and oracle sample usage. |
| `holdout_certification` | Optional fresh-sample certification of the exported artifact. |
| `events` | Ordered human-readable reduction events and their oracle evidence. |
| `java_exception_signature` | Present only with `--java-exception`. |
| `python_exception_signature` | Present only with `--python-exception`. |
| `process_failure_signature` | Present only with `--process-failure`. |

`source` and `output` contain `files` and `bytes`. Output counts deliberately
exclude `report.json` and `REPOMIN.md`. New reports also store
`output.tree_sha256` and `output.tree_fingerprint_policy` for every exported
payload, independently of optional holdout certification. They also store
`output.tree_content_sha256` with the `tree-content-sha256-v1` policy. The
complete fingerprint includes filesystem metadata and is authoritative for a
local export; the content fingerprint covers paths, entry kinds, file contents,
and symlink targets so archive transports that rewrite mtimes can still be
checked. A consumer should label that case as content-only verification.

## Failure contract

`failure_spec` is an additive schema-v1 object that preserves the exact oracle
configuration needed by replay: `match`, optional `exit_code`, and the boolean
`java_exception`, `python_exception`, and `process_failure` modes. At most one
signature mode can be true, process-failure mode cannot also configure an exit
code, and the stored match must equal the legacy top-level `failure_match`.

Java, Python, and process signature objects remain top-level fields for
backward compatibility. When `failure_spec` selects a signature mode, exactly
the corresponding recorded signature must be present. Replay pins this
identity; it never learns a replacement signature from current output.

## Execution

The `execution` object records the boundary in which commands were sampled.
Important fields include:

- `backend`: `host` or `docker`.
- `jobs`: maximum candidate concurrency.
- `cache_enabled`, `cache_hits`, and `resumed`.
- `baseline_runs`, `candidate_runs`, `final_runs`, and their pass counts.
- `confidence`, `min_baseline_rate`, `min_candidate_rate`, and the sampling
  policy identifiers.
- `reduction_strategy`: the reducer strategy identity used for this report and
  persistent-session compatibility.
- `ignored_names`, `ignored_paths`, `gitignore_files`, `keep_paths`, and
  `text_files`: input-selection controls applied before reduction.
- `environment_names` and `environment_sha256`: names and a digest of explicit
  environment values. Values are intentionally never recorded.
- `timeout_seconds`: configured timeout for each reproduction command.
- `budget_exhausted`: boolean indicating whether an optional reduction budget
  stopped the search before the normal fixed-point condition.

Docker reports additionally contain the image reference, resolved immutable
image ID, network policy, and configured resource limits when applicable.
These fields describe the execution boundary; they do not make Docker a
complete security sandbox.

## Phase accounting

`phase_statistics.phases` contains one object per reduction phase. Each phase
tracks attempts, no-ops, rejected/accepted/superseded/aborted candidates,
oracle sample uses, actual oracle samples, cache hits, and samples saved by
early stopping.

For complete reports, consumers can check both accounting identities:

```text
attempts = no_op + rejected + accepted + superseded + aborted
oracle_sample_uses = oracle_samples + cache_hits
```

`coverage` is `partial` when a legacy or interrupted session cannot provide a
complete phase history. Missing historical data must not be reconstructed from
the aggregate counters.

## Holdout certification

`holdout_certification.status` is `not_requested`, `certified`, `rejected`, or
an interrupted/aborted status. When certification is enabled, its samples are
fresh fixed-size runs against the frozen exported payload. They are separate
from baseline, candidate, and ordinary final-validation samples.

The report records the planned/completed sample counts, passes, exact lower
bound, exact p-value, resource/timeout veto counts, artifact fingerprint, and
the holdout policy identifier. Sample `index` values are one-based and
contiguous through `completed_runs`. When present, each sample's outcome must
agree with its acceptance, timeout, and resource-exhaustion flags; interrupted
samples carry no execution evidence. A certified lower bound is a statistical
claim about oracle pass probability under fresh iid samples in the recorded
environment. It is not a proof of correctness, compatibility, or production
reliability.

When the aggregate timeout, resource-exhaustion, or interruption counters are
present alongside complete sample fields, they must equal the corresponding
sample counts and cannot exceed `completed_runs`.

Terminal holdout statistics are an all-or-none group when present. Modern
reports (`holdout_certification.schema_version: 1`) with status `certified`
must include the complete terminal group and must have completed every planned
run. The `observed_rate` must equal `passes / planned_runs`, confidence must be
in `(0, 1)`, and the exact gate result must be boolean. When the optional
`ordinary_failures` aggregate is present, it must equal the number of samples
whose outcome is `failed`.

## Events and signatures

Each `events` entry records the phase, description, duration, oracle pass/runs,
rate and lower-bound evidence, and (when applicable) candidate family
confidence and early-acceptance state. Event order is significant for audit
and resume diagnostics. Optional rates and bounds are finite probabilities in
the inclusive `[0, 1]` range; `oracle_rate` must agree with
`oracle_passes / oracle_runs`. Candidate family index, confidence, and alpha
are an all-or-none group when present, and the early-acceptance flag is
boolean when present.

Signature objects preserve identity beyond a broad output match. Java and
Python signatures include exception class, message, and normalized frames.
Process signatures distinguish POSIX signals, Windows statuses, and ordinary
exit codes. Timeout and resource-exhaustion outcomes are never treated as a
matching failure signature.

## Consumer guidance

1. Verify `schema_version`, output file/byte counts, and the exported payload
   fingerprint before trusting an artifact. A content-only match means
   transport metadata may have changed.
2. Check `execution.backend`, Docker identity/policy, environment names, and
   the reproduction command before sharing the sidecar.
3. Treat `failure_match` and signatures as the configured oracle contract, not
   as an explanation of every possible failure mode.
4. Keep `report.json` and `REPOMIN.md` beside the payload; do not copy either
   file into the tree when independently rerunning the command.

The bundled validator checks these structural rules without executing the
reproduction command:

```sh
repomin report validate OUTPUT.repomin/report.json --payload OUTPUT
```

It returns exit code `2` for malformed JSON, unsupported schema versions,
inconsistent phase/holdout accounting, unsafe payload entries, size drift, or
a payload fingerprint mismatch. Legacy reports without an output or holdout
fingerprint still receive safe-tree and file/byte-count validation.

Add `--json` when a CI step or issue report needs a compact summary. The result
has `summary_schema_version: 1` and includes `valid`, `schema_version`,
`holdout_status`, the report path, `repomin_version`, `backend`, the
privacy-safe `oracle_mode`, source/output file and byte counts, removed
file/byte counts, file/byte retention ratios, `attempts`,
`accepted_mutations`, `cache_hits`, and `budget_exhausted`. Holdout run/pass
counts are included without including command output,
environment names, or environment values. The version is `null` for
legacy reports that predate version provenance. Ratios are `null` when the
source denominator is zero; otherwise they are descriptive fractions rounded
to six decimal places. A negative removal count is possible for a report whose
recorded output is larger than its source and should be read as a size change,
not a correctness signal. These fields are descriptive metadata copied or
derived from the validated report; they do not add a new correctness claim.
`payload_fingerprint_verified` distinguishes a cryptographic tree match from
count-only validation of a legacy report, and `payload_fingerprint_mode` is
`exact`, `content`, or `unavailable` when `--payload` is supplied.

For a human-readable, shareable version of the same safe fields, request the
Markdown exporter:

```sh
repomin report validate OUTPUT.repomin/report.json \
  --payload OUTPUT --format markdown
```

The exporter is deterministic and uses a fixed whitelist: summary/report
schema and version, execution backend, oracle type, source/output sizes,
removal and retention figures, reduction attempt/mutation counts, holdout
status/counts, and payload-fingerprint status. Every value is escaped for a
Markdown table. It never renders the report path, payload path, reproduction
command, match expression, logs, environment names or values, signatures, or
other report fields. A missing payload is represented as `n/a` for fingerprint
fields; it does not trigger command execution. Invalid JSON, unsupported schema,
inconsistent evidence, or a mismatched payload returns exit code `2` and emits
no Markdown summary.

After reviewing the unsigned command in a report, consumers can also run a
fresh-copy [replay](REPLAY.md):

```sh
repomin report replay OUTPUT.repomin/report.json --payload OUTPUT --yes
```

Replay is a new current-environment observation. It does not upgrade the
original report or create a statistical certificate.

The architecture document explains the statistical contracts and reducer
invariants behind these fields. See [ARCHITECTURE.md](ARCHITECTURE.md) and
[SECURITY.md](../SECURITY.md) before processing untrusted repositories.
