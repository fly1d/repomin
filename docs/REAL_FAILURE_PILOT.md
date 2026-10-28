# Real failure pilot

Get a repeatable failure into a smaller reproduction that helps a bug report,
debugging session, or regression test. Start with the three-item help request
below. The later sections are for running and reviewing a reduction yourself;
they are not prerequisites for asking for help.

For completed public workflows, see the
[tsdown CSS module pilot](CASE_STUDY_TSDOWN_979.md), which records an oracle
weakness found during its first reduction, and the
[Gradle composite-build pilot](CASE_STUDY_GRADLE_38843.md), which records a
large staged reduction and a path-sensitive cache boundary, and the
[pydoctor Sphinx pilot and outreach postmortem](CASE_STUDY_PYDOCTOR_728.md),
which combines a strict late-failure oracle with an important example of
community boundaries that were not respected.

## Offer a public case without installing

You do not need to install ReproMin or design the complete oracle before asking
for help. For a public repository, add these three items to
[pilot issue #11](https://github.com/fly1d/repomin/issues/11):

1. the repository and revision;
2. the local command that fails; and
3. a short error excerpt showing which failure should remain.

A maintainer first checks whether the failure reproduces or identifies the
one missing detail needed to try it. Then they can propose a strict oracle and
run a bounded first reduction. The repository must have an explicit open-source
license, and the command must not require credentials, private services,
special hardware, or production data. The result is not sent to another
project until a human has reviewed the artifact and that project's policies.

## Install the pilot build

The replay and transport-fingerprint workflow described below is included in
the `v0.1.0.dev14` pre-release. Keep the environment that already reproduces
your project's failure active. Install the tool in a separate environment
outside the repository, then call its executable explicitly:

```sh
repomin_env="$HOME/.venvs/repomin"
python3 -m venv "$repomin_env"
REPOMIN_VERSION=0.1.0.dev14
"$repomin_env/bin/python" -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v${REPOMIN_VERSION}/repomin-${REPOMIN_VERSION}-py3-none-any.whl"
repomin_cli="$repomin_env/bin/repomin"
"$repomin_cli" --version
```

Use the snippets below in the same shell so `repomin_cli` remains defined.
Installing ReproMin does not install the project's dependencies. Fresh copies
exclude `.venv` and `venv`, so a relative command such as
`.venv/bin/python -m pytest` cannot find that environment there. For Python,
keep the working project interpreter or use its absolute path; the
[quick start](QUICKSTART.md#1-install-the-published-release) shows how to record
it before installing the tool. Do not activate the tool environment in place
of the project's environment.

Record the release version (and the Git SHA when installing from a source
checkout) in the pilot summary. Users who only capture and validate a report
can use the same release installation described in the [README](../README.md).

## Before sharing

- Identify who selected the task, prepared the fixture, configured the oracle,
  ran the tool, reviewed the result, and wrote the surrounding analysis.
- Disclose any material LLM or agent involvement in the first message. A
  generated `REPOMIN.md` records ReproMin's own role but cannot infer who
  performed the surrounding work.
- Read and follow the recipient project's contribution and AI-assistance
  policies. If the project declines the contribution or asks you to stop, do
  not continue, debate the policy, or send an automated follow-up.
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
"$repomin_cli" reduce /path/to/public-fixture \
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
  --max-attempts 25 \
  --max-duration 300 \
  --output /tmp/repomin-pilot-result
```

The lock file can dominate byte retention while still being essential for a
portable reproduction. Report file counts and the concrete behavior removed,
not only byte ratios.

## Capture a run

Run ReproMin against the sanitized checkout and use the failure signal that is
stable for the project. Replace the marker and exit code with those observed
for the original target failure. For the first reduction, set an attempt budget
of 25 and a duration budget of 300 seconds:

```sh
"$repomin_cli" reduce /path/to/sanitized-project \
  --command './run-failing-test.sh' \
  --match 'STABLE_FAILURE_MARKER' \
  --exit-code 1 \
  --adapter auto \
  --max-attempts 25 \
  --max-duration 300 \
  --output /tmp/repomin-pilot-result
```

When output text is unstable, an exit-only contract is suitable only if the
command reserves that code for the target failure. In this example, `23` must
not occur for setup, import, dependency, or unrelated failures. A generic `1`
often cannot distinguish those cases. Choose a contract that also rejects
those failures; the [Doctor guide](DOCTOR.md) explains the available exception
and process-signature modes.

```sh
"$repomin_cli" reduce /path/to/sanitized-project \
  --command './run-failing-test.sh' \
  --exit-code 23 \
  --adapter auto \
  --max-attempts 25 \
  --max-duration 300 \
  --output /tmp/repomin-pilot-result
```

Validate the payload and its sidecar before inspecting or sharing them:

```sh
"$repomin_cli" report validate \
  /tmp/repomin-pilot-result.repomin/report.json \
  --payload /tmp/repomin-pilot-result \
  --format markdown
```

The Markdown summary contains the validated version, sizes, attempt counts and
validation status without local paths or command output. Review it before
sharing; it replaces manual report fields. Review the complete `report.json`
and payload for secrets separately before sharing either artifact.

The sidecar contains `report.json` and a human-readable `REPOMIN.md`. Review
both files and the minimized payload for secrets before posting anything.
Do not run exploratory commands directly against the payload while validation
or replay is reading it; use a copy so generated files cannot cause fingerprint
drift.

## Replay a pilot artifact

After reviewing the command and payload, run fresh-copy replay from the same
pilot environment to report whether the configured oracle still matches:

```sh
"$repomin_cli" report replay \
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

After a run, paste the reviewed Markdown validation summary in
[the existing thread](https://github.com/fly1d/repomin/issues/11) and add one
answer: what did the result help you do, or where did you get blocked? Generate
the summary with the `report validate --format markdown` command described
above. It already contains the version, sizes, attempts and validation status;
there is no need to retype them. If the run stopped before creating a report,
just share the stopped stage and a short redacted error.

Do not attach the original checkout or unredacted logs. A maintainer may ask
for a public fixture only after the workflow and licensing are clear. Repeatable
workflows can become a benchmark, an examples entry, or a documented
compatibility boundary.
