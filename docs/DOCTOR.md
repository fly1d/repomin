# Doctor preflight

`repomin doctor` checks whether a repository is ready for a reduction without
changing the source tree or creating an output directory. It detects supported
manifest and source reducers, checks the selected toolchain, and validates the
output and sibling metadata paths that a normal run would use.

Start with a read-only project scan:

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
  --command 'python -m pytest -q' \
  --match 'FAILED tests/test_regression.py' \
  --adapter python \
  --source-reducer python \
  --output /tmp/project-repro
```

Use `--exit-code` when output is unstable, or `--process-failure` to learn an
exact process termination signature. `--baseline-runs N` changes the number
of fresh checks; it defaults to two. Doctor never treats a command's output as
an issue report and does not include stdout/stderr in its result.

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
