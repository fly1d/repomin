# ReproMin

![CI](https://github.com/fly1d/repomin/actions/workflows/ci.yml/badge.svg)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License Apache-2.0](https://img.shields.io/github/license/fly1d/repomin)
[![Discussions](https://img.shields.io/github/discussions/fly1d/repomin)](https://github.com/fly1d/repomin/discussions)

ReproMin turns a large failing repository into a smaller, verified bug
reproduction. Give it a repository, the command that fails, and a signal that
identifies the failure. ReproMin removes unnecessary material and keeps an edit
only when the same failure still occurs.

```text
failing repository + command + failure signal
                       |
                       v
          remove something and test again
                       |
                       v
       smaller repository + report + evidence
```

> **Project status:** ReproMin is a pre-alpha feasibility build. Use it on a
> reproducible failure in a repository you trust. The default host backend runs
> your command directly and is not a sandbox.

## What you get

- A reduced repository in the output directory you choose.
- A human-readable receipt and machine-readable `report.json` in a separate
  `OUTPUT.repomin` directory.
- A validation and replay workflow for checking the exported result later.

ReproMin is useful when a bug already reproduces but the repository is too
large to share, review, or keep as a regression fixture.

Two maintainer-led technical pilots show the current scope:

| Pilot | Before | After | Fresh-copy evidence |
| --- | ---: | ---: | ---: |
| [tsdown #979](docs/CASE_STUDY_TSDOWN_979.md) | 14 files | 8 files | 3/3 replays |
| [Gradle #38843](docs/CASE_STUDY_GRADLE_38843.md) | 854 files, 3.8 MB | 11 files, 87.9 KB | 5/5 replays + cold start |

These are feasibility results, not a promise that every repository will shrink
by the same amount. Neither pilot counts as independent adoption.

## Is it the right tool?

| Your goal | Start with |
| --- | --- |
| Find the commit that introduced a regression | `git bisect` |
| Capture the runtime environment and dependencies | A container or ReproZip |
| Minimize one source or compiler input | C-Reduce or Shrink Ray |
| Reduce compiler test cases across files | C-Vise or Perses |
| Shrink a build or application repository and retain evidence | ReproMin |
| Remove one obvious file from a tiny example | Manual editing |

ReproMin treats your build, test, or reproduction command as the authority. It
does not diagnose the root cause and it does not prove that the remaining code
is correct.

## Try it in one command

With [`uv`](https://docs.astral.sh/uv/getting-started/installation/), run the
published release without installing anything into your project or system
Python:

```sh
uvx --from https://github.com/fly1d/repomin/releases/download/v0.1.0.dev10/repomin-0.1.0.dev10-py3-none-any.whl \
  repomin demo ./repomin-demo
```

The demo creates a new directory, runs the real file and text reducers, reduces
three files to two, validates the exact output fingerprint, and prints the
paths and next command. It is network-free after the package is downloaded and
refuses to overwrite an existing path.

For a guided walkthrough, use the [five-minute quick start](docs/QUICKSTART.md),
the [PowerShell quick start](docs/QUICKSTART.windows.md), or the
[中文快速开始](docs/QUICKSTART.zh-CN.md).

## Use it on a real failure

A first run has three steps: check the failure, reduce with a time budget, then
validate the exported evidence.

### 1. Check readiness

Run the read-only Doctor preflight with the command and failure signal you plan
to use:

```sh
repomin doctor . \
  --command 'python -m pytest -q tests/test_checkout.py' \
  --match 'FAILED tests/test_checkout.py' \
  --output ../checkout-repro
```

Doctor checks paths, reducer and toolchain availability, and two fresh baseline
runs without modifying the source repository or creating the output. See the
[Doctor guide](docs/DOCTOR.md) when a check is skipped or fails.

### 2. Run a bounded reduction

Start with a short budget while you evaluate whether the failure oracle and
reducers fit the repository:

```sh
repomin . \
  --command 'python -m pytest -q tests/test_checkout.py' \
  --match 'FAILED tests/test_checkout.py' \
  --max-attempts 25 \
  --max-duration 300 \
  --output ../checkout-repro
```

ReproMin works in temporary copies. It does not modify the source repository
and refuses to overwrite an existing output path. Standard output contains only
the final payload path, while progress and the next validation command go to
standard error.

Choose a failure signal that distinguishes the target bug from setup, import,
or unrelated test failures:

| Signal | Option | Good for |
| --- | --- | --- |
| Stable output text | `--match REGEX` | A distinctive error message or test name |
| Stable process status | `--exit-code CODE` | Failures whose text varies |
| Java/Python exception identity | `--java-exception` / `--python-exception` | Rejecting a different exception with similar text |
| Native crash or exact termination | `--process-failure` | Signals and status codes without stable output |

The [real-failure pilot guide](docs/REAL_FAILURE_PILOT.md) explains how to
design a reliable failure contract. A weak signal can produce a small but
misleading result.

### 3. Validate before sharing

A successful run creates two sibling paths:

```text
checkout-repro/                         reduced repository
checkout-repro.repomin/report.json     machine-readable evidence
checkout-repro.repomin/REPOMIN.md      human-readable receipt
```

Validate the report and exact payload fingerprint without executing the
recorded command:

```sh
repomin report validate ../checkout-repro.repomin/report.json \
  --payload ../checkout-repro \
  --format markdown
```

Inspect the payload and full report before sharing either one. Replay is a
separate, explicit action because it executes the recorded command; review it
first and follow the [replay guide](docs/REPLAY.md).

## What ReproMin can reduce

| Layer | Current coverage |
| --- | --- |
| Repository tree | Files and directories in any trusted local repository |
| Build manifests | Maven, Gradle, Python, Pipenv, Node, Composer, MSBuild, Bundler, Cargo, and Go |
| Source structure | Native Java and Python symbol reducers |
| Selected text | Explicit UTF-8 files reduced by line |
| Custom semantic edits | Optional OpenAI-compatible HTTP integration; every edit still passes the ordinary oracle |

Other languages still benefit from repository, manifest, and explicit text
reduction. ReproMin does not claim semantic source reduction for every language.
Use [examples by ecosystem](docs/EXAMPLES.md) to find the nearest workflow.

For repeatable runs, store the failure contract in a strict
[versioned JSON configuration](docs/CONFIGURATION.md). For CI artifacts, use the
[GitHub Action guide](docs/GITHUB_ACTION.md). Advanced reliability controls,
resumable sessions, Docker limits, reducer invariants, and report fields are
kept in focused documentation rather than repeated here.

## Safety and privacy

The host backend runs the supplied command with your user account. It is not a
sandbox. Only run repositories and commands you trust. Docker can reduce access
when configured carefully, but it is not a complete security boundary either.
Read [SECURITY.md](SECURITY.md) before handling third-party code.

Do not publish credentials, private URLs, proprietary source, customer data,
raw logs, commands, or environment values. Validation with `--format markdown`
uses a path-free, allow-listed summary, but it does not make the payload or full
report safe to publish.

## Install

ReproMin requires Python 3.9 or newer and has no runtime dependencies. The
current pre-alpha release is distributed through GitHub Releases and is not on
PyPI yet. Install it in an isolated virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip

REPOMIN_VERSION=0.1.0.dev10
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v${REPOMIN_VERSION}/repomin-${REPOMIN_VERSION}-py3-none-any.whl"

repomin --version
```

The final command should print `repomin 0.1.0.dev10`. The
[release page](https://github.com/fly1d/repomin/releases/tag/v0.1.0.dev10)
includes SHA-256 checksums. Windows users should follow the
[PowerShell installation steps](docs/QUICKSTART.windows.md).

Shell completion is available for Bash, Zsh, Fish, and PowerShell:

```sh
eval "$(repomin completion bash)"       # Bash
eval "$(repomin completion zsh)"        # Zsh
repomin completion fish | source        # Fish
```

For PowerShell, run:

```powershell
Invoke-Expression (repomin completion powershell | Out-String)
```

## Documentation

The [documentation index](docs/README.md) separates first-run guides from
reference and maintainer material. Common paths are:

| Task | Guide |
| --- | --- |
| Complete the first reduction | [Quick start](docs/QUICKSTART.md) |
| Prepare and preflight a real failure | [Real-failure pilot](docs/REAL_FAILURE_PILOT.md) and [Doctor](docs/DOCTOR.md) |
| Find an ecosystem workflow | [Examples](docs/EXAMPLES.md) |
| Automate a repeatable run | [Configuration](docs/CONFIGURATION.md) and [GitHub Action](docs/GITHUB_ACTION.md) |
| Validate or replay evidence | [Replay](docs/REPLAY.md) and [report schema](docs/REPORT_SCHEMA.md) |
| Understand guarantees or contribute | [Documentation index](docs/README.md) |

Support routes are listed in [SUPPORT.md](SUPPORT.md). Not sure whether your
failure fits? Post a sanitized question with the structured [Q&A Discussion
form](https://github.com/fly1d/repomin/discussions/new?category=q-a).

## Contributing

Real workflow feedback is the most valuable contribution at this stage. You
can:

- report a useful, confusing, blocked, or incompatible trial with the
  [workflow feedback template](https://github.com/fly1d/repomin/issues/new?template=adoption_feedback.md);
- offer a sanitized public repository, revision, command, and failure signal in
  [pilot issue #11](https://github.com/fly1d/repomin/issues/11);
- pick a scoped task from [Good first issues](docs/GOOD_FIRST_ISSUES.md);
- propose a reproducible offline benchmark or a reducer improvement.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing code and follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Security reports belong through the
private route in [SECURITY.md](SECURITY.md), not a public issue.

For development:

```sh
git clone https://github.com/fly1d/repomin.git
cd repomin
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python3 scripts/check_contribution.py
```

## License

Apache-2.0.
