# ReproMin

![CI](https://github.com/fly1d/repomin/actions/workflows/ci.yml/badge.svg)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License Apache-2.0](https://img.shields.io/github/license/fly1d/repomin)
[![Discussions](https://img.shields.io/github/discussions/fly1d/repomin)](https://github.com/fly1d/repomin/discussions)

[中文快速开始](https://github.com/fly1d/repomin/blob/main/docs/QUICKSTART.zh-CN.md) · [Inspect the demo](https://github.com/fly1d/repomin/blob/main/docs/DEMO.md)

**Turn a large failing repository into a small, verified bug reproduction.**

ReproMin is a repository-scale test-case reducer for repeatable Maven, Gradle,
and Python failures. It removes files, manifest entries, source structure, and
selected text only while the same failure still occurs.

[![Synthetic demo: three files become two while preserving the same failure](https://raw.githubusercontent.com/fly1d/repomin/main/docs/assets/demo-preview.svg)](https://github.com/fly1d/repomin/blob/main/docs/DEMO.md)

[Maintainer-run Gradle pilot](https://github.com/fly1d/repomin/blob/main/docs/CASE_STUDY_GRADLE_38843.md): **854 -> 11
files**, with the same failure reproduced in **5/5 fresh copies**.

## Start here

Run the self-contained demo to see a reduction complete in about 30 seconds.
Already have a repeatable failure? [Reduce it yourself](#reduce-a-real-failure).
For a public, licensed Maven, Gradle, or Python case, [reply in the pilot thread
with its exact revision, reproduction command, and target failure
signal](https://github.com/fly1d/repomin/issues/11). You do not need to install
ReproMin or complete a run report first. A maintainer will check the fit and
run a bounded first reduction.

### 30-second demo

With [`uv` 0.13.4+](https://docs.astral.sh/uv/getting-started/installation/), run the
published development release from GitHub without installing into your project
or system Python:

```sh
uvx --from "https://github.com/fly1d/repomin/releases/download/v0.1.0.dev15/repomin-0.1.0.dev15-py3-none-any.whl" \
  repomin demo ./repomin-demo
```

The trusted, network-free demo normally finishes in a few seconds. Its key
output looks like this:

```text
ReproMin demo completed.
Reduced: 3 files / 855 bytes -> 2 files / 276 bytes in 14 attempts.
Removed: unused.txt and two unrelated input lines.
Validated: exact payload fingerprint.
```

The demo validates the reduced payload and leaves it with an evidence report
for inspection. To use your own repeatable failure, continue with the
[real-repository quick start](https://github.com/fly1d/repomin/blob/main/docs/QUICKSTART.md).
The [PowerShell walkthrough](https://github.com/fly1d/repomin/blob/main/docs/QUICKSTART.windows.md)
provides a self-contained Windows tour; the
[Chinese quick start](https://github.com/fly1d/repomin/blob/main/docs/QUICKSTART.zh-CN.md)
covers the same real-repository path in Chinese.
For a failing CI job, start with the
[GitHub Action](https://github.com/fly1d/repomin/blob/main/docs/GITHUB_ACTION.md).

> **Project status:** ReproMin is a pre-alpha feasibility build. Use it on a
> reproducible failure in a repository you trust. The default host backend runs
> your command directly and is not a sandbox.

## Evidence so far

Two maintainer-run pilots demonstrate the current technical scope:

| Pilot | Before | After | Fresh-copy evidence |
| --- | ---: | ---: | ---: |
| [Gradle #38843](https://github.com/fly1d/repomin/blob/main/docs/CASE_STUDY_GRADLE_38843.md) | 854 files, 3.8 MB | 11 files, 87.9 KB | 5/5 replays + cold start |
| [tsdown #979](https://github.com/fly1d/repomin/blob/main/docs/CASE_STUDY_TSDOWN_979.md) | 14 files | 8 files | 3/3 replays |

[Inspect the public tsdown reproduction](https://github.com/fly1d/tsdown-css-modules-bug-report/tree/69801202853e56c5e4e970145cbef86aab14007d).
It contains the eight payload files plus a README and `.gitignore` restored
for readers. The Gradle case study records the method and results; it does
not offer a downloadable payload.

These are feasibility results, not independent adoption or a promise that
every repository will shrink by the same amount.

## When to use it

Use ReproMin when a bug already repeats but the repository is too large to
share, review, or keep as a regression fixture. It produces a smaller payload,
a human-readable receipt, and a machine-readable report that can be validated
or replayed.

| Your goal | Start with |
| --- | --- |
| Find the commit that introduced a regression | `git bisect` |
| Capture the runtime environment and dependencies | A container or ReproZip |
| Minimize source or compiler inputs | [C-Reduce](https://github.com/csmith-project/creduce), [C-Vise](https://github.com/marxin/cvise), [Perses](https://github.com/perses-project/perses), or [Shrink Ray](https://github.com/DRMacIver/shrinkray) |
| Reduce multi-file inputs with an interestingness test | [C-Vise's directory workflow](https://github.com/marxin/cvise#usage-with-multi-file-inputs) |
| Reduce Maven/Gradle/Python projects with build-manifest edits and validated replay evidence | ReproMin (pre-alpha) |

Directory reduction is also available in other tools. ReproMin's current
focus is the build/test workflow and its exported evidence; no comparative
performance or output-size advantage has been established.

ReproMin treats your build, test, or reproduction command as the authority. It
does not diagnose the root cause or prove that the remaining code is correct.

## Reduce a real failure

ReproMin requires Python 3.9+ and has no runtime dependencies. For the pip
installation below, use maintained Python 3.11+ and pip 26.2.2+; see the
[installer advisory](https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576).
For an older project interpreter, use the separate tool environment in the
[quick start](https://github.com/fly1d/repomin/blob/main/docs/QUICKSTART.md).
First run the reproduction command yourself and identify a marker that is specific to the
target failure. Then, using the project's working virtual environment, install
the current pre-alpha release, run a bounded reduction, and
validate the exported evidence:

```sh
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v0.1.0.dev15/repomin-0.1.0.dev15-py3-none-any.whl"

repomin reduce . \
  --command 'python -m pytest -q tests/test_checkout.py' \
  --match 'AssertionError: checkout total mismatch' \
  --exit-code 1 \
  --max-attempts 25 \
  --max-duration 300 \
  --semantic-reducer none \
  --output ../checkout-repro
```

Reduction checks the failure in two fresh copies before trying edits; a failed
baseline stops the run. To check readiness separately before a reduction, use
the optional [Doctor](https://github.com/fly1d/repomin/blob/main/docs/DOCTOR.md).
After reduction finishes, validate the result:

```sh
repomin report validate ../checkout-repro.repomin/report.json \
  --payload ../checkout-repro \
  --format markdown
```

Keep the project environment active throughout this example; installing
ReproMin does not install its test dependencies. Fresh copies exclude `.venv`
and `venv`, so use an activated environment or an absolute project interpreter,
rather than a relative `.venv/bin/python` command. The
[quick start](https://github.com/fly1d/repomin/blob/main/docs/QUICKSTART.md)
shows how to retain the project interpreter when installing ReproMin separately.

An editable install can still import the original project from a fresh copy.
Make the reproduction command [check the tested package's source](https://github.com/fly1d/repomin/blob/main/docs/DOCTOR.md#when-a-passing-baseline-uses-the-original-python-source)
on every run; preserving the interpreter does not ensure it tests the candidate.

ReproMin itself prepares candidates in temporary copies and does not write into
the source tree. On the host backend, however, your reproduction command still
runs with your account and can modify any resource it can access.
Do not use a generic marker such as `FAILED`, `error`, or only a test filename:
an unrelated failure could satisfy it. The example requires both a distinctive
failure message and its exact exit code. Before reducing, confirm that the
target repeats and that setup errors or other failures do not emit the same
marker. The
[quick start](https://github.com/fly1d/repomin/blob/main/docs/QUICKSTART.md)
explains the complete real-repository workflow and the available exception and
process signatures; use the
[Doctor guide](https://github.com/fly1d/repomin/blob/main/docs/DOCTOR.md) for
failed checks and the
[replay guide](https://github.com/fly1d/repomin/blob/main/docs/REPLAY.md) before
executing an exported command.

For a repeatable CI failure, the
[GitHub Action](https://github.com/fly1d/repomin/blob/main/docs/GITHUB_ACTION.md)
can upload the reduced payload and validated report as workflow artifacts.

## Current coverage

| Layer | Coverage |
| --- | --- |
| Repository tree | Files and directories in any trusted local repository |
| Build manifests | Maven, Gradle, Python, Pipenv, Node, Composer, MSBuild, Bundler, Cargo, and Go |
| Source structure | Native Java and Python reducers |
| Selected text | Explicit UTF-8 files reduced by line |
| Custom semantic edits | Optional OpenAI-compatible HTTP integration; every edit still passes the oracle |

Other languages still benefit from repository, manifest, and explicit text
reduction. Start with the nearest
[ecosystem example](https://github.com/fly1d/repomin/blob/main/docs/EXAMPLES.md).

## Safety and privacy

The host backend runs the supplied command with your user account. It is not a
sandbox. Only run repositories and commands you trust. Docker can reduce
access when configured carefully, but it is not a complete security boundary.
Read [SECURITY.md](https://github.com/fly1d/repomin/blob/main/SECURITY.md)
before handling third-party code.

The Markdown validation summary is path-free and allow-listed, but it does not
make the payload or full report safe to publish. Review every shared artifact.

## Documentation and community

[Documentation](https://github.com/fly1d/repomin/blob/main/docs/README.md),
[Chinese quick start](https://github.com/fly1d/repomin/blob/main/docs/QUICKSTART.zh-CN.md),
[Q&A](https://github.com/fly1d/repomin/discussions/new?category=q-a),
[Share a result](https://github.com/fly1d/repomin/discussions/new?category=show-and-tell),
[Contribute](https://github.com/fly1d/repomin/blob/main/CONTRIBUTING.md),
[Starter tasks](https://github.com/fly1d/repomin/issues?q=is%3Aissue%20state%3Aopen%20label%3A%22good%20first%20issue%22),
[Support](https://github.com/fly1d/repomin/blob/main/SUPPORT.md)

Apache-2.0 licensed. See
[LICENSE](https://github.com/fly1d/repomin/blob/main/LICENSE).
