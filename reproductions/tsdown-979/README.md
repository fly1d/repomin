# Re-run a real CSS reduction with ReproMin dev14

This kit rechecks a prepared tsdown CSS-module reproduction using the **published
ReproMin 0.1.0.dev14 wheel**, without installing ReproMin from the current source
checkout. It includes the complete input, preparation diff, source provenance,
runner and a small recorded result. The existing release wheel is downloaded
separately and verified by the runner; no binary package is committed here.

The maintainer's first run reduced **14 files / 32,788 bytes to 8 files / 31,342
bytes**, followed by **3/3 successful fresh-copy replays**. That is six fewer
files and 1,446 fewer bytes (4.41%). The lockfile dominates the byte total.
This establishes one configured workflow in one environment. It does not show
independent adoption, a speed advantage over another reducer, a globally minimal
result, or a production-ready product. 中文说明见 [README.zh-CN.md](README.zh-CN.md).

## Run

Prerequisites: macOS or Linux, **Node v24.19.0 or v26.5.0** and npm on `PATH`,
and Python 3.9+ with `venv` and `ensurepip`. The measured macOS arm64 combinations
are Python 3.9.6 / Node 24.19.0 and Python 3.13.9 / Node 26.5.0. npm was 12.0.1.
Linux and other combinations remain unverified. The runner stops on other Node
versions to keep the evidence boundary explicit.

From the repository root, download the existing wheel into the reproduction
directory, then preflight and run. The download is explicit; `--check` itself
never fetches it:

```sh
curl --http1.1 --fail --location --retry 2 --connect-timeout 15 --max-time 120 \
  https://github.com/fly1d/repomin/releases/download/v0.1.0.dev14/repomin-0.1.0.dev14-py3-none-any.whl \
  --output reproductions/tsdown-979/repomin-0.1.0.dev14-py3-none-any.whl
```

If your network route blocks the command, download the same named wheel from
the [dev14 release page](https://github.com/fly1d/repomin/releases/tag/v0.1.0.dev14)
and place it in `reproductions/tsdown-979/`. The preflight verifies its checksum.

```sh
python3 reproductions/tsdown-979/run.py --check
python3 reproductions/tsdown-979/run.py ../tsdown-dev14-run
```

The first command checks local prerequisites, fixture bytes and the wheel hash.
It creates no workspace and installs/downloads no dependencies. Expect JSON with
`"status": "ready"`. This does not test registry access or perform the build.
The second command starts the full run; on success expect `"status": "passed"`
and a pointer to the validation summary.

The destination must be a **new directory with an existing parent**. A failed or
completed workspace is retained; use a different name to retry. Paths containing
spaces are supported when quoted. No existing workspace is overwritten.

The runner checks the fixture's file list and SHA-256 hashes, verifies the downloaded
release wheel, installs pnpm 11.8.0 in the new workspace, and warms a separate
dependency store from the public registry. Initial setup needs network access.
Subsequent candidate and replay commands install from that store with `--offline`
and `--frozen-lockfile`. Dependency versions come from the included lockfile,
even though the original `package.json` uses `latest` specifiers.

This executes the included JavaScript and build tools **on the host**. Review
`run.py` and the fixture before running. Offline dependency installation is not
a network sandbox. npm configuration, stores and the Python environment are
task-local; standard proxy/CA settings are preserved. The runner does not install
global packages or change repository protection or release settings.

The maintainer's first complete run took about two minutes. Network setup and
hardware affect the duration. Reduction has a 300-second / 80-attempt budget;
setup and the full recipe have separate bounded stages. A nonzero exit means
the recipe did not pass. Preflight errors appear directly in the terminal and
create no workspace. Once execution has started, the stopped stage and reason
are recorded in the workspace; keep it for local troubleshooting.

## What must pass

1. `doctor` establishes the configured failure before reduction.
2. File reduction and text reduction of `src/index.ts` produce a smaller payload.
   Native source and manifest reduction are disabled; the oracle, lockfile,
   package manifest and license are protected.
3. Report validation verifies the exact locally generated payload fingerprint.
4. Three fresh repository copies reproduce the recorded oracle without using
   ReproMin's result cache.
5. Two separate negative controls reach their intended checks: removing the
   target CSS declaration exits 0 with zero matches; exporting only one CSS
   object exits 2 with the expected insufficient-exports diagnostic. Neither may
   emit the target marker. An installation/build error is not a passing control.
6. The original prepared input remains byte-identical and the exported payload
   still validates after the controls.

The strict oracle deletes stale `dist`, builds successfully, imports the newly
generated module and requires at least two object exports. It accepts only a
duplicated `cursor: pointer` declaration together with marker
`DUPLICATED_CSS_MODULE_COMPOSITION` and exit code 23. This tests a specific
symptom and a limited usability contract; it is not proof of identical root
cause or complete application correctness.

On success, read `validation-summary.md` and inspect `reduced/` in your workspace.
`run-record.json`, the full report and `private-logs/` are **private troubleshooting
materials**: they can contain local paths or command output. Do not attach them
as routine public feedback. The allowlisted `validation-summary.md` is the small
shareable technical result; inspect even that before sharing. The included
`reference-result/` contains only a selected receipt, file hashes, the eight-file
payload and the allowlisted summary. It does not include an executable full
report. Each new run produces and verifies its own report. Candidate ordering,
attempt counts and the retained CSS pair can vary; identical final bytes are not
a success requirement.

## Where the input comes from

The real reproduction repository is
[fly1d/tsdown-css-modules-bug-report](https://github.com/fly1d/tsdown-css-modules-bug-report).
`upstream-input/` contains all 12 files from commit
`094c1471678386c4851fae87922cd7b4d4fac461`; Git blob IDs and SHA-256 hashes are
in `provenance.json`. The upstream MIT license is retained unchanged.

`prepared-input/` adds the public lockfile and strict oracle from commit
`69801202853e56c5e4e970145cbef86aab14007d`. The original entry point logs CSS
objects through a wrapper instead of exporting them. `prepared-input.patch`
explicitly replaces that wrapper with four direct CSS-object exports so the
oracle can import and check the generated module. **No reduced source file is
used to seed the input.** This preparation is part of the experiment.

The old dev9 case's original 14-file prepared directory was not recovered.
This is a **current-version revalidation from transparently prepared public
sources**, not a recovered historical snapshot. Matching historical counts and
byte totals do not prove identity. The measured pnpm here is **11.8.0**, as
declared in the public fixture; the historical case reported 11.19.0.

The separately downloaded wheel is from the existing
[dev14 prerelease](https://github.com/fly1d/repomin/releases/tag/v0.1.0.dev14), with
SHA-256 `763be17e31d0398e204c7fc5b911097ea77a7aab599da4512d53da2f55bd5267`.
Runner and documentation: Apache-2.0, see `LICENSE`. Fixture: MIT, see the
`LICENSE` in each fixture directory. Dependency licenses remain their own.

## If the run stops

| Last stage or message | Next action |
| --- | --- |
| Missing runtime, ensurepip, or different Node version | Use a locally installed matching runtime, then rerun `--check`. No workspace is created by this check. |
| Workspace already exists / parent missing | Choose a new destination whose parent exists. Existing work is retained. |
| `install-pnpm` or `warm-dependencies` | Check public-registry access and the stage's private log locally; do not loosen the lockfile or oracle. |
| `doctor`, reduction or replay | Read `run-record.json` and the matching private log locally. Record a redacted stage/error in feedback. A failed check is not a successful result. |

Use a different new destination to retry. The kit does not delete old workspaces
or change your global runtime configuration.

## Recording a second-person run

[FEEDBACK.md](FEEDBACK.md) has five optional fields: run context, environment,
outcome/blocker, setup cost and actual usefulness. It needs no personal identity
or raw log. Running the included example establishes replication; it does not
automatically establish a user's own real workflow or useful artifact use.
Maintainer reruns and AI-agent technical reviews are **not independent-user
trials**. This kit does not submit feedback, telemetry, posts or messages
automatically.
