# Doctor preflight

`repomin doctor` checks whether a repository is ready for reduction without
exporting to the configured output directory. It detects supported manifest
and source reducers, checks the selected toolchain, and validates the output
and sibling metadata paths that a normal run would use. When a failure command
is supplied, ReproMin prepares fresh copies but the command can still modify
anything its backend permits.

Doctor skips the Java toolchain check when the included tree has no `.java`
files. Files excluded by ignore rules do not trigger it; a `--keep` override
can include them again. Requesting `--source-reducer java` still requires Java
source files and JDK 11 or newer.

If you installed into the separate tool environment in the
[quick start](QUICKSTART.md), use `"$repomin_cli"` in place of `repomin` in the
examples below, keeping your project environment active.

Start with a static project scan that does not run a project command:

```sh
repomin doctor .
```

A scan without a reproduction command reports `static checks passed; failure
not verified`. Doctor reports `ready to reduce` only after the configured
failure passes its fresh-copy baseline. This distinction prevents a successful
repository scan from being mistaken for proof that the failure reproduces.

Without `--output`, Doctor checks the normal `SOURCE-minimal` default. An
existing payload, an existing `OUTPUT.repomin` sidecar, a symbolic link, or an
output inside the source is reported as a failure because a new reduction would
reject the same path. Pass the intended path when the command depends on its
working-directory basename:

```sh
repomin doctor . --output /tmp/project-repro
```

The output's parent directory must already exist. If a new parent is needed,
create it with `mkdir` before running Doctor or reduction. A missing or
non-directory parent fails the output check before any baseline command runs;
Doctor does not create directories, and reduction does not create output
ancestors implicitly.

Use the same repository exclusion rules as the reduction command when generated
files or nested projects would otherwise affect detection:

```sh
repomin doctor . \
  --gitignore-recursive \
  --gitignore-file .ci/repomin.ignore \
  --output /tmp/project-repro
```

`--gitignore` and `--gitignore-recursive` load the root `.gitignore`; the latter
also discovers nested `.gitignore` files in top-down order. Repeat
`--gitignore-file PATH` for explicit rule files. Rules are applied after the
built-in and exact `--ignore`/`--ignore-path` exclusions, so Doctor's file
counts, adapter detection, source-reducer detection, and optional baseline use
the same effective tree as a reduction. A missing or malformed rule file is a
failed check rather than a silently ignored option.

Doctor and reduction preflight validate filesystem entries in that effective
tree. Excluded entries are omitted from candidate copies, while a
`--keep` override makes its selected entries subject to the usual filesystem
safety checks. Rule files must be readable regular files; repository rule
files cannot be hard-linked or traverse absolute, escaping symbolic links or
reparse points, even when a rule would exclude them. These checks also apply
when an explicit rule-file path reaches the repository through a directory
alias. Explicit external regular rule files remain supported through
`--gitignore-file`.

Pass the same protected paths and explicit text-reduction targets that the
reduction will use:

```sh
repomin doctor . \
  --ignore generated \
  --keep generated/failure.txt \
  --text-file generated/failure.txt \
  --output /tmp/project-repro
```

Repeat `--keep RELATIVE_PATH` for each regular file or directory that the file
reducer must preserve. A protected path overrides built-in, exact, and
gitignore-style exclusions for the target and the ancestors needed to reach
it; protecting a directory also protects its descendants. Doctor applies that
override to its source counts, adapter and source-reducer detection, and fresh
baseline copies, matching the effective tree used by a reduction.

Repeat `--text-file RELATIVE_PATH` for each file that the text reducer will
line-reduce. Doctor checks that every target exists in the effective tree and
is a readable, UTF-8 regular file and that no component of its path is a
symbolic link; it does not reduce the file. Combine `--keep` with `--text-file`
when an exclusion would otherwise hide the target. Both options accept exact
repository-relative paths without glob syntax or Windows drive prefixes.
Duplicate values are collapsed and the normalized `keep_paths` and
`text_files` arrays are sorted in JSON output.

With no `--text-file`, the text-target check is reported as skipped. A valid
selection is reported as passed. A missing, ignored, non-regular, symbolic-link,
unreadable, or non-UTF-8 target is a failed check: Doctor returns `1` and does
not start the optional baseline. Invalid path syntax is an invalid invocation
and returns `2` through the argument parser.

When a reproduction command is available, ask Doctor to run the configured
failure oracle twice in fresh copies before spending time on reduction:

```sh
repomin doctor . \
  --command 'python -m pytest -q tests/test_checkout.py' \
  --match 'AssertionError: checkout total mismatch' \
  --exit-code 1 \
  --adapter python \
  --source-reducer python \
  --output /tmp/project-repro
```

Use `--exit-code` when output is unstable, or `--process-failure` to learn an
exact process termination signature. `--baseline-runs N` changes the number
of fresh checks; it defaults to two. Doctor never treats a command's output as
an issue report and does not include stdout/stderr in its result.

<a id="literal-failure-messages-unreleased"></a>

## Literal failure messages

`--match-text` is available starting with `v0.1.0.dev14`. The `--match`
examples above remain supported.

Use `--match-text` when copying a distinctive message directly from failure
output. It searches for that literal text, so brackets and parentheses do not
need regular-expression escaping:

```sh
repomin doctor . \
  --command 'python -m pytest -q tests/test_checkout.py' \
  --match-text 'FAIL [checkout]: total (12)' \
  --exit-code 1 \
  --output ../checkout-repro
```

`--match` still accepts a regular expression. To require the same message with
that option, use `--match 'FAIL \[checkout\]: total \(12\)'`. Choose either
`--match-text` or `--match`, and keep the same choice and value when running
`repomin reduce`. A literal message should still identify the target failure;
it does not replace checking the exit code or rejecting unrelated failures.

The example uses Bash/Zsh line continuation (`\`). In PowerShell, put the
command on one line or use its backtick continuation. Single quotes preserve
the displayed message and regex in either shell. For a message containing an
apostrophe, Bash/Zsh closes the quoted string and inserts an escaped apostrophe
(`'can'\''t'`); PowerShell doubles it (`'can''t'`).

<a id="continue-with-a-suggested-reduction-unreleased"></a>

## Continue with a suggested reduction

Starting with `v0.1.0.dev14`, Doctor shows a copyable reduction command using
the current Python interpreter and the same ReproMin installation after all
Doctor checks and the fresh-copy failure baseline pass. The isolated launcher
does not select a different ReproMin module from the working directory or
`PYTHONPATH`; the reproduction command still inherits its environment.
A static scan without a verified failure, or any failed check or baseline,
shows no copyable reduction command.

For a CLI invocation, the suggestion keeps the checked failure and input
options, removes Doctor-only output-format flags, and adds `--max-attempts 25`,
`--max-duration 300`, and `--semantic-reducer none` for a bounded first trial.
With `--config`, it keeps the configuration file and uses its budgets and
reducer choices without adding those overrides. Review the suggested command
before running it in the same working directory and environment. The command
is labelled for Bash/Zsh on POSIX systems or PowerShell on Windows. The Windows
suggestion is for Windows PowerShell 5.1 or PowerShell 7, rather than `cmd.exe`.

The suggestion appears only in text output; JSON and privacy-safe Markdown
output do not contain it. Local text can include your reproduction command and
paths, so inspect it before sharing. If the Doctor invocation includes any
explicit `--env` entries, the entire copyable command is omitted and generic
next-step guidance is shown instead. Environment override values are not
echoed in the handoff; supply them yourself when starting the reduction.

## Output formats

Doctor writes its existing detailed text result by default. The equivalent
explicit selection is `--format text`.

For CI scripts, use `--format json`. The existing `--json` flag remains a
compatibility alias for that selection; do not combine `--json` with
`--format`, even when the requested format is also JSON. The result contains
`ok`, detected adapter and source-reducer details, the effective source size
after all exclusion rules, resolved output and metadata paths, normalized
`keep_paths` and `text_files`, per-check status, and (when requested) baseline
pass counts. When gitignore rules are enabled, `gitignore_files`,
`gitignore_sha256`, `gitignore_recursive`, and `gitignore_checked` record the
ordered rule-file labels, a digest of their contents, whether nested discovery
was enabled, and whether loading completed; rule contents are never copied into
the result. JSON is intended for local automation and can contain private paths
and path-bearing diagnostics, so review and redact it before posting it
publicly.

For a deterministic summary intended for an issue, discussion, or other public
feedback, use Markdown:

```sh
repomin doctor . --format markdown
```

The Markdown `status` field uses the same distinction in machine-friendly
form: `static_checks_passed`, `ready_to_reduce`, or `needs_attention`.

The Markdown renderer uses a strict whitelist rather than redacting the full
diagnostic result. It can report ReproMin version and readiness, sanitized
backend and oracle modes, aggregate source size and input-selection counts,
requested and detected reducers, aggregate baseline evidence, and fixed check
names and statuses. Rate-gated baselines include the separate evidence
run/pass counts used for their exact bound, which can differ from total counts
when the first sample discovers a signature. The renderer never includes
source or output paths, commands, match expressions, environment names or
values, detected filenames, selected ignore, keep, text, or gitignore paths,
or raw diagnostic messages. This makes the summary suitable for sharing
whether Doctor passes or explains a blocked trial; it does not make the source
tree, configuration, or full JSON result safe to publish. This guarantee
applies to a successfully emitted Markdown document. Argument/configuration
errors and other invocation failures are reported separately on stderr and can
include the offending private input.

Exit code `0` means all requested checks passed, `1` means a check or baseline
failed, and `2` means the Doctor invocation itself was invalid. Output format
does not change those semantics. A passing baseline only says that the
configured failure was observed in the recorded environment; it is not a
correctness claim.

The baseline runs in disposable copies and inherits the normal host or Docker
trust boundary. Do not run an untrusted command on the host backend, and review
the repository and environment settings before sharing Doctor output.

## When the original command works but the baseline fails

Doctor runs the command in disposable repository copies. A command that depends
on an ignored environment, a generated file, an absolute checkout path, or a
working-directory name can behave differently there.

For Python projects, `.venv` and `venv` are excluded by default. A relative
command such as `.venv/bin/python -m pytest` therefore loses its interpreter in
the copy. Use the absolute interpreter from the project's working environment,
or keep that environment active and use `python`. Installing ReproMin in a new
environment does not install pytest or the project's other dependencies there.
The [Bash/Zsh quick start](QUICKSTART.md#1-install-the-published-release)
keeps the project interpreter and environment while calling ReproMin from a
separate tool environment. The
[PowerShell tour](QUICKSTART.windows.md#3-preflight-and-reduce-the-project)
shows how a process-local `PATH` keeps an environment available to fresh copies;
for a real project, that interpreter must also contain its dependencies.

| Observation | What to check |
| --- | --- |
| The baseline reports `0/2` passes | The complete failure contract was not observed in either run; this does not identify the cause. |
| The last exit code differs from `--exit-code` | Check the executable, relative paths, selected environment, and setup command. A command-not-found exit can come from an excluded `.venv`. |
| The last exit code agrees, but the baseline still fails | Check the actual error in the project environment, the exact marker or regex, and any requested exception/process signature. A missing dependency and the intended failure can share an exit code. |
| The baseline reports a timeout or resource exhaustion | Check whether setup fits the selected timeout and backend limits before changing the failure contract. |

Doctor deliberately omits raw stdout/stderr. Run the trusted command yourself
to inspect its error; if you need to compare fresh-copy behavior, use a separate
scratch checkout with the same exclusions. Do not run diagnostic commands in an
exported payload while validation or replay is reading it. Keep the strict
marker and exit code: broadening the oracle can accept a setup failure instead
of the bug you want to reduce.

## When a passing baseline uses the original Python source

Keeping the project interpreter preserves its dependencies, including any
[editable installation](https://pip.pypa.io/en/stable/topics/local-project-installs/#editable-installs)
that points to the original checkout. An installed copy of the same package or
an absolute `PYTHONPATH` can also select code outside the candidate. A baseline
and replay can then match even after the candidate's package has been removed.
Payload fingerprints check the exported tree, not the origin of its imports.

For a Python `src` layout, put a source check in your reproduction script before
the tests run in that same process. This example covers one ordinary file-backed
package that imports successfully; replace `your_package` with the package under
reduction. Import-time failures need a project-specific check before running the
failing code.

```python
from pathlib import Path
import sys

candidate_root = Path.cwd().resolve()
sys.path.insert(0, str(candidate_root / "src"))
import your_package

try:
    Path(your_package.__file__).resolve().relative_to(candidate_root)
except (TypeError, ValueError):
    print("SETUP: project imported outside this candidate", file=sys.stderr)
    raise SystemExit(2)
```

Keep the oracle script unchanged. `--keep oracle.py` prevents whole-file deletion
only; other passes can still edit it. For this first file-reduction trial, also
use `--adapter none --source-reducer none --semantic-reducer none` and do not
select the oracle with `--text-file`. The failure contract must reject missing
imports and this setup exit instead of accepting
them as the target failure. Check that deleting the package in a separate scratch
copy is rejected. Do not modify an exported payload during validation or replay.

The check covers the selected package's entry file, not every dependency or
submodule. Namespace packages and compiled projects need checks suited to their
paths and build outputs. Selecting `PYTHONPATH=src` alone is insufficient: when
the candidate package disappears, Python can fall back to an installed copy.
Reinstalling from the original checkout or running Doctor again does not verify
the candidate's import origin.
