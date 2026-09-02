# Public pilot: pydoctor Sphinx API-folder rename failure

This case study records a maintainer-led public pilot completed on 2026-09-02
for [twisted/pydoctor#728](https://github.com/twisted/pydoctor/issues/728) and
the missing reproduction requested on
[twisted/pydoctor#799](https://github.com/twisted/pydoctor/pull/799). The goal
was to turn a persistent but underspecified documentation-build failure into a
small executable fixture and CI check. It does not decide whether the missing
Sphinx subtree should be supported or which upstream fix is correct.

## Why this issue qualified

- Upstream maintainers had repeatedly requested a minimal public reproduction.
- The failure needed only Python packages and local files, with no account,
  credential, private service, or interactive UI.
- The reported `pydoctor 23.4.1` and `Sphinx 7.0.1` combination reproduced the
  same terminal exception in 3 of 3 clean builds.
- Current `pydoctor 25.10.1` with the same Sphinx version also reproduced it in
  3 of 3 clean builds, so the evidence was not limited to an obsolete release.
- The fixture source was written for the pilot and released under MIT, avoiding
  redistribution ambiguity.

The reduction used ReproMin 0.1.0.dev9 with Python 3.13.9 on the macOS host.
The configured failure command used an isolated Python 3.12.13 environment
with `pydoctor 25.10.1` and `Sphinx 7.0.1`.

## Failure and oracle contract

The Sphinx source tree contains only a root `index.rst`. The pydoctor extension
is configured to write API output to `{outdir}/api`:

1. `on_builder_inited` generates that pydoctor output successfully;
2. the extension moves it to `api.pydoctor_temp`;
3. Sphinx generates its root `index.html` but no Sphinx-owned `api/` subtree;
4. `on_build_finished` tries to rename the absent `api` path to
   `api.sphinx_files`; and
5. the build exits with a late `FileNotFoundError`.

The separate `reproduce.py` oracle deletes stale `build/` output, invokes
Sphinx with the current Python interpreter, and emits `ORIGINAL_FAILURE` with
exit code 23 only when all of these conditions hold:

- Sphinx itself returned exit code 2;
- the Sphinx root `index.html` exists;
- pydoctor's temporary API `index.html` exists;
- the exception came from the `on_build_finished` handler; and
- the output identifies the `api -> api.sphinx_files` rename.

Configuration errors, missing source modules, and other documentation failures
exit with code 1. This prevents a generic non-zero build from being mistaken
for the target failure.

## Result

| Evidence | Prepared fixture | Reduced payload |
| --- | ---: | ---: |
| Files | 12 | 9 |
| Bytes | 4,963 | 4,724 |

The run protected the oracle, dependency pins, valid Sphinx index, CI workflow,
README, license, and `.gitignore` from whole-file removal. Structured and
source reducers were disabled; the file reducer removed three intentionally
unrelated files while retaining `docs/conf.py` and `example.py` because the
strict oracle required them.

The reducer reached a fixed point after 10 attempts, with two accepted
mutations and two cache uses. It did not exhaust its attempt or duration
budget. `report validate` confirmed the exact payload fingerprint, and
`report replay` reproduced the recorded match-and-exit-code oracle in 3 of 3
fresh copies.

During verification, an intentionally direct oracle run was mistakenly started
against the exported payload while replay was reading it. The command generated
a `build/` directory, and replay rejected the concurrent mutation with
`payload changed while replay was running`. A clean re-export then passed exact
validation and replay. This is useful boundary evidence: run exploratory
commands on a copy, not concurrently against the payload being certified.

## Public delivery

The exact nine-file result is available in the
[public reproduction repository](https://github.com/fly1d/pydoctor-sphinx-api-repro).
Its pinned
[GitHub Actions run](https://github.com/fly1d/pydoctor-sphinx-api-repro/actions/runs/33602073983)
passed on Ubuntu with Python 3.12. The reproduction and the reason Sphinx does
not create an `api/` subtree were delivered in the
[existing upstream pull request](https://github.com/twisted/pydoctor/pull/799#issuecomment-5505859457)
instead of opening a duplicate issue or fix.

Only the public fixture and scalar reduction evidence were shared. The private
report contains a local interpreter path and was not uploaded; raw build logs,
temporary paths, and local command output were also omitted. Upstream has not
yet responded, so this is delivered reproduction evidence, not adoption or an
accepted fix.

## Reusable lessons

- Recheck an old report against the current release before spending upstream
  attention on it.
- Require successful intermediate artifacts when a late failure is the target;
  exit code alone is too broad.
- Explain the state transition that makes a path absent, not only the final
  exception text.
- Add a public CI oracle so recipients can inspect a stable pass/fail contract
  without recreating the maintainer's workstation.
- Contribute evidence to the existing issue or pull request when the proposed
  fix already exists.
- Keep the exported payload immutable during validation and replay; execute
  manual checks on fresh copies.
