# GitHub Action

ReproMin can turn a failing CI job into a downloadable minimized repository.
The action runs the configured reproduction command, then uploads both the
payload and its sibling `.repomin` report directory as one artifact.

## Example

Add this step after the command that identifies the failure, or use it as the
job's failure-handling step:

```yaml
- name: Minimize failure
  if: ${{ failure() }}
  uses: fly1d/repomin@v0.1.0.dev8
  with:
    command: python -m pytest -q
    match: "FAILED tests/test_regression.py"
    adapter: python
    source-reducer: python
    artifact-name: minimized-reproduction
```

The checkout must happen before this step. The action installs ReproMin from
the selected ref and uses `GITHUB_WORKSPACE` as the repository boundary. Keep
the action ref pinned to a reviewed release or full commit SHA for production
CI; the version above is the current pre-release.

When `output` is omitted, the action writes the payload and its `.repomin`
metadata directory under the runner's temporary directory. This is the safe
default for `source: .`, because the reducer never writes an output inside the
source repository. To choose a repository-relative output path, use a source
subdirectory and a sibling output path, for example `source: app` and
`output: .repomin-result`. An explicitly configured output inside `source` is
rejected before export.

## Inputs

`command` is required. Set at least one of `match`, `exit-code`, or
`process-failure` to define the failure oracle. `match` is useful when the
failure text is stable; `exit-code` is safer when test output changes between
runs; `process-failure: true` learns and preserves the exact signal or process
termination signature. `exit-code` and `process-failure` are mutually
exclusive, while `match` may be combined with either to narrow the oracle.

`source`, `output`, `adapter`, `source-reducer`, `backend`, `docker-image`,
`docker-network`, `timeout`, and `jobs` map directly to the corresponding CLI
options. `python-version` selects the interpreter used to install and run
ReproMin. `actions/setup-python` also prepends that interpreter to `PATH` for
the action steps, so a reproduction command that resolves `python` or
`python3` can run under this version. It does not reconstruct the PATH or
runtime from an earlier failed step; set `python-version` to the version used
by the failing job (or invoke an explicit interpreter) when that distinction
matters. `artifact-name` controls the uploaded artifact name.

Set `step-summary: true` when the job should append a compact, privacy-safe
validation table to GitHub's step summary. The option is `false` by default, so
existing workflows keep their current output contract. The summary contains
only schema/version, backend, oracle type, source/payload sizes, reduction
counts, holdout status/counts, and payload-fingerprint status. It deliberately
omits the reproduction command, match expression, logs, file paths, and
environment names or values. A workflow-run link is added when GitHub provides
validated run context. On local action runners or older hosts where
`GITHUB_STEP_SUMMARY` is unset, the request is reported as a warning and the
action continues without a summary.

`ignore` and `ignore-path` accept one exact entry per line. Use them for
secrets or private fixtures that must never enter the uploaded payload;
`gitignore: true` applies the repository's root `.gitignore`, and
`gitignore-recursive: true` also applies nested rule files. These exclusions are
passed as structured arguments, not evaluated as shell code. A missing rule
file or invalid path causes the action to fail before it publishes an artifact.

```yaml
    ignore: |
      .env
      credentials
    ignore-path: |
      test/private-fixtures
    gitignore: true
```

For a fresh final certification, set all three holdout inputs. The action keeps
holdout samples separate from ordinary candidate evidence and reports the
result through `holdout-status`:

```yaml
- name: Certify minimized failure
  if: ${{ failure() }}
  uses: fly1d/repomin@v0.1.0.dev8
  with:
    command: python -m pytest -q
    match: "FAILED tests/test_regression.py"
    holdout-runs: "5"
    min-holdout-rate: "0.8"
    holdout-confidence: "0.95"
```

For a command with a stable exit code but unstable output:

```yaml
- name: Minimize failure
  if: ${{ failure() }}
  uses: fly1d/repomin@v0.1.0.dev8
  with:
    command: python -m pytest -q
    exit-code: "1"
    adapter: python
```
The default backend is `host`; use Docker when the command should run inside an
existing local image:

```yaml
- name: Minimize Docker failure
  if: ${{ failure() }}
  uses: fly1d/repomin@v0.1.0.dev8
  with:
    command: python3 reproduce.py
    match: "ORIGINAL_FAILURE"
    backend: docker
    docker-image: my-reproduction-image:ci
    docker-network: none
```

## Outputs

Give the action an `id` when a later step needs to inspect the generated files.
The action exposes absolute paths for the payload and report, the metadata
directory, the artifact name, and scalar report facts useful for downstream
gates: `report-schema-version`, `source-files`, `source-bytes`, `output-files`,
`output-bytes`, `attempts`, `accepted-mutations`, and `holdout-status`. It also
exposes the privacy-safe `oracle-mode`, `file-retention-ratio`,
`byte-retention-ratio`, `payload-fingerprint-mode`, and
`payload-fingerprint-verified` values from the validated report. Ratios are
fractions rounded to six decimal places; they are empty when the source size is
zero. These outputs omit the reproduction command, match expression, logs, and
environment names or values.

```yaml
- name: Minimize failure
  if: ${{ failure() }}
  id: minimize
  uses: fly1d/repomin@v0.1.0.dev8
  with:
    command: python -m pytest -q
    match: "FAILED tests/test_regression.py"
    step-summary: true

- name: Validate minimized report
  if: ${{ always() && steps.minimize.conclusion == 'success' }}
  run: |
    repomin report validate \
      "${{ steps.minimize.outputs.report-path }}" \
      --payload "${{ steps.minimize.outputs.payload-path }}" --json
```

`payload-path` points to the minimized tree, `report-path` points to its
`report.json`, `metadata-path` points to the sibling `.repomin` directory, and
`artifact-name` is the name passed to
`actions/upload-artifact`. The numeric outputs are copied from the generated
report and are strings, as required by the Actions output protocol.
When `step-summary: true` and `GITHUB_STEP_SUMMARY` is available,
`step-summary-path` contains that runner-managed summary file path; otherwise it
is empty. The path is provided for smoke tests and diagnostics, while the
summary content itself remains limited to the privacy-safe Markdown whitelist.
`holdout-status` is `not_requested` when no holdout inputs are supplied.
The action validates the report and payload fingerprint before publishing its
outputs. Report validation returns exit code `2` for an invalid report or
payload fingerprint, so it can also be used as a later CI gate without
rerunning the original failure command.

`source` must be repository-relative and cannot escape the workspace. An
explicit `output` must also be repository-relative and outside `source`; when
it is omitted, the action uses the runner temporary directory. The command is
intentionally passed to the configured ReproMin runner; do not use this action
with untrusted workflow input. The host backend is not a sandbox, and Docker is
not a complete security boundary. Read
[SECURITY.md](../SECURITY.md) before minimizing an untrusted project.

## Artifact contract

The artifact contains the reduced payload at `payload-path` and the sibling
metadata directory at `metadata-path`. The report records the command outcome,
reduction attempts, execution backend, and payload fingerprint. It is evidence
for the configured reproduction in the recorded environment, not a proof of
code correctness or production reliability. The action explicitly includes
hidden paths so a dot-prefixed payload or metadata directory is uploaded by
GitHub Actions.

For local reproduction and report validation, see [EXAMPLES.md](EXAMPLES.md)
and [REPORT_SCHEMA.md](REPORT_SCHEMA.md).

## Compare artifacts from multiple runs

After downloading two or more artifacts, compare their validated reports in
the order you want to inspect them:

```sh
repomin report compare \
  ./run-before/repomin-result.repomin/report.json \
  ./run-after/repomin-result.repomin/report.json \
  --label before --label after \
  --format markdown
```

The command is local and read-only: it validates report structure, does not
execute the recorded failure command, does not read payload contents, and does
not contact GitHub or another service. The result is useful for reviewing
changes in reduction evidence, but it is not a performance, correctness, or
causal analysis. Warnings identify changed backends, oracle or sampling
configuration, source sizes, and other execution context. Use a benchmark
history tool for duration or throughput trends.
