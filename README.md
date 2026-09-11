# ReproMin

![CI](https://github.com/fly1d/repomin/actions/workflows/ci.yml/badge.svg)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License Apache-2.0](https://img.shields.io/github/license/fly1d/repomin)
[![Discussions](https://img.shields.io/github/discussions/fly1d/repomin)](https://github.com/fly1d/repomin/discussions)

**Turn a large failing repository into a small, verified bug reproduction.**

ReproMin is a repository-scale test-case reducer for repeatable Maven, Gradle,
and Python failures. It removes files, manifest entries, source structure, and
selected text only while the same failure still occurs.

[Maintainer-run Gradle pilot](https://github.com/fly1d/repomin/blob/main/docs/CASE_STUDY_GRADLE_38843.md): **854 -> 11
files**, with the same failure reproduced in **5/5 fresh copies**.

## Start here

Run the self-contained demo to see a reduction complete in about 30 seconds.
Already have a repeatable failure? Skip to [Reduce a real failure](#reduce-a-real-failure).

### 30-second demo

With [`uv`](https://docs.astral.sh/uv/getting-started/installation/), run the
published release without installing anything into your project or system
Python:

```sh
uvx --from https://github.com/fly1d/repomin/releases/download/v0.1.0.dev13/repomin-0.1.0.dev13-py3-none-any.whl \
  repomin demo ./repomin-demo
```

The trusted, network-free demo normally finishes in a few seconds. Its key
output looks like this:

```text
ReproMin demo completed.
Reduced: 3 files / 855 bytes -> 2 files / 276 bytes in 14 attempts.
Removed: unused.txt and two unrelated input lines.
Validated: exact payload fingerprint.
Next: install ReproMin persistently, then check your repository with `repomin doctor SOURCE`.
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

These are feasibility results, not independent adoption or a promise that
every repository will shrink by the same amount. The current milestone is
[five non-maintainer workflows](https://github.com/fly1d/repomin/issues/11).

## When to use it

Use ReproMin when a bug already repeats but the repository is too large to
share, review, or keep as a regression fixture. It produces a smaller payload,
a human-readable receipt, and a machine-readable report that can be validated
or replayed.

| Your goal | Start with |
| --- | --- |
| Find the commit that introduced a regression | `git bisect` |
| Capture the runtime environment and dependencies | A container or ReproZip |
| Minimize source or compiler inputs | C-Reduce, C-Vise, Perses, or Shrink Ray |
| Shrink a build or application repository and retain evidence | ReproMin |

ReproMin treats your build, test, or reproduction command as the authority. It
does not diagnose the root cause or prove that the remaining code is correct.

## Reduce a real failure

ReproMin requires Python 3.9+ and has no runtime dependencies. First run the
reproduction command yourself and identify a marker that is specific to the
target failure. Then, from a virtual environment, install the current
pre-alpha release, check readiness, run a bounded reduction, and validate the
exported evidence:

```sh
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v0.1.0.dev13/repomin-0.1.0.dev13-py3-none-any.whl"

repomin doctor . \
  --command 'python -m pytest -q tests/test_checkout.py' \
  --match 'AssertionError: checkout total mismatch' \
  --exit-code 1 \
  --output ../checkout-repro

repomin reduce . \
  --command 'python -m pytest -q tests/test_checkout.py' \
  --match 'AssertionError: checkout total mismatch' \
  --exit-code 1 \
  --max-attempts 25 \
  --max-duration 300 \
  --output ../checkout-repro

repomin report validate ../checkout-repro.repomin/report.json \
  --payload ../checkout-repro \
  --format markdown
```

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
