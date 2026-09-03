# ReproMin quick start

This guide installs the current development release, creates a three-file
failing project, reduces it, and validates the exported evidence. The example
is self-contained and does not access the network after installation.

ReproMin requires Python 3.9 or newer. The commands below use Bash or Zsh on
macOS or Linux. Windows users can follow the complete
[PowerShell quick start](QUICKSTART.windows.md), including the isolated
installation, fixture creation, Doctor preflight, reduction, validation, and
replay steps.

## 1. Install in an isolated environment

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip

REPOMIN_VERSION=0.1.0.dev9
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v${REPOMIN_VERSION}/repomin-${REPOMIN_VERSION}-py3-none-any.whl"

repomin --version
```

The final command should print `repomin 0.1.0.dev9`. The
[release page](https://github.com/fly1d/repomin/releases/tag/v0.1.0.dev9)
publishes SHA-256 digests for users who need to verify the downloaded wheel.

## 2. Create a small failing project

```sh
demo_dir="$(mktemp -d)"
mkdir "$demo_dir/case"

cat > "$demo_dir/case/reproduce.py" <<'PY'
from pathlib import Path
import sys

text = Path("input.txt").read_text(encoding="utf-8")
if "keep-me" not in text:
    print("DIFFERENT_FAILURE", file=sys.stderr)
    raise SystemExit(2)

print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(1)
PY

printf 'keep-me\nremove-me\n' > "$demo_dir/case/input.txt"
printf 'unrelated file\n' > "$demo_dir/case/unused.txt"
```

Confirm the failure contract before reducing anything:

```sh
cd "$demo_dir/case"
python reproduce.py
```

The command prints `ORIGINAL_FAILURE` and exits with status `1`. This marker is
the oracle: ReproMin accepts a candidate only while the command still fails and
its combined output still matches that text.

## 3. Reduce the project

```sh
repomin "$demo_dir/case" \
  --command 'python reproduce.py' \
  --match 'ORIGINAL_FAILURE' \
  --adapter none \
  --source-reducer none \
  --text-file input.txt \
  --output "$demo_dir/reduced"
```

The output must be outside the source directory. ReproMin works in temporary
copies and never overwrites an existing output path.

The reduced payload keeps the command entry point and the required input, but
removes `unused.txt` and the unrelated line from `input.txt`:

```sh
find "$demo_dir/reduced" -type f -print | sort
cat "$demo_dir/reduced/input.txt"
```

Expected payload files:

```text
input.txt
reproduce.py
```

The exact temporary path printed by `find` will differ. The remaining input
line is:

```text
keep-me
```

## 4. Validate the exported evidence

The payload and its evidence sidecar are separate:

```text
reduced/                         reduced repository
reduced.repomin/report.json     machine-readable evidence
reduced.repomin/REPOMIN.md      human-readable receipt
```

Validate the report structure and the recorded payload fingerprint without
rerunning the failure command:

```sh
repomin report validate \
  "$demo_dir/reduced.repomin/report.json" \
  --payload "$demo_dir/reduced" \
  --json
```

A successful result has `"payload_fingerprint_verified": true`. The JSON
summary omits the command, match expression, logs, paths, and environment
values, but you must still inspect the payload and full report before sharing
them.

To execute the recorded oracle again in fresh copies, first review the command
inside `report.json`, then explicitly allow replay:

```sh
repomin report replay \
  "$demo_dir/reduced.repomin/report.json" \
  --payload "$demo_dir/reduced" \
  --runs 2 \
  --yes
```

## Safety boundary

The default host backend runs the supplied command with your user account. It
is not a sandbox. Only use it with repositories and commands you trust. The
Docker backend reduces access when configured carefully, but it is not a
complete security boundary either. Do not publish credentials, private URLs,
proprietary source, customer data, raw logs, or environment values.

## Next steps

- Run the read-only [doctor preflight](DOCTOR.md) before a larger reduction.
- Choose a language or build-tool workflow from the [examples](EXAMPLES.md).
- Add a minimized artifact to CI with the [GitHub Action guide](GITHUB_ACTION.md).
- Read the two public upstream pilots for
  [tsdown](CASE_STUDY_TSDOWN_979.md) and
  [pydoctor](CASE_STUDY_PYDOCTOR_728.md).
- Share a useful, inconclusive, or blocked trial with the
  [user workflow feedback template](https://github.com/fly1d/repomin/issues/new?template=adoption_feedback.md).

For a Chinese version of the same first-run workflow, see the
[Chinese quick start](QUICKSTART.zh-CN.md).
