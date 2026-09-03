# Releasing ReproMin

This checklist describes the supported GitHub Release process for the
pre-alpha project. ReproMin is not published to PyPI; do not add a PyPI upload
step without an explicit maintainer decision. The runtime version in
`src/repomin/__init__.py` is the single source of truth; `setup.cfg` reads it
when building wheel and source-distribution metadata.

## Before Tagging

1. Confirm the working tree is clean and the default branch is up to date. Do
   not start a release with generated `build/`, `dist/`, or coverage files in
   the commit.
2. Update `src/repomin/__init__.py` with the release version. Keep the version
   consistent with the Git tag and the wheel/sdist filenames; `setup.cfg` is
   intentionally not edited for each release.
3. Move the relevant entries from the `Unreleased` section of `CHANGELOG.md`
   into a dated version heading. Include user-visible behavior, compatibility
   notes, known limitations, and the verification scope.
4. Update every pinned release reference, not only the wheel URL:
   `README.md`, `docs/QUICKSTART.md`, `docs/QUICKSTART.windows.md`,
   `docs/QUICKSTART.zh-CN.md`, `docs/GITHUB_ACTION.md`, `docs/REPLAY.md`, and
   `docs/REAL_FAILURE_PILOT.md`. This includes both tag or Action refs and the
   `REPOMIN_VERSION` values used in install commands.
   Use `rg -n 'v[0-9]+\.[0-9]+\.[0-9]+|REPOMIN_VERSION' README.md docs action.yml`
   to find stale references, then inspect the diff so historical changelog
   entries remain unchanged.
5. Run the full local verification from the repository root:

   ```sh
   python -m pip install -e ".[dev]"
   python -m unittest discover -s tests -v
   python scripts/check_contribution.py --skip-tests
   python3 benchmarks/run_offline.py --json-output /tmp/repomin-benchmark-results.json
   git diff --check
   ```

   The benchmark JSON must report zero failed checks. Skips are acceptable only
   when the missing optional tool is understood and documented.

## Build And Verify

Install the build tools in the isolated development environment if necessary,
then build into a newly-created temporary directory. A temporary output keeps
the release command from deleting an existing checkout's `dist/` or `build/`:

```sh
python -m pip install --upgrade build twine
release_dir="$(mktemp -d "${TMPDIR:-/tmp}/repomin-release.XXXXXX")"
release_version="X.Y.Z"
release_tag="v$release_version"
python -m build --outdir "$release_dir"
python -m twine check "$release_dir"/*
wheel_path="$release_dir/repomin-$release_version-py3-none-any.whl"
sdist_path="$release_dir/repomin-$release_version.tar.gz"
release_record="$(mktemp "${TMPDIR:-/tmp}/repomin-release-record.XXXXXX")"
python scripts/check_release_artifacts.py \
  --tag "$release_tag" \
  "$wheel_path" \
  "$sdist_path" | tee "$release_record"
printf 'Release artifacts: %s\n' "$release_dir"
printf 'Release record: %s\n' "$release_record"
```

The artifact check requires exactly the expected wheel and source-distribution
filenames, verifies their package name and version metadata, confirms the wheel
is pure Python with the `py3-none-any` tag, and checks the source archive's
top-level directory. Its JSON output records the SHA-256 digest of each
validated file outside the artifact directory; keep that record with the
release notes.

Run the following snippets in one shell session so `release_dir` remains set.
Install the wheel and source distribution in separate temporary virtual
environments. Run both the console entry point and `python -m repomin` from
outside the source tree, then run the complete test suite. Confirm that the
installed package imports from its virtual environment rather than from the
checkout. The following shell sketch uses the validated artifact paths set
above:

```sh
wheel_venv="$(mktemp -d "${TMPDIR:-/tmp}/repomin-wheel-venv.XXXXXX")"
python -m venv "$wheel_venv"
"$wheel_venv/bin/python" -m pip install --no-deps "$wheel_path"
"$wheel_venv/bin/repomin" --help >/dev/null
"$wheel_venv/bin/python" -m repomin --version
"$wheel_venv/bin/repomin" completion bash >/dev/null
"$wheel_venv/bin/python" -m pip check

sdist_venv="$(mktemp -d "${TMPDIR:-/tmp}/repomin-sdist-venv.XXXXXX")"
python -m venv "$sdist_venv"
"$sdist_venv/bin/python" -m pip install --no-deps "$sdist_path"
"$sdist_venv/bin/repomin" --help >/dev/null
"$sdist_venv/bin/python" -m repomin --version
"$sdist_venv/bin/python" -m pip check
```

Run the test suite with each environment's interpreter and an absolute test
path, for example:

```sh
"$wheel_venv/bin/python" -I -m unittest discover -s "$PWD/tests" -v
"$sdist_venv/bin/python" -I -m unittest discover -s "$PWD/tests" -v
```

`--no-deps` makes the no-runtime-dependency claim explicit. Installing an sdist
may still need the build-system requirements declared in `pyproject.toml`;
that build isolation step is expected to contact the package index unless those
requirements are already available locally.

Run `scripts/check_release_artifacts.py` again if either artifact changes. Its
standard-library implementation provides the same structural and SHA-256
checks on Linux, macOS, and Windows. Never publish an artifact whose digest
differs from the recorded JSON output.

## Tag-bound Release Candidate

Pushing a new `v*` tag starts `.github/workflows/release-candidate.yml`. The
workflow has no branch, manual-dispatch, or GitHub Release trigger. It first
requires a tag ref whose name exactly equals `v` plus the runtime version in
`src/repomin/__init__.py`. After building, it passes that same event tag and
the resulting wheel and source distribution to
`scripts/check_release_artifacts.py`. A malformed tag, a source-version
mismatch, a normalized filename mismatch, or inconsistent archive metadata
fails the run before any candidate is stored.

A candidate must also pass installed-package verification before it is stored.
The workflow installs that exact wheel and source distribution with
`--no-deps --no-cache-dir` in separate virtual environments outside the
checkout, checks both the console and `python -m repomin` versions, and runs
an isolated import-location assertion, `pip check`, and the complete test suite
against each installed package. It then recomputes the artifact validation
record and requires it to match the pre-install record, so the tested archive
bytes and recorded SHA-256 digests cannot drift.

A successful run then stores the wheel, source distribution, and
`release-record.json` together in the run-scoped
`release-candidate-vX.Y.Z` Actions artifact for 14 days. This artifact is
temporary release evidence, not a PyPI upload or a GitHub Release asset. The
workflow has read-only repository permissions, does not receive publishing
credentials, and cannot create or modify a GitHub Release. Both the normal CI
workflow and the release-candidate workflow must be green before a maintainer
uses the candidate in the manual process below.

Do not move or recreate an existing release tag to obtain another candidate.
In particular, never rebuild current `main` as `v0.1.0.dev9` or replace the
published `v0.1.0.dev9` assets. Prepare a new version in a reviewed commit and
create a new tag instead. If validation exposes a bad release commit, fix it
under the next version and tag; do not publish artifacts from the failed run.
A transient runner or package-index failure may be rerun against the same
unchanged tag.

## GitHub Release

Confirm that the commit used to build the artifacts is the commit that will be
tagged, then create an annotated tag and push it:

```sh
git rev-parse HEAD
git tag -a vX.Y.Z -m "ReproMin vX.Y.Z"
git rev-parse vX.Y.Z
git push origin vX.Y.Z
```

Create a GitHub Release for that tag and attach exactly the wheel and source
distribution produced by the verification step. Copy the changelog section
into the release notes and include the checksums. Do not rebuild the artifacts
after recording checksums. Keep the release marked as a pre-release while the
project status in `README.md` is pre-alpha. The GitHub Action should continue to
use a reviewed release tag or full commit SHA; do not point it at a mutable
branch for production workflows.

After publishing, verify that:

- the release page links to the expected tag and assets;
- the README, Chinese quick-start, Action, replay, and pilot-guide references
  resolve;
- a clean virtual environment can install each asset;
- `python -m repomin --version` and `repomin --version` agree with the tag;
- the release CI run is successful.

If an artifact is wrong, mark the release as a draft or remove the incorrect
asset before users install it. Never overwrite an existing release asset with a
different file under the same name.
