# ReproMin

![CI](https://github.com/fly1d/repomin/actions/workflows/ci.yml/badge.svg)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License Apache-2.0](https://img.shields.io/github/license/fly1d/repomin)
[![Discussions](https://img.shields.io/github/discussions/fly1d/repomin)](https://github.com/fly1d/repomin/discussions)

**Turn a large failing repository into a small, verified bug reproduction.**

ReproMin is a repository-scale test-case reducer for repeatable Maven, Gradle,
and Python failures. It removes files, manifest entries, source structure, and
selected text only while the same failure still occurs.

## Try a real reduction

With [`uv`](https://docs.astral.sh/uv/getting-started/installation/), run the
published release without installing anything into your project or system
Python:

```sh
uvx --from https://github.com/fly1d/repomin/releases/download/v0.1.0.dev11/repomin-0.1.0.dev11-py3-none-any.whl \
  repomin demo ./repomin-demo
```

The trusted, network-free demo normally finishes in a few seconds. Its key
output looks like this:

```text
ReproMin demo completed.
Reduced: 3 files / 855 bytes -> 2 files / 276 bytes in 14 attempts.
Removed: unused.txt and two unrelated input lines.
Validated: exact payload fingerprint.
Next: run `repomin doctor --help` before trying your own repository.
```

The demo leaves the reduced payload and evidence report available for
inspection and prints a copyable validation command. Continue with the
[five-minute quick start](docs/QUICKSTART.md), the
[PowerShell guide](docs/QUICKSTART.windows.md), or the
[Chinese guide](docs/QUICKSTART.zh-CN.md). For a failing CI job, start with the
[GitHub Action](docs/GITHUB_ACTION.md).

> **Project status:** ReproMin is a pre-alpha feasibility build. Use it on a
> reproducible failure in a repository you trust. The default host backend runs
> your command directly and is not a sandbox.

## Evidence so far

Two maintainer-run pilots demonstrate the current technical scope:

| Pilot | Before | After | Fresh-copy evidence |
| --- | ---: | ---: | ---: |
| [Gradle #38843](docs/CASE_STUDY_GRADLE_38843.md) | 854 files, 3.8 MB | 11 files, 87.9 KB | 5/5 replays + cold start |
| [tsdown #979](docs/CASE_STUDY_TSDOWN_979.md) | 14 files | 8 files | 3/3 replays |

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

## Use it on a real failure

A first run has three steps. ReproMin requires Python 3.9+ and has no runtime
dependencies. From a virtual environment, install the current pre-alpha
release, then check readiness, run a bounded reduction, and validate the
exported evidence:

```sh
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v0.1.0.dev11/repomin-0.1.0.dev11-py3-none-any.whl"

repomin doctor . \
  --command 'python -m pytest -q tests/test_checkout.py' \
  --match 'FAILED tests/test_checkout.py' \
  --output ../checkout-repro

repomin . \
  --command 'python -m pytest -q tests/test_checkout.py' \
  --match 'FAILED tests/test_checkout.py' \
  --max-attempts 25 \
  --max-duration 300 \
  --output ../checkout-repro

repomin report validate ../checkout-repro.repomin/report.json \
  --payload ../checkout-repro \
  --format markdown
```

ReproMin works in temporary copies and never changes the source repository.
Use a signal that identifies the target failure: stable output (`--match`), an
exact exit code, an exception identity, or a process-failure signature. The
[quick start](docs/QUICKSTART.md) explains the complete workflow; use the
[Doctor guide](docs/DOCTOR.md) for failed checks and the
[replay guide](docs/REPLAY.md) before executing an exported command.

## Local CLI or GitHub Action

The CLI is best for interactive reduction. The reusable
[GitHub Action](docs/GITHUB_ACTION.md) can reduce a repeatable CI failure and
upload the payload and validated report as an artifact:

```yaml
- uses: fly1d/repomin@v0.1.0.dev11
  with:
    command: python -m pytest -q tests/test_checkout.py
    match: FAILED tests/test_checkout.py
    max-attempts: 25
    max-duration: 300
```

Pin a reviewed release tag or full commit SHA in real workflows.

## Current coverage

| Layer | Coverage |
| --- | --- |
| Repository tree | Files and directories in any trusted local repository |
| Build manifests | Maven, Gradle, Python, Pipenv, Node, Composer, MSBuild, Bundler, Cargo, and Go |
| Source structure | Native Java and Python reducers |
| Selected text | Explicit UTF-8 files reduced by line |
| Custom semantic edits | Optional OpenAI-compatible HTTP integration; every edit still passes the oracle |

Other languages still benefit from repository, manifest, and explicit text
reduction. Start with the nearest [ecosystem example](docs/EXAMPLES.md).

## Safety and privacy

The host backend runs the supplied command with your user account. It is not a
sandbox. Only run repositories and commands you trust. Docker can reduce
access when configured carefully, but it is not a complete security boundary.
Read [SECURITY.md](SECURITY.md) before handling third-party code.

Do not publish credentials, private URLs, proprietary source, customer data,
raw logs, commands, or environment values. The Markdown validation summary is
path-free and allow-listed, but it does not make the payload or full report
safe to publish.

## Documentation and community

- **Start:** [quick start](docs/QUICKSTART.md), [Windows](docs/QUICKSTART.windows.md),
  [Chinese](docs/QUICKSTART.zh-CN.md), and [ecosystem examples](docs/EXAMPLES.md).
- **Try a real failure:** offer a sanitized public case in
  [pilot issue #11](https://github.com/fly1d/repomin/issues/11).
- **Ask or share:** use [Q&A](https://github.com/fly1d/repomin/discussions/new?category=q-a)
  or [Show and tell](https://github.com/fly1d/repomin/discussions/new?category=show-and-tell).
- **Contribute:** read [CONTRIBUTING.md](CONTRIBUTING.md) and choose an
  [open starter task](https://github.com/fly1d/repomin/issues?q=is%3Aissue%20state%3Aopen%20label%3A%22good%20first%20issue%22).

The [documentation index](docs/README.md) covers configuration, reports, and
design. Use [SUPPORT.md](SUPPORT.md) for other help and [SECURITY.md](SECURITY.md)
for private vulnerability reports.

Apache-2.0 licensed. See [LICENSE](LICENSE).
