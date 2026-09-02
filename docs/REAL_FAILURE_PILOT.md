# Real failure pilot

Use this guide to share a sanitized ReproMin run from a real CI or dependency
failure. The goal is to learn which defaults, adapters, and report fields help
in practice; a complete private repository is not required.

For completed public workflows, see the
[tsdown CSS module pilot](CASE_STUDY_TSDOWN_979.md), which records an oracle
weakness found during its first reduction, and the
[pydoctor Sphinx pilot](CASE_STUDY_PYDOCTOR_728.md), which verifies successful
intermediate artifacts before accepting a late build failure.

## Install the pilot build

The replay and transport-fingerprint workflow described below is included in
the `v0.1.0.dev9` pre-release. Install that wheel in an isolated environment
when you want a reproducible versioned pilot run:

```sh
python3 -m venv .venv
. .venv/bin/activate
REPOMIN_VERSION=0.1.0.dev9
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v${REPOMIN_VERSION}/repomin-${REPOMIN_VERSION}-py3-none-any.whl"
python -m repomin --version
```

Record the release version (and the Git SHA when installing from a source
checkout) in the pilot summary. Users who only capture and validate a report
can use the same release installation described in the [README](../README.md).

## Before sharing

- Remove credentials, tokens, private URLs, customer data, and proprietary
  source from the example.
- Replace package names, paths, and service names when they are confidential.
- Check that the reproduction command does not publish environment values.
- Prefer a public fixture or a small synthetic copy when the original project
  cannot be shared.
- Read the [security policy](../SECURITY.md). The host backend runs commands
  directly, and Docker is not a complete security boundary.

## Design a useful oracle

ReproMin preserves exactly the configured failure contract. A narrow marker
can retain the target symptom while allowing a malformed or unusable artifact,
so make recipient-facing quality part of the oracle when it matters:

- Delete stale generated output before building or testing each candidate.
- Use an exact exit code together with a stable marker when both are available,
  so an unrelated command failure cannot pass.
- Execute or load the generated artifact when a successful build alone does
  not prove it is usable.
- When a late build step is the target, verify that the expected intermediate
  artifacts exist so an earlier configuration or import failure cannot pass.
- Pin dependencies and warm an offline cache before a many-attempt reduction
  when the toolchain supports it.
- Protect the oracle, public license, and required lock files from whole-file
  removal with repeatable `--keep` options. Disable a structured, source, or
  text reducer when the protected file's content must remain unchanged;
  `--keep` alone does not block those content edits.
- Use `--text-file` only when the oracle rejects malformed syntax and other
  low-quality results for that file type.

For example, a public Node fixture can keep an explicit evidence boundary:

```sh
repomin /path/to/public-fixture \
  --command 'pnpm install --offline --frozen-lockfile && node reproduce.mjs' \
  --match 'STABLE_FAILURE_MARKER' \
  --exit-code 23 \
  --adapter none \
  --source-reducer none \
  --text-file src/index.ts \
  --keep reproduce.mjs \
  --keep package.json \
  --keep pnpm-lock.yaml \
  --keep LICENSE \
  --output /tmp/repomin-pilot-result
```

The lock file can dominate byte retention while still being essential for a
portable reproduction. Report file counts and the concrete behavior removed,
not only byte ratios.

## Capture a run

Run ReproMin against the sanitized checkout and use the failure signal that is
stable for the project:

```sh
repomin /path/to/sanitized-project \
  --command './run-failing-test.sh' \
  --match 'STABLE_FAILURE_MARKER' \
  --adapter auto \
  --output /tmp/repomin-pilot-result
```

When output text is unstable, use an exact exit code instead:

```sh
repomin /path/to/sanitized-project \
  --command './run-failing-test.sh' \
  --exit-code 1 \
  --adapter auto \
  --output /tmp/repomin-pilot-result
```

Validate the payload and its sidecar before inspecting or sharing them:

```sh
repomin report validate \
  /tmp/repomin-pilot-result.repomin/report.json \
  --payload /tmp/repomin-pilot-result \
  --json
```

The `--json` result includes the validated ReproMin version, execution backend,
source and payload sizes, attempt/mutation counts, cache uses, and holdout
status. You can paste those scalar fields into the issue template instead of
manually opening the full report; still review the complete `report.json` and
payload for secrets before sharing.

The sidecar contains `report.json` and a human-readable `REPOMIN.md`. Review
both files and the minimized payload for secrets before posting anything.
Do not run exploratory commands directly against the payload while validation
or replay is reading it; use a copy so generated files cannot cause fingerprint
drift.

## Replay a pilot artifact

After reviewing the command and payload, run fresh-copy replay from the same
pilot environment to report whether the configured oracle still matches:

```sh
repomin report replay \
  /tmp/repomin-pilot-result.repomin/report.json \
  --payload /tmp/repomin-pilot-result \
  --runs 2 \
  --yes \
  --json
```

Replay is a current-environment observation, not a correctness, root-cause, or
production-reliability claim. Do not share raw command output or environment
values; review the report and payload for secrets first.

## Share the result

Open [the real CI pilot issue](https://github.com/fly1d/repomin/issues/11) and
include only this summary:

```text
Language and build/test system:
ReproMin version:
Backend and adapter:
Source/text reducer options:
Failure signal shape (redacted):
Payload retained:
Payload removed:
Approximate duration:
What was useful or confusing:
Known limitations:
```

Do not attach the original checkout or unredacted logs. A maintainer may ask
for a public fixture only after the workflow and licensing are clear. Repeatable
workflows can become a benchmark, an examples entry, or a documented
compatibility boundary.
