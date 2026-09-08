from copy import deepcopy
import hashlib
import os
import signal
import shlex
import stat
import sys
import tempfile
import threading
import time
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from repomin.execution import CommandRunner
from repomin.gitignore import GitignoreMatcher
from repomin.model import (
    CANDIDATE_FAMILY_CONTROL_POLICY,
    TREE_FINGERPRINT_POLICY,
    FailureSpec,
    ReductionStats,
    RunResult,
)
from repomin.oracle import (
    BASELINE_RATE_EVIDENCE_FIELDS,
    FailureOracle,
    OracleError,
    anytime_lower_bound,
    exact_binomial_rate_gate,
    wilson_lower_bound,
)
from repomin.session import (
    HoldoutCertificationError,
    MutationCandidate,
    DEFAULT_IGNORES,
    ReductionSession,
    SessionError,
    _copy_repository,
    IgnoreSet,
    _session_identities_match,
    _tree_digest,
    _validate_repository_entries,
)


def _write_candidate(name: str):
    def mutation(root: Path) -> bool:
        (root / name).write_text("candidate\n", encoding="utf-8")
        return True

    return mutation


def _set_tree_mtime(root: Path, mtime_ns: int) -> None:
    entries = [root, *root.rglob("*")]
    entries.sort(
        key=lambda path: len(path.relative_to(root).parts),
        reverse=True,
    )
    for path in entries:
        if path.is_symlink():
            if os.utime not in os.supports_follow_symlinks:
                continue
            os.utime(
                path,
                ns=(mtime_ns, mtime_ns),
                follow_symlinks=False,
            )
        else:
            os.utime(path, ns=(mtime_ns, mtime_ns))


def _clear_tree_flags(root: Path) -> None:
    chflags = getattr(os, "chflags", None)
    if not callable(chflags) or chflags not in os.supports_follow_symlinks:
        return
    try:
        entries = [root, *root.rglob("*")]
    except OSError:
        entries = [root]
    for path in entries:
        try:
            chflags(path, 0, follow_symlinks=False)
        except (FileNotFoundError, OSError):
            pass


class _ConcurrentRunner:
    def __init__(self, participants: int) -> None:
        self.barrier = threading.Barrier(participants)
        self.calls = []
        self.lock = threading.Lock()

    def run(self, cwd: Path) -> RunResult:
        marker = next(cwd.glob("candidate-*"))
        index = int(marker.name.rsplit("-", 1)[-1])
        with self.lock:
            self.calls.append(index)
        self.barrier.wait(timeout=2)
        if index == 0:
            time.sleep(0.1)
        return RunResult(1, "ORIGINAL_FAILURE", "", 0.01)


class _ConcurrentSequenceRunner:
    def __init__(self) -> None:
        self.barrier = threading.Barrier(2)
        self.calls = {0: 0, 1: 0}
        self.lock = threading.Lock()

    def run(self, cwd: Path) -> RunResult:
        marker = next(cwd.glob("candidate-*"))
        candidate = int(marker.name.rsplit("-", 1)[-1])
        with self.lock:
            sample = self.calls[candidate]
            self.calls[candidate] += 1
        if sample == 0:
            self.barrier.wait(timeout=2)
        output = "ORIGINAL_FAILURE" if candidate == 0 else "DIFFERENT_FAILURE"
        return RunResult(1, output, "", 0.01)


class _CountingRunner:
    def __init__(self, output: str = "DIFFERENT_FAILURE") -> None:
        self.output = output
        self.calls = 0

    def run(self, cwd: Path) -> RunResult:
        self.calls += 1
        return RunResult(1, self.output, "", 0.01)


class _CombinedFailureRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def run(self, cwd: Path) -> RunResult:
        with self.lock:
            self.calls += 1
        markers = list(cwd.glob("candidate-*"))
        output = "ORIGINAL_FAILURE" if len(markers) == 1 else "DIFFERENT_FAILURE"
        return RunResult(1, output, "", 0.01)


class _SequenceRunner:
    def __init__(self, results) -> None:
        self.results = iter(results)
        self.calls = 0

    def run(self, cwd: Path) -> RunResult:
        self.calls += 1
        return next(self.results)


class _WritingRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, cwd: Path) -> RunResult:
        self.calls += 1
        (cwd / "command-output.txt").write_text("generated\n", encoding="utf-8")
        return RunResult(1, "ORIGINAL_FAILURE", "", 0.01)


class _PathRecordingRunner:
    def __init__(self) -> None:
        self.paths = []

    def run(self, cwd: Path) -> RunResult:
        self.paths.append(cwd)
        if not (cwd / "seed.txt").is_file():
            return RunResult(2, "DIFFERENT_FAILURE", "", 0.01)
        (cwd / "command-output.txt").write_text("generated\n", encoding="utf-8")
        return RunResult(1, "ORIGINAL_FAILURE", "", 0.01)


class _FreshCopyRunner:
    def __init__(self) -> None:
        self.paths = []
        self.saw_existing_marker = []

    def run(self, cwd: Path) -> RunResult:
        self.paths.append(cwd)
        marker = cwd / "sample-marker.txt"
        self.saw_existing_marker.append(marker.exists())
        marker.write_text("generated\n", encoding="utf-8")
        return RunResult(1, "ORIGINAL_FAILURE", "", 0.01)

    def reset_observations(self) -> None:
        self.paths.clear()
        self.saw_existing_marker.clear()


class _InterruptingRunner:
    def __init__(self, interrupt_at: int) -> None:
        self.interrupt_at = interrupt_at
        self.calls = 0

    def run(self, cwd: Path) -> RunResult:
        self.calls += 1
        if self.calls == self.interrupt_at:
            raise KeyboardInterrupt
        return RunResult(1, "ORIGINAL_FAILURE", "", 0.01)


class _HardStopRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, cwd: Path) -> RunResult:
        self.calls += 1
        raise SystemExit("simulated process termination")


class _ParallelProcessInterruptRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        script = root / "delayed-candidate.py"
        script.write_text(
            """\
import sys
import time
import signal
from pathlib import Path

root = Path(sys.argv[1])
candidate = next(Path.cwd().glob("candidate-*"))
index = candidate.name.rsplit("-", 1)[-1]
(root / ("started-" + index)).write_text("started\\n", encoding="utf-8")
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(1.5)
(root / ("escaped-" + index)).write_text("survived\\n", encoding="utf-8")
""",
            encoding="utf-8",
        )
        command = "%s %s %s" % (
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            shlex.quote(str(root)),
        )
        self.delegate = CommandRunner(command, timeout_seconds=5)
        self.calls = []
        self.lock = threading.Lock()

    def run(self, cwd: Path) -> RunResult:
        marker = next(cwd.glob("candidate-*"))
        index = int(marker.name.rsplit("-", 1)[-1])
        if index == 0:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if all((self.root / ("started-%d" % value)).is_file() for value in (1, 2)):
                    raise KeyboardInterrupt
                time.sleep(0.01)
            raise RuntimeError("parallel candidate commands did not start")
        with self.lock:
            self.calls.append(index)
        return self.delegate.run(cwd)

    def cancel(self) -> None:
        self.delegate.cancel()


class ReductionSessionTest(unittest.TestCase):
    def test_directory_only_gitignore_preserves_same_named_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / ".gitignore").write_text("cache/\n", encoding="utf-8")
            # A trailing slash must not discard a regular file with the same
            # basename, while an actual directory with that basename remains
            # ignored at any depth.
            (source / "cache").write_text("this is a file\n", encoding="utf-8")
            ignored_directory = source / "nested" / "cache"
            ignored_directory.mkdir(parents=True)
            (ignored_directory / "payload.txt").write_text(
                "ignored\n", encoding="utf-8"
            )

            matcher = GitignoreMatcher.from_text("cache/\n")
            ignores = IgnoreSet((), (), matcher)
            _copy_repository(source, destination, ignores)

            self.assertTrue((destination / "cache").is_file())
            self.assertFalse((destination / "nested" / "cache").exists())
            self.assertEqual(
                _tree_digest(source, ignores), _tree_digest(destination, ignores)
            )

    def test_double_star_negation_preserves_deep_file_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            deep = source / "foo" / "x" / "y"
            deep.mkdir(parents=True)
            (source / ".gitignore").write_text(
                "foo/\n!foo/**/keep.txt\n",
                encoding="utf-8",
            )
            (deep / "keep.txt").write_text("keep\n", encoding="utf-8")
            (deep / "drop.txt").write_text("drop\n", encoding="utf-8")

            matcher = GitignoreMatcher.from_text("foo/\n!foo/**/keep.txt\n")
            ignores = IgnoreSet((), (), matcher)
            _copy_repository(source, destination, ignores)

            self.assertTrue((destination / "foo" / "x" / "y" / "keep.txt").is_file())
            self.assertFalse((destination / "foo" / "x" / "y" / "drop.txt").exists())
            self.assertEqual(
                _tree_digest(source, ignores), _tree_digest(destination, ignores)
            )

    def test_session_identity_tracks_gitignore_rules(self) -> None:
        base = {"command": "true"}
        self.assertTrue(_session_identities_match(base, dict(base)))
        legacy_saved = dict(base)
        self.assertTrue(_session_identities_match(legacy_saved, dict(base)))

        with_rules = dict(base)
        with_rules["gitignore_files"] = [".gitignore"]
        with_rules["gitignore_sha256"] = "a" * 64
        self.assertFalse(_session_identities_match(dict(base), with_rules))

        changed_rules = dict(with_rules)
        changed_rules["gitignore_sha256"] = "b" * 64
        self.assertFalse(_session_identities_match(with_rules, changed_rules))

        recursive_rules = dict(with_rules)
        recursive_rules["gitignore_recursive"] = True
        self.assertFalse(_session_identities_match(with_rules, recursive_rules))

        with_keep = dict(base)
        with_keep["keep_paths"] = ["LICENSE"]
        self.assertFalse(_session_identities_match(dict(base), with_keep))

        changed_keep = dict(with_keep)
        changed_keep["keep_paths"] = ["NOTICE"]
        self.assertFalse(_session_identities_match(with_keep, changed_keep))

        with_budget = dict(base)
        with_budget["max_attempts"] = 10
        self.assertFalse(_session_identities_match(dict(base), with_budget))

        changed_budget = dict(with_budget)
        changed_budget["max_attempts"] = 20
        self.assertFalse(_session_identities_match(with_budget, changed_budget))

        with_duration = dict(base)
        with_duration["max_duration_seconds"] = 30.0
        self.assertFalse(_session_identities_match(dict(base), with_duration))

        changed_duration = dict(with_duration)
        changed_duration["max_duration_seconds"] = 60.0
        self.assertFalse(
            _session_identities_match(with_duration, changed_duration)
        )

        with_semantic = dict(base)
        with_semantic["semantic_reducer"] = "http"
        with_semantic["semantic_model"] = "model-a"
        with_semantic["semantic_endpoint"] = "http://localhost:8000/v1/chat/completions"
        self.assertFalse(_session_identities_match(dict(base), with_semantic))

        with_default_semantic_timeout = dict(with_semantic)
        with_default_semantic_timeout["semantic_timeout"] = 60.0
        self.assertFalse(
            _session_identities_match(
                with_semantic,
                with_default_semantic_timeout,
            )
        )

        changed_semantic_timeout = dict(with_default_semantic_timeout)
        changed_semantic_timeout["semantic_timeout"] = 30.0
        self.assertFalse(
            _session_identities_match(
                with_default_semantic_timeout,
                changed_semantic_timeout,
            )
        )

        changed_semantic = dict(with_semantic)
        changed_semantic["semantic_model"] = "model-b"
        self.assertFalse(
            _session_identities_match(with_semantic, changed_semantic)
        )

        with_text = dict(base)
        with_text["text_files"] = ["data.txt"]
        self.assertFalse(_session_identities_match(dict(base), with_text))

        changed_text = dict(with_text)
        changed_text["text_files"] = ["other.txt"]
        self.assertFalse(_session_identities_match(with_text, changed_text))

    def test_session_identity_tracks_gitignore_rule_file_order(self) -> None:
        first = {
            "gitignore_files": ["root.ignore", "local.ignore"],
            "gitignore_sha256": None,
        }
        second = dict(first)
        second["gitignore_files"] = ["local.ignore", "root.ignore"]

        self.assertFalse(_session_identities_match(first, second))

    def test_legacy_ignore_matcher_is_called_once_without_directory_keyword(self) -> None:
        calls = []

        class LegacyMatcher:
            def matches(self, relative):
                calls.append(relative)
                return True

        from repomin.session import _is_ignored

        self.assertTrue(
            _is_ignored(Path("generated/file.txt"), LegacyMatcher(), is_directory=False)
        )
        self.assertEqual([Path("generated/file.txt")], calls)

    def test_ignore_matcher_internal_type_error_is_not_retried(self) -> None:
        calls = []

        class BrokenMatcher:
            def matches(self, relative, is_directory=None):
                calls.append((relative, is_directory))
                raise TypeError("matcher implementation failed")

        from repomin.session import _is_ignored

        with self.assertRaisesRegex(TypeError, "implementation failed"):
            _is_ignored(Path("generated/file.txt"), BrokenMatcher(), is_directory=False)
        self.assertEqual(
            [(Path("generated/file.txt"), False)],
            calls,
        )

    def test_default_ignores_cover_common_generated_dependency_directories(self) -> None:
        self.assertTrue({".vs", "bin", "obj", "vendor"}.issubset(DEFAULT_IGNORES))

    def test_reduction_stats_keeps_legacy_positional_jobs_argument(self) -> None:
        stats = ReductionStats(1, 2, 3)
        self.assertEqual(3, stats.jobs)
        self.assertEqual([], stats.ignored_names)

    def test_execution_working_directory_basename_must_be_one_ordinary_segment(
        self,
    ) -> None:
        for basename in ("", ".", "..", "nested/output", "/absolute"):
            with self.subTest(basename=basename), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, "one ordinary path segment"):
                    self._session(
                        Path(directory),
                        _CountingRunner("ORIGINAL_FAILURE"),
                        execution_working_directory_basename=basename,
                    )

    def test_tree_digest_frames_entry_boundaries(self) -> None:
        def legacy_digest(root: Path) -> str:
            digest = hashlib.sha256()
            paths = sorted(
                root.rglob("*"),
                key=lambda path: path.relative_to(root).as_posix(),
            )
            for path in paths:
                relative = path.relative_to(root).as_posix().encode("utf-8")
                digest.update(relative)
                digest.update(b"\0")
                digest.update(str(path.lstat().st_mode & 0o7777).encode("ascii"))
                digest.update(b"\0F")
                digest.update(path.read_bytes())
                digest.update(b"\0")
            return digest.hexdigest()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            split = root / "split"
            joined = root / "joined"
            split.mkdir(mode=0o755)
            joined.mkdir(mode=0o755)
            split.chmod(0o755)
            joined.chmod(0o755)
            (split / "a").write_bytes(b"prefix")
            (split / "b").write_bytes(b"suffix")
            (split / "a").chmod(0o640)
            (split / "b").chmod(0o640)
            mode = str((split / "b").lstat().st_mode & 0o7777).encode("ascii")
            (joined / "a").write_bytes(b"prefix\0b\0" + mode + b"\0Fsuffix")
            (joined / "a").chmod(0o640)
            _set_tree_mtime(split, 1_700_000_000_000_000_000)
            _set_tree_mtime(joined, 1_700_000_000_000_000_000)

            self.assertEqual(legacy_digest(split), legacy_digest(joined))
            self.assertNotEqual(_tree_digest(split), _tree_digest(joined))

    @unittest.skipIf(os.name == "nt", "POSIX file-mode semantics")
    def test_tree_digest_covers_root_mode_and_entry_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            root_mode_a = root / "root-mode-a"
            root_mode_b = root / "root-mode-b"
            root_mode_a.mkdir()
            root_mode_b.mkdir()
            root_mode_a.chmod(0o755)
            root_mode_b.chmod(0o700)
            _set_tree_mtime(root_mode_a, 1_700_000_000_000_000_000)
            _set_tree_mtime(root_mode_b, 1_700_000_000_000_000_000)
            self.assertNotEqual(_tree_digest(root_mode_a), _tree_digest(root_mode_b))

            mode_a = root / "mode-a"
            mode_b = root / "mode-b"
            mode_a.mkdir(mode=0o755)
            mode_b.mkdir(mode=0o755)
            mode_a.chmod(0o755)
            mode_b.chmod(0o755)
            (mode_a / "entry").write_bytes(b"content")
            (mode_b / "entry").write_bytes(b"content")
            (mode_a / "entry").chmod(0o644)
            (mode_b / "entry").chmod(0o600)
            _set_tree_mtime(mode_a, 1_700_000_000_000_000_000)
            _set_tree_mtime(mode_b, 1_700_000_000_000_000_000)
            self.assertNotEqual(_tree_digest(mode_a), _tree_digest(mode_b))

            path_a = root / "path-a"
            path_b = root / "path-b"
            path_a.mkdir(mode=0o755)
            path_b.mkdir(mode=0o755)
            path_a.chmod(0o755)
            path_b.chmod(0o755)
            (path_a / "entry-a").write_bytes(b"content")
            (path_b / "entry-b").write_bytes(b"content")
            _set_tree_mtime(path_a, 1_700_000_000_000_000_000)
            _set_tree_mtime(path_b, 1_700_000_000_000_000_000)
            self.assertNotEqual(_tree_digest(path_a), _tree_digest(path_b))

            link_a = root / "link-a"
            link_b = root / "link-b"
            link_a.mkdir(mode=0o755)
            link_b.mkdir(mode=0o755)
            link_a.chmod(0o755)
            link_b.chmod(0o755)
            (link_a / "entry").symlink_to("target-a")
            (link_b / "entry").symlink_to("target-b")
            _set_tree_mtime(link_a, 1_700_000_000_000_000_000)
            _set_tree_mtime(link_b, 1_700_000_000_000_000_000)
            self.assertNotEqual(_tree_digest(link_a), _tree_digest(link_b))

            type_a = root / "type-a"
            type_b = root / "type-b"
            type_a.mkdir(mode=0o755)
            type_b.mkdir(mode=0o755)
            type_a.chmod(0o755)
            type_b.chmod(0o755)
            (type_a / "entry").write_bytes(b"target")
            (type_a / "entry").chmod(0o777)
            (type_b / "entry").symlink_to("target")
            _set_tree_mtime(type_a, 1_700_000_000_000_000_000)
            _set_tree_mtime(type_b, 1_700_000_000_000_000_000)
            self.assertNotEqual(_tree_digest(type_a), _tree_digest(type_b))

            mtime_a = root / "mtime-a"
            mtime_b = root / "mtime-b"
            mtime_a.mkdir(mode=0o755)
            mtime_b.mkdir(mode=0o755)
            mtime_a.chmod(0o755)
            mtime_b.chmod(0o755)
            (mtime_a / "entry").write_bytes(b"content")
            (mtime_b / "entry").write_bytes(b"content")
            _set_tree_mtime(mtime_a, 1_700_000_000_000_000_000)
            _set_tree_mtime(mtime_b, 1_700_000_000_000_000_000)
            os.utime(
                mtime_b / "entry",
                ns=(
                    1_700_000_000_000_000_001,
                    1_700_000_000_000_000_001,
                ),
            )
            self.assertNotEqual(_tree_digest(mtime_a), _tree_digest(mtime_b))

    @unittest.skipUnless(
        hasattr(os, "listxattr")
        and hasattr(os, "getxattr")
        and hasattr(os, "setxattr"),
        "extended attribute API unavailable",
    )
    def test_tree_digest_covers_extended_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = root / "plain"
            attributed = root / "attributed"
            plain.mkdir(mode=0o755)
            attributed.mkdir(mode=0o755)
            (plain / "entry").write_bytes(b"content")
            (attributed / "entry").write_bytes(b"content")
            _set_tree_mtime(plain, 1_700_000_000_000_000_000)
            _set_tree_mtime(attributed, 1_700_000_000_000_000_000)
            os.setxattr(attributed / "entry", "user.repomin", b"metadata")

            self.assertNotEqual(_tree_digest(plain), _tree_digest(attributed))

    @unittest.skipUnless(
        hasattr(os, "chflags")
        and os.chflags in os.supports_follow_symlinks
        and hasattr(stat, "UF_NODUMP"),
        "BSD filesystem flags are unavailable",
    )
    def test_harmless_filesystem_flags_are_copied_and_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = root / "plain"
            flagged = root / "flagged"
            copied = root / "copied"
            plain.mkdir()
            flagged.mkdir()
            (plain / "entry").write_bytes(b"content")
            (flagged / "entry").write_bytes(b"content")
            mtime_ns = 1_700_000_000_000_000_000
            _set_tree_mtime(plain, mtime_ns)
            _set_tree_mtime(flagged, mtime_ns)
            try:
                os.chflags(
                    flagged / "entry",
                    stat.UF_NODUMP,
                    follow_symlinks=False,
                )

                _validate_repository_entries(flagged, set())
                self.assertNotEqual(_tree_digest(plain), _tree_digest(flagged))
                _copy_repository(flagged, copied, set())
                self.assertEqual(
                    stat.UF_NODUMP,
                    (copied / "entry").lstat().st_flags & stat.UF_NODUMP,
                )
            finally:
                _clear_tree_flags(flagged)
                _clear_tree_flags(copied)

    @unittest.skipUnless(
        hasattr(os, "chflags")
        and os.chflags in os.supports_follow_symlinks
        and hasattr(stat, "UF_IMMUTABLE")
        and hasattr(stat, "UF_APPEND"),
        "immutable and append-only filesystem flags are unavailable",
    )
    def test_source_tree_mutation_blocking_flags_are_rejected_before_copy(
        self,
    ) -> None:
        for flag_name in ("UF_IMMUTABLE", "UF_APPEND"):
            for target_name in (".", "seed.txt"):
                with self.subTest(flag=flag_name, target=target_name):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        source = root / "source"
                        source.mkdir()
                        seed = source / "seed.txt"
                        seed.write_text("seed\n", encoding="utf-8")
                        target = source if target_name == "." else seed
                        try:
                            os.chflags(
                                target,
                                getattr(stat, flag_name),
                                follow_symlinks=False,
                            )
                            with self.assertRaisesRegex(
                                SessionError,
                                flag_name,
                            ):
                                _tree_digest(source)
                            with patch(
                                "repomin.session.shutil.copytree"
                            ) as copytree:
                                with self.assertRaisesRegex(
                                    SessionError,
                                    r"mutation-blocking filesystem flags \(%s\): %s"
                                    % (flag_name, target_name.replace(".", r"\.")),
                                ):
                                    ReductionSession(
                                        source,
                                        FailureOracle(
                                            _CountingRunner("ORIGINAL_FAILURE"),
                                            FailureSpec("ORIGINAL_FAILURE"),
                                        ),
                                        ReductionStats(
                                            source_files=1,
                                            source_bytes=5,
                                        ),
                                    )
                            copytree.assert_not_called()
                        finally:
                            os.chflags(target, 0, follow_symlinks=False)

    @unittest.skipUnless(
        any(
            hasattr(stat, name)
            for name in ("SF_IMMUTABLE", "SF_APPEND")
        ),
        "system filesystem flags are unavailable",
    )
    def test_repository_validation_rejects_other_mutation_blocking_flags(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flag_names = ["SF_IMMUTABLE", "SF_APPEND"]
            if sys.platform.startswith(("freebsd", "dragonfly")):
                flag_names.extend(("UF_NOUNLINK", "SF_NOUNLINK"))
            for flag_name in flag_names:
                if not hasattr(stat, flag_name):
                    continue

                class FlaggedEntry:
                    name = "protected"
                    path = str(root / name)

                    def stat(self, *, follow_symlinks):
                        self.follow_symlinks = follow_symlinks
                        return type(
                            "FlaggedStatus",
                            (),
                            {
                                "st_mode": stat.S_IFREG | 0o644,
                                "st_flags": getattr(stat, flag_name),
                                "st_nlink": 1,
                            },
                        )()

                entry = FlaggedEntry()
                with self.subTest(flag=flag_name), patch(
                    "repomin.session.os.scandir",
                    return_value=[entry],
                ):
                    with self.assertRaisesRegex(SessionError, flag_name):
                        _validate_repository_entries(root, set())
                    self.assertFalse(entry.follow_symlinks)

    def test_repository_copy_normalizes_atime_and_preserves_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            entry = source / "entry"
            entry.write_bytes(b"content")
            mtime_ns = 1_700_000_000_000_000_000
            os.utime(entry, ns=(mtime_ns + 1234, mtime_ns))

            _copy_repository(source, destination, set())

            copied = (destination / "entry").lstat()
            self.assertEqual(mtime_ns, copied.st_mtime_ns)
            self.assertEqual(mtime_ns, copied.st_atime_ns)

    @unittest.skipUnless(
        hasattr(os, "chflags")
        and os.chflags in os.supports_follow_symlinks
        and hasattr(stat, "UF_IMMUTABLE"),
        "immutable filesystem flags are unavailable",
    )
    def test_repository_copy_race_cleans_protected_destination(self) -> None:
        from repomin import session as session_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            source_entry = source / "entry"
            source_entry.write_text("content\n", encoding="utf-8")
            validate = session_module._validate_repository_entries
            source_validated = False

            def validate_then_protect(path: Path, ignores) -> None:
                nonlocal source_validated
                validate(path, ignores)
                if path == source and not source_validated:
                    source_validated = True
                    os.chflags(
                        source_entry,
                        stat.UF_IMMUTABLE,
                        follow_symlinks=False,
                    )

            try:
                with patch(
                    "repomin.session._validate_repository_entries",
                    side_effect=validate_then_protect,
                ):
                    with self.assertRaisesRegex(SessionError, "UF_IMMUTABLE"):
                        _copy_repository(source, destination, set())
                self.assertFalse(destination.exists())
            finally:
                os.chflags(source_entry, 0, follow_symlinks=False)

    @unittest.skipIf(os.name == "nt", "POSIX atime semantics")
    def test_tree_digest_normalizes_atime_even_when_hashing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "entry"
            entry.write_bytes(b"content")
            mtime_ns = 1_700_000_000_000_000_000
            os.utime(entry, ns=(mtime_ns + 1234, mtime_ns))

            with patch(
                "repomin.session._compute_tree_digest",
                side_effect=OSError("simulated fingerprint failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated fingerprint failure"):
                    _tree_digest(root)

            normalized = entry.lstat()
            self.assertEqual(mtime_ns, normalized.st_mtime_ns)
            self.assertEqual(mtime_ns, normalized.st_atime_ns)

            os.utime(entry, ns=(mtime_ns + 5678, mtime_ns))
            with patch(
                "repomin.session._compute_tree_digest",
                side_effect=OSError("simulated source fingerprint failure"),
            ):
                with self.assertRaises(OSError):
                    _tree_digest(root, normalize_atimes=False)
            self.assertEqual(mtime_ns + 5678, entry.lstat().st_atime_ns)

    def test_tree_digest_rejects_file_metadata_change_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "entry"
            entry.write_bytes(b"content")
            opened = entry.lstat()
            changed = type(
                "ChangedStatus",
                (),
                {
                    "st_mode": opened.st_mode,
                    "st_size": opened.st_size,
                    "st_mtime_ns": opened.st_mtime_ns + 1,
                    "st_dev": opened.st_dev,
                    "st_ino": opened.st_ino,
                    "st_nlink": opened.st_nlink,
                },
            )()

            with patch(
                "repomin.session.os.fstat",
                side_effect=(opened, changed),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "regular file changed while fingerprinting",
                ):
                    _tree_digest(root)

    @unittest.skipIf(os.name == "nt", "POSIX hardlink semantics")
    @unittest.skipUnless(hasattr(os, "link"), "hardlink API unavailable")
    def test_source_tree_hardlink_is_rejected_before_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            seed = source / "seed.txt"
            seed.write_text("seed\n", encoding="utf-8")
            try:
                os.link(seed, source / "alias.txt")
            except OSError as exc:
                self.skipTest("hardlinks unavailable: %s" % exc)

            with patch("repomin.session.shutil.copytree") as copytree:
                with self.assertRaisesRegex(
                    SessionError,
                    "hard-linked regular file",
                ):
                    ReductionSession(
                        source,
                        FailureOracle(
                            _CountingRunner("ORIGINAL_FAILURE"),
                            FailureSpec("ORIGINAL_FAILURE"),
                        ),
                        ReductionStats(source_files=2, source_bytes=10),
                    )
            copytree.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX hardlink semantics")
    @unittest.skipUnless(hasattr(os, "link"), "hardlink API unavailable")
    def test_source_tree_external_hardlink_alias_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            external = root / "external.txt"
            external.write_text("seed\n", encoding="utf-8")
            try:
                os.link(external, source / "seed.txt")
            except OSError as exc:
                self.skipTest("hardlinks unavailable: %s" % exc)

            with self.assertRaisesRegex(SessionError, "hard-linked regular file"):
                ReductionSession(
                    source,
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                    ),
                    ReductionStats(source_files=1, source_bytes=5),
                )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO API unavailable")
    def test_source_tree_fifo_is_rejected_before_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            os.mkfifo(source / "events.fifo")

            with patch("repomin.session.shutil.copytree") as copytree:
                with self.assertRaisesRegex(
                    SessionError,
                    r"unsupported special file \(fifo\)",
                ):
                    ReductionSession(
                        source,
                        FailureOracle(
                            _CountingRunner("ORIGINAL_FAILURE"),
                            FailureSpec("ORIGINAL_FAILURE"),
                        ),
                        ReductionStats(source_files=0, source_bytes=0),
                    )
            copytree.assert_not_called()

    def test_failed_persistent_initialization_removes_partial_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            checkpoint = root / "checkpoint"

            def fail_during_copy(_source, destination, _ignores) -> None:
                destination.mkdir(parents=True)
                (destination / "partial.txt").write_text(
                    "partial\n",
                    encoding="utf-8",
                )
                raise SessionError("simulated copy failure")

            with patch(
                "repomin.session._copy_repository",
                side_effect=fail_during_copy,
            ):
                with self.assertRaisesRegex(SessionError, "simulated copy failure"):
                    ReductionSession(
                        source,
                        FailureOracle(
                            _CountingRunner("ORIGINAL_FAILURE"),
                            FailureSpec("ORIGINAL_FAILURE"),
                        ),
                        ReductionStats(source_files=1, source_bytes=5),
                        session_path=checkpoint,
                    )

            self.assertFalse((checkpoint / "workspace").exists())
            self.assertFalse((checkpoint / "state.json").exists())
            self.assertEqual([], list(checkpoint.glob(".state-*.tmp")))

    def test_source_change_after_copy_rejects_every_session_mode(self) -> None:
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "source"
                    source.mkdir()
                    (source / "seed.txt").write_text(
                        "seed\n",
                        encoding="utf-8",
                    )
                    scratch = root / "scratch"
                    scratch.mkdir()
                    checkpoint = root / "checkpoint"

                    def copy_then_change_source(
                        copy_source,
                        destination,
                        ignores,
                    ) -> None:
                        _copy_repository(copy_source, destination, ignores)
                        (copy_source / "seed.txt").write_text(
                            "changed after copy\n",
                            encoding="utf-8",
                        )

                    options = (
                        {"session_path": checkpoint}
                        if persistent
                        else {"temporary_parent": scratch}
                    )
                    with patch(
                        "repomin.session._copy_repository",
                        side_effect=copy_then_change_source,
                    ):
                        with self.assertRaisesRegex(
                            SessionError,
                            "source repository changed while its initial snapshot "
                            "was copied",
                        ):
                            ReductionSession(
                                source,
                                FailureOracle(
                                    _CountingRunner("ORIGINAL_FAILURE"),
                                    FailureSpec("ORIGINAL_FAILURE"),
                                ),
                                ReductionStats(source_files=1, source_bytes=5),
                                **options,
                            )

                    if persistent:
                        self.assertFalse((checkpoint / "workspace").exists())
                        self.assertFalse((checkpoint / "state.json").exists())
                        self.assertEqual(
                            [],
                            list(checkpoint.glob(".state-*.tmp")),
                        )
                    else:
                        self.assertEqual([], list(scratch.iterdir()))

    @unittest.skipIf(os.name == "nt", "POSIX hardlink semantics")
    @unittest.skipUnless(hasattr(os, "link"), "hardlink API unavailable")
    def test_candidate_hardlink_is_rejected_before_oracle_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner("ORIGINAL_FAILURE")
            session = self._session(Path(directory), runner)

            def create_hardlink(root: Path) -> bool:
                try:
                    os.link(root / "seed.txt", root / "alias.txt")
                except OSError as exc:
                    self.skipTest("hardlinks unavailable: %s" % exc)
                return True

            try:
                with self.assertRaisesRegex(SessionError, "hard-linked regular file"):
                    session.try_mutation(
                        "test",
                        "create hardlink",
                        create_hardlink,
                    )
                self.assertEqual(0, runner.calls)
                self.assertFalse((session.current / "alias.txt").exists())
            finally:
                session.close()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO API unavailable")
    def test_candidate_fifo_is_rejected_before_oracle_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner("ORIGINAL_FAILURE")
            session = self._session(Path(directory), runner)

            def create_fifo(root: Path) -> bool:
                os.mkfifo(root / "events.fifo")
                return True

            try:
                with self.assertRaisesRegex(
                    SessionError,
                    r"unsupported special file \(fifo\)",
                ):
                    session.try_mutation(
                        "test",
                        "create FIFO",
                        create_fifo,
                    )
                self.assertEqual(0, runner.calls)
                self.assertFalse((session.current / "events.fifo").exists())
            finally:
                session.close()

    @unittest.skipUnless(
        hasattr(os, "chflags")
        and os.chflags in os.supports_follow_symlinks
        and hasattr(stat, "UF_IMMUTABLE"),
        "immutable filesystem flags are unavailable",
    )
    def test_candidate_mutation_blocking_flag_is_rejected_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = _CountingRunner("ORIGINAL_FAILURE")
            session = self._session(root, runner)

            def protect_candidate(candidate_root: Path) -> bool:
                os.chflags(
                    candidate_root / "seed.txt",
                    stat.UF_IMMUTABLE,
                    follow_symlinks=False,
                )
                return True

            try:
                with self.assertRaisesRegex(SessionError, "UF_IMMUTABLE"):
                    session.try_mutation(
                        "test",
                        "protect candidate",
                        protect_candidate,
                    )
                self.assertEqual(0, runner.calls)
                self.assertEqual([], list(session.root.glob("trial-*")))
                self.assertEqual(0, (session.current / "seed.txt").lstat().st_flags)
            finally:
                _clear_tree_flags(session.root)
                session.close()

    def test_non_symlink_reparse_directory_is_rejected_before_recursion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class ReparseDirectory:
                name = "junction"
                path = str(root / name)

                def stat(self, *, follow_symlinks):
                    self.follow_symlinks = follow_symlinks
                    return type(
                        "ReparseStatus",
                        (),
                        {
                            "st_mode": stat.S_IFDIR,
                            "st_file_attributes": (
                                stat.FILE_ATTRIBUTE_REPARSE_POINT
                            ),
                        },
                    )()

            entry = ReparseDirectory()
            with patch("repomin.session.os.scandir", return_value=[entry]):
                with self.assertRaisesRegex(SessionError, "reparse point"):
                    _validate_repository_entries(root, set())
            self.assertFalse(entry.follow_symlinks)

    def _session(
        self,
        root: Path,
        runner,
        oracle_min_candidate_rate=None,
        oracle_confidence=0.95,
        **kwargs,
    ) -> ReductionSession:
        source = root / "source"
        source.mkdir()
        (source / "seed.txt").write_text("seed\n", encoding="utf-8")
        oracle = FailureOracle(
            runner,
            FailureSpec("ORIGINAL_FAILURE"),
            min_candidate_rate=oracle_min_candidate_rate,
            confidence=oracle_confidence,
        )
        stats = ReductionStats(source_files=1, source_bytes=5)
        return ReductionSession(source, oracle, stats, **kwargs)

    def _certified_holdout_checkpoint(self, root: Path):
        checkpoint = root / "checkpoint"
        identity = {
            "command": "reproduce",
            "holdout_runs": 3,
            "min_holdout_rate": 0.5,
            "holdout_confidence": 0.5,
        }
        passing = RunResult(1, "ORIGINAL_FAILURE", "", 0.01)
        session = self._session(
            root,
            _CountingRunner("ORIGINAL_FAILURE"),
            session_path=checkpoint,
            identity=identity,
            holdout_runs=3,
            holdout_minimum_rate=0.5,
            holdout_confidence=0.5,
        )
        try:
            session.record_final_validation(passing)
            self.assertEqual("certified", session.run_holdout_certification().status)
        finally:
            session.close()
        state_path = checkpoint / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return checkpoint, identity, state_path, state

    @staticmethod
    def _resume_holdout_checkpoint(root: Path, checkpoint: Path, identity: dict):
        return ReductionSession(
            root / "source",
            FailureOracle(
                _CountingRunner("ORIGINAL_FAILURE"),
                FailureSpec("ORIGINAL_FAILURE"),
            ),
            ReductionStats(source_files=0, source_bytes=0),
            session_path=checkpoint,
            resume=True,
            identity=identity,
            holdout_runs=3,
            holdout_minimum_rate=0.5,
            holdout_confidence=0.5,
        )

    def test_parallel_candidates_run_concurrently_and_choose_lowest_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _ConcurrentRunner(3)
            session = self._session(
                Path(directory), runner, jobs=3, cache_enabled=False
            )
            try:
                candidates = [
                    MutationCandidate(
                        "candidate %d" % index,
                        _write_candidate("candidate-%d" % index),
                    )
                    for index in range(3)
                ]

                accepted = session.try_mutations("test", candidates)

                self.assertEqual(0, accepted)
                self.assertEqual([0, 1, 2], sorted(runner.calls))
                self.assertTrue((session.current / "candidate-0").exists())
                self.assertFalse((session.current / "candidate-1").exists())
                self.assertFalse((session.current / "candidate-2").exists())
                phase = session.stats.phase_stats["test"]
                self.assertEqual(3, phase.attempts)
                self.assertEqual(1, phase.accepted)
                self.assertEqual(2, phase.superseded)
                self.assertEqual(0, phase.rejected)
                self.assertEqual(3, phase.oracle_samples)
                self.assertEqual(3, phase.oracle_sample_uses)
                self.assertAlmostEqual(0.03, phase.oracle_seconds)
            finally:
                session.close()

    @unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
    def test_parallel_interrupt_cancels_every_active_candidate_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = _ParallelProcessInterruptRunner(root)
            session = self._session(
                root,
                runner,
                jobs=3,
                cache_enabled=False,
                candidate_runs=3,
            )
            try:
                candidates = [
                    MutationCandidate(
                        "candidate %d" % index,
                        _write_candidate("candidate-%d" % index),
                    )
                    for index in range(3)
                ]

                with self.assertRaises(KeyboardInterrupt):
                    session.try_mutations("test", candidates)

                self.assertTrue((root / "started-1").is_file())
                self.assertTrue((root / "started-2").is_file())
                time.sleep(1.0)
                self.assertFalse((root / "escaped-1").exists())
                self.assertFalse((root / "escaped-2").exists())
                self.assertEqual([1, 2], sorted(runner.calls))
                self.assertEqual(3, session.stats.phase_stats["test"].aborted)
                self.assertEqual([], list(session.root.glob("trial-*")))
            finally:
                session.close()

    def test_parallel_positive_candidates_can_be_revalidated_as_one_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(
                Path(directory),
                _CountingRunner("ORIGINAL_FAILURE"),
                jobs=3,
                cache_enabled=False,
            )
            try:
                candidates = [
                    MutationCandidate(
                        "candidate %d" % index,
                        _write_candidate("candidate-%d" % index),
                    )
                    for index in range(3)
                ]

                def combine(accepted):
                    def mutation(root: Path) -> bool:
                        return all(candidate.mutation(root) for candidate in accepted)

                    return MutationCandidate("combined candidates", mutation)

                selected = session.try_mutations(
                    "test",
                    candidates,
                    combine_accepted=combine,
                )

                self.assertEqual(0, selected)
                for index in range(3):
                    self.assertTrue((session.current / ("candidate-%d" % index)).is_file())
                phase = session.stats.phase_stats["test"]
                self.assertEqual(4, phase.attempts)
                self.assertEqual(1, phase.accepted)
                self.assertEqual(3, phase.superseded)
                self.assertEqual(0, phase.rejected)
                self.assertEqual(4, phase.oracle_samples)
                self.assertEqual("combined candidates", session.stats.events[-1].description)
            finally:
                session.close()

    def test_run_confidence_numbers_parallel_and_combination_families_deterministically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(
                Path(directory),
                _CountingRunner("ORIGINAL_FAILURE"),
                jobs=3,
                cache_enabled=False,
                oracle_min_candidate_rate=0.2,
                oracle_confidence=0.5,
                candidate_runs=10,
                candidate_min_passes=1,
                candidate_min_rate=0.2,
                run_confidence=0.8,
            )
            observed = []
            observed_lock = threading.Lock()
            original = session._run_trial_repeated

            def record(trial):
                with observed_lock:
                    observed.append(
                        (trial.candidate.description, trial.candidate_family_index)
                    )
                return original(trial)

            session._run_trial_repeated = record
            candidates = [
                MutationCandidate(
                    "candidate %d" % index,
                    _write_candidate("candidate-%d" % index),
                )
                for index in range(3)
            ]

            def combine(accepted):
                def mutation(root: Path) -> bool:
                    return all(candidate.mutation(root) for candidate in accepted)

                return MutationCandidate("combined candidates", mutation)

            try:
                self.assertEqual(
                    0,
                    session.try_mutations(
                        "test",
                        candidates,
                        combine_accepted=combine,
                    ),
                )
                self.assertEqual(
                    [
                        ("candidate 0", 1),
                        ("candidate 1", 2),
                        ("candidate 2", 3),
                        ("combined candidates", 4),
                    ],
                    sorted(observed, key=lambda item: item[1]),
                )
                self.assertEqual(4, session.stats.candidate_family_count)
                event = session.stats.events[-1]
                self.assertEqual(4, event.candidate_family_index)
                self.assertIsNotNone(event.candidate_confidence)
                self.assertIsNotNone(event.candidate_alpha)
            finally:
                session.close()

    def test_run_confidence_skips_no_ops_but_spends_for_cached_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner("DIFFERENT_FAILURE")
            session = self._session(
                Path(directory),
                runner,
                oracle_min_candidate_rate=0.1,
                oracle_confidence=0.5,
                candidate_runs=1,
                candidate_min_passes=1,
                candidate_min_rate=0.1,
                run_confidence=0.1,
            )
            try:
                self.assertFalse(
                    session.try_mutation("test", "no-op", lambda _root: False)
                )
                mutation = _write_candidate("same-content.txt")
                self.assertFalse(session.try_mutation("test", "first", mutation))
                self.assertFalse(session.try_mutation("test", "cached", mutation))
                self.assertEqual(2, session.stats.candidate_family_count)
                self.assertEqual(1, runner.calls)
                self.assertEqual(1, session.stats.cache_hits)
            finally:
                session.close()

    def test_run_confidence_uses_allocated_level_for_early_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(
                Path(directory),
                _CountingRunner("ORIGINAL_FAILURE"),
                oracle_min_candidate_rate=0.4,
                oracle_confidence=0.5,
                candidate_runs=10,
                candidate_min_passes=1,
                candidate_min_rate=0.4,
                run_confidence=0.8,
            )
            try:
                self.assertTrue(
                    session.try_mutation(
                        "test",
                        "accepted with family budget",
                        _write_candidate("kept.txt"),
                    )
                )
                event = session.stats.events[-1]
                self.assertEqual(1, event.candidate_family_index)
                self.assertGreater(event.candidate_confidence, 0.5)
                self.assertAlmostEqual(
                    anytime_lower_bound(
                        event.oracle_passes,
                        event.oracle_runs,
                        event.candidate_confidence,
                    ),
                    event.oracle_anytime_lower_bound,
                )
                self.assertEqual(
                    event.candidate_confidence,
                    session.final_candidate_confidence,
                )
            finally:
                session.close()

    def test_run_confidence_checkpoint_burns_interrupted_family_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {
                "command": "reproduce",
                "min_candidate_rate": 0.1,
                "confidence": 0.5,
                "run_confidence": 0.1,
                "candidate_family_control_policy": (
                    CANDIDATE_FAMILY_CONTROL_POLICY
                ),
            }
            session = self._session(
                root,
                _HardStopRunner(),
                session_path=checkpoint,
                identity=identity,
                oracle_min_candidate_rate=0.1,
                oracle_confidence=0.5,
                candidate_runs=1,
                candidate_min_rate=0.1,
                run_confidence=0.1,
            )
            with self.assertRaisesRegex(SystemExit, "simulated process termination"):
                session.try_mutation(
                    "test",
                    "interrupted family",
                    _write_candidate("interrupted.txt"),
                )
            session.close()

            state = json.loads(
                (checkpoint / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, state["stats"]["candidate_family_count"])
            resumed_runner = _CountingRunner("DIFFERENT_FAILURE")
            resumed = ReductionSession(
                root / "source",
                FailureOracle(
                    resumed_runner,
                    FailureSpec("ORIGINAL_FAILURE"),
                    min_candidate_rate=0.1,
                    confidence=0.5,
                ),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
                candidate_runs=1,
                candidate_min_rate=0.1,
                run_confidence=0.1,
            )
            try:
                self.assertFalse(
                    resumed.try_mutation(
                        "test",
                        "next family",
                        _write_candidate("next.txt"),
                    )
                )
                self.assertEqual(2, resumed.stats.candidate_family_count)
                self.assertEqual(1, resumed_runner.calls)
            finally:
                resumed.close()

    def test_run_confidence_checkpoint_burns_combination_family_budget(self) -> None:
        class PassThenStopRunner:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, _cwd: Path) -> RunResult:
                self.calls += 1
                if self.calls == 3:
                    raise SystemExit("combination stopped")
                return RunResult(1, "ORIGINAL_FAILURE", "", 0.01)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {
                "command": "reproduce",
                "min_candidate_rate": 0.01,
                "confidence": 0.5,
                "run_confidence": 0.5,
                "candidate_family_control_policy": (
                    CANDIDATE_FAMILY_CONTROL_POLICY
                ),
            }
            runner = PassThenStopRunner()
            session = self._session(
                root,
                runner,
                jobs=2,
                cache_enabled=False,
                session_path=checkpoint,
                identity=identity,
                oracle_min_candidate_rate=0.01,
                oracle_confidence=0.5,
                candidate_runs=1,
                candidate_min_rate=0.01,
                run_confidence=0.5,
            )
            candidates = [
                MutationCandidate("first", _write_candidate("first.txt")),
                MutationCandidate("second", _write_candidate("second.txt")),
            ]

            def combine(accepted):
                def mutation(path: Path) -> bool:
                    return all(candidate.mutation(path) for candidate in accepted)

                return MutationCandidate("combined", mutation)

            with self.assertRaisesRegex(SystemExit, "combination stopped"):
                session.try_mutations("test", candidates, combine_accepted=combine)
            session.close()
            state = json.loads(
                (checkpoint / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(3, state["stats"]["candidate_family_count"])
            self.assertEqual(3, state["stats"]["phase_stats"][0]["aborted"])

            resumed_runner = _CountingRunner("DIFFERENT_FAILURE")
            resumed = ReductionSession(
                root / "source",
                FailureOracle(
                    resumed_runner,
                    FailureSpec("ORIGINAL_FAILURE"),
                    min_candidate_rate=0.01,
                    confidence=0.5,
                ),
                ReductionStats(source_files=0, source_bytes=0),
                jobs=2,
                cache_enabled=False,
                session_path=checkpoint,
                resume=True,
                identity=identity,
                candidate_runs=1,
                candidate_min_rate=0.01,
                run_confidence=0.5,
            )
            try:
                self.assertFalse(
                    resumed.try_mutation(
                        "test",
                        "after combination",
                        _write_candidate("after.txt"),
                    )
                )
                self.assertEqual(4, resumed.stats.candidate_family_count)
                self.assertEqual(1, resumed_runner.calls)
            finally:
                resumed.close()

    def test_run_confidence_checkpoint_rejects_tampered_family_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {
                "command": "reproduce",
                "min_candidate_rate": 0.2,
                "confidence": 0.5,
                "run_confidence": 0.8,
                "candidate_family_control_policy": (
                    CANDIDATE_FAMILY_CONTROL_POLICY
                ),
            }
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                session_path=checkpoint,
                identity=identity,
                oracle_min_candidate_rate=0.2,
                oracle_confidence=0.5,
                candidate_runs=10,
                candidate_min_rate=0.2,
                run_confidence=0.8,
            )
            self.assertTrue(
                session.try_mutation(
                    "test",
                    "accepted family",
                    _write_candidate("accepted.txt"),
                )
            )
            session.close()
            state_path = checkpoint / "state.json"
            original_state = json.loads(state_path.read_text(encoding="utf-8"))

            def resume():
                return ReductionSession(
                    root / "source",
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                        min_candidate_rate=0.2,
                        confidence=0.5,
                    ),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                    candidate_runs=10,
                    candidate_min_rate=0.2,
                    run_confidence=0.8,
                )

            mutations = (
                lambda stats: stats.__setitem__("candidate_family_count", 2),
                lambda stats: stats.__setitem__(
                    "candidate_family_alpha_upper_bound", 0.123
                ),
                lambda stats: stats["events"][-1].__setitem__(
                    "candidate_family_index", 0
                ),
                lambda stats: stats["events"][-1].__setitem__(
                    "candidate_confidence", 0.5
                ),
                lambda stats: stats["events"][-1].__setitem__(
                    "candidate_alpha", 0.5
                ),
            )
            for mutation in mutations:
                tampered = deepcopy(original_state)
                mutation(tampered["stats"])
                state_path.write_text(
                    json.dumps(tampered, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(SessionError, "candidate"):
                    resume()

    def test_run_confidence_fails_when_next_family_is_unattainable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner("DIFFERENT_FAILURE")
            session = self._session(
                Path(directory),
                runner,
                oracle_min_candidate_rate=0.1,
                oracle_confidence=0.5,
                candidate_runs=1,
                candidate_min_passes=1,
                candidate_min_rate=0.1,
                run_confidence=0.1,
            )
            try:
                for index in range(2):
                    self.assertFalse(
                        session.try_mutation(
                            "test",
                            "rejected %d" % index,
                            _write_candidate("rejected-%d.txt" % index),
                        )
                    )
                with self.assertRaisesRegex(
                    SessionError,
                    "candidate family 3 is unattainable",
                ):
                    session.try_mutation(
                        "test",
                        "unattainable",
                        _write_candidate("unattainable.txt"),
                    )
                self.assertEqual(3, session.stats.candidate_family_count)
                self.assertEqual(2, runner.calls)
            finally:
                session.close()

    def test_failed_combination_falls_back_to_lowest_positive_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CombinedFailureRunner()
            session = self._session(
                Path(directory),
                runner,
                jobs=3,
                cache_enabled=False,
            )
            try:
                candidates = [
                    MutationCandidate(
                        "candidate %d" % index,
                        _write_candidate("candidate-%d" % index),
                    )
                    for index in range(3)
                ]

                def combine(accepted):
                    def mutation(root: Path) -> bool:
                        return all(candidate.mutation(root) for candidate in accepted)

                    return MutationCandidate("combined candidates", mutation)

                selected = session.try_mutations(
                    "test",
                    candidates,
                    combine_accepted=combine,
                )

                self.assertEqual(0, selected)
                self.assertTrue((session.current / "candidate-0").is_file())
                self.assertFalse((session.current / "candidate-1").exists())
                self.assertFalse((session.current / "candidate-2").exists())
                self.assertEqual(4, runner.calls)
                phase = session.stats.phase_stats["test"]
                self.assertEqual(4, phase.attempts)
                self.assertEqual(1, phase.rejected)
                self.assertEqual(2, phase.superseded)
                self.assertEqual(1, phase.accepted)
                self.assertEqual(0, phase.no_op)
                self.assertEqual(0, phase.aborted)
                self.assertEqual(
                    phase.attempts,
                    phase.no_op
                    + phase.rejected
                    + phase.accepted
                    + phase.superseded
                    + phase.aborted,
                )
                self.assertEqual("candidate 0", session.stats.events[-1].description)
            finally:
                session.close()

    def test_combined_promotion_is_checkpointed_as_one_atomic_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce", "strategy": "combined"}
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                jobs=2,
                cache_enabled=False,
                session_path=checkpoint,
                identity=identity,
            )
            candidates = [
                MutationCandidate("first", _write_candidate("first.txt")),
                MutationCandidate("second", _write_candidate("second.txt")),
            ]

            def combine(accepted):
                def mutation(path: Path) -> bool:
                    return all(candidate.mutation(path) for candidate in accepted)

                return MutationCandidate("combined", mutation)

            self.assertEqual(
                0,
                session.try_mutations(
                    "test",
                    candidates,
                    combine_accepted=combine,
                ),
            )
            session.close()

            state = json.loads((checkpoint / "state.json").read_text(encoding="utf-8"))
            phase = state["stats"]["phase_stats"][0]
            self.assertEqual(3, phase["attempts"])
            self.assertEqual(1, phase["accepted"])
            self.assertEqual(2, phase["superseded"])

            resumed = ReductionSession(
                root / "source",
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                ),
                ReductionStats(source_files=0, source_bytes=0),
                jobs=2,
                cache_enabled=False,
                session_path=checkpoint,
                resume=True,
                identity=identity,
            )
            try:
                self.assertTrue((resumed.current / "first.txt").is_file())
                self.assertTrue((resumed.current / "second.txt").is_file())
                self.assertEqual(1, resumed.stats.accepted)
                self.assertEqual(3, resumed.stats.phase_stats["test"].attempts)
            finally:
                resumed.close()

    def test_parallel_repeated_candidates_keep_early_stop_statistics_separate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _ConcurrentSequenceRunner()
            session = self._session(
                Path(directory),
                runner,
                jobs=2,
                candidate_runs=5,
                candidate_min_passes=2,
            )
            try:
                candidates = [
                    MutationCandidate(
                        "candidate %d" % index,
                        _write_candidate("candidate-%d" % index),
                    )
                    for index in range(2)
                ]

                accepted = session.try_mutations("test", candidates)

                self.assertEqual(0, accepted)
                self.assertEqual({0: 2, 1: 4}, runner.calls)
                self.assertTrue((session.current / "candidate-0").exists())
                self.assertFalse((session.current / "candidate-1").exists())
                self.assertEqual(6, session.stats.candidate_samples)
                self.assertEqual(2, session.stats.candidate_passes)
                self.assertEqual(1, session.stats.candidate_early_acceptances)
                self.assertEqual(1, session.stats.candidate_early_rejections)
                self.assertEqual(4, session.stats.candidate_samples_saved)
                self.assertEqual("candidate 0", session.stats.events[-1].description)
                self.assertTrue(session.stats.events[-1].oracle_early_acceptance)
            finally:
                session.close()

    def test_cache_reuses_result_for_identical_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner()
            session = self._session(Path(directory), runner)
            try:
                mutation = _write_candidate("same-content.txt")

                self.assertFalse(session.try_mutation("test", "first", mutation))
                self.assertFalse(session.try_mutation("test", "second", mutation))

                self.assertEqual(1, runner.calls)
                self.assertEqual(1, session.stats.cache_hits)
                self.assertEqual(2, session.stats.attempts)
                phase = session.stats.phase_stats["test"]
                self.assertEqual(2, phase.rejected)
                self.assertEqual(2, phase.oracle_sample_uses)
                self.assertEqual(1, phase.oracle_samples)
                self.assertEqual(1, phase.cache_hits)
                self.assertAlmostEqual(0.01, phase.oracle_seconds)
            finally:
                session.close()

    def test_nondeterministic_mutation_cannot_promote_an_untested_tree(self) -> None:
        configurations = (
            ("no-cache", {"cache_enabled": False}, 1),
            ("repeated", {"candidate_runs": 2}, 2),
        )
        for label, options, expected_runner_calls in configurations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                calls = 0

                def nondeterministic(root: Path) -> bool:
                    nonlocal calls
                    calls += 1
                    (root / "changing.txt").write_text(
                        "version %d\n" % calls,
                        encoding="utf-8",
                    )
                    return True

                runner = _CountingRunner("ORIGINAL_FAILURE")
                session = self._session(Path(directory), runner, **options)
                try:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "different tree when reapplied",
                    ):
                        session.try_mutation(
                            "test",
                            "nondeterministic mutation",
                            nondeterministic,
                        )
                    self.assertEqual(2, calls)
                    self.assertEqual(expected_runner_calls, runner.calls)
                    self.assertFalse((session.current / "changing.txt").exists())
                    self.assertTrue((session.current / "seed.txt").is_file())
                finally:
                    session.close()

    def test_ignored_only_mutation_is_a_canonical_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner("ORIGINAL_FAILURE")
            session = self._session(Path(directory), runner)

            def create_ignored_output(root: Path) -> bool:
                target = root / "target"
                target.mkdir()
                (target / "marker").write_text("required\n", encoding="utf-8")
                return True

            try:
                self.assertFalse(
                    session.try_mutation(
                        "test",
                        "create ignored output",
                        create_ignored_output,
                    )
                )
                self.assertEqual(0, runner.calls)
                self.assertEqual(1, session.stats.phase_stats["test"].no_op)
                self.assertFalse((session.current / "target").exists())
            finally:
                session.close()

    def test_parallel_window_runs_identical_content_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner()
            session = self._session(Path(directory), runner, jobs=3)
            try:
                candidates = [
                    MutationCandidate(
                        "candidate %d" % index,
                        _write_candidate("same-content.txt"),
                    )
                    for index in range(3)
                ]

                self.assertIsNone(session.try_mutations("test", candidates))

                self.assertEqual(1, runner.calls)
                self.assertEqual(2, session.stats.cache_hits)
                phase = session.stats.phase_stats["test"]
                self.assertEqual(3, phase.rejected)
                self.assertEqual(3, phase.oracle_sample_uses)
                self.assertEqual(1, phase.oracle_samples)
                self.assertEqual(2, phase.cache_hits)
                self.assertAlmostEqual(0.01, phase.oracle_seconds)
            finally:
                session.close()

    @unittest.skipIf(os.name == "nt", "POSIX mtime semantics")
    def test_measured_phase_records_wall_time_and_net_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(
                Path(directory),
                _CountingRunner("ORIGINAL_FAILURE"),
            )
            try:
                def remove_seed(root: Path) -> bool:
                    (root / "seed.txt").unlink()
                    return True

                with session.measure_phase("files"):
                    self.assertTrue(
                        session.try_mutation("files", "remove seed", remove_seed)
                    )

                phase = session.stats.phase_stats["files"]
                self.assertEqual(1, phase.passes)
                self.assertEqual(1, phase.completed_passes)
                self.assertEqual(0, phase.aborted_passes)
                self.assertGreaterEqual(phase.wall_seconds, 0.0)
                self.assertEqual(5, phase.bytes_removed)
                self.assertEqual(0, phase.bytes_added)
                self.assertEqual(
                    phase.attempts,
                    phase.no_op
                    + phase.rejected
                    + phase.accepted
                    + phase.superseded
                    + phase.aborted,
                )
            finally:
                session.close()

    def test_resume_classifies_an_active_phase_as_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce"}
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                session_path=checkpoint,
                identity=identity,
            )
            with session.measure_phase("files"):
                pass
            session.close()

            state_path = checkpoint / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active_phase"] = "files"
            state["stats"]["phase_stats"][0]["completed_passes"] = 0
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            resumed = ReductionSession(
                root / "source",
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                ),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
            )
            try:
                phase = resumed.stats.phase_stats["files"]
                self.assertEqual((1, 0, 1), (
                    phase.passes,
                    phase.completed_passes,
                    phase.aborted_passes,
                ))
                self.assertFalse(resumed.stats.phase_statistics_complete)

                with resumed.measure_phase("files"):
                    pass

                self.assertEqual((2, 1, 1), (
                    phase.passes,
                    phase.completed_passes,
                    phase.aborted_passes,
                ))
            finally:
                resumed.close()

    def test_repeated_early_acceptance_makes_identical_followup_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner("ORIGINAL_FAILURE")
            session = self._session(
                Path(directory),
                runner,
                candidate_runs=5,
                candidate_min_passes=2,
            )
            try:
                mutation = _write_candidate("same-content.txt")

                self.assertTrue(session.try_mutation("test", "first", mutation))
                self.assertFalse(session.try_mutation("test", "second", mutation))

                self.assertEqual(2, runner.calls)
                self.assertEqual(0, session.stats.cache_hits)
                self.assertFalse(session.cache_enabled)
                self.assertEqual(1, session.stats.candidate_early_acceptances)
                self.assertEqual(3, session.stats.candidate_samples_saved)
                self.assertEqual(1, session.stats.phase_stats["test"].no_op)
            finally:
                session.close()

    def test_repeated_candidate_runs_use_fresh_copies_and_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = _PathRecordingRunner()
            session = self._session(
                root,
                runner,
                candidate_runs=3,
                candidate_min_passes=3,
            )
            try:
                session.verify_baseline(1)
                self.assertTrue(
                    session.try_mutation(
                        "test",
                        "repeated accepted mutation",
                        _write_candidate("accepted.txt"),
                    )
                )
                repeated_paths = runner.paths[-3:]
                self.assertEqual(3, len(repeated_paths))
                self.assertEqual(3, len(set(repeated_paths)))
                self.assertTrue(
                    all(path.name.startswith("repeat-") for path in repeated_paths)
                )
                self.assertEqual(3, session.stats.candidate_samples)
                self.assertEqual(3, session.stats.candidate_passes)
                self.assertFalse(session.cache_enabled)
                event = session.stats.events[-1]
                self.assertEqual(3, event.oracle_runs)
                self.assertEqual(3, event.oracle_passes)
                self.assertEqual(1.0, event.oracle_rate)
                self.assertIsNotNone(event.oracle_lower_bound)
                self.assertFalse(event.oracle_early_acceptance)
            finally:
                session.close()

    def test_repeated_candidates_stop_when_count_threshold_is_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner("DIFFERENT_FAILURE")
            session = self._session(
                Path(directory),
                runner,
                candidate_runs=5,
                candidate_min_passes=5,
            )
            try:
                self.assertFalse(
                    session.try_mutation(
                        "test", "rejected mutation", _write_candidate("rejected.txt")
                    )
                )
                self.assertEqual(1, runner.calls)
                self.assertEqual(1, session.stats.candidate_samples)
                self.assertEqual(1, session.stats.candidate_early_rejections)
                self.assertEqual(4, session.stats.candidate_samples_saved)
            finally:
                session.close()

    def test_repeated_candidates_stop_when_rate_is_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            runner = _CountingRunner("DIFFERENT_FAILURE")
            oracle = FailureOracle(
                runner,
                FailureSpec("ORIGINAL_FAILURE"),
                min_candidate_rate=0.6,
            )
            session = ReductionSession(
                source,
                oracle,
                ReductionStats(source_files=1, source_bytes=5),
                candidate_runs=10,
                candidate_min_passes=1,
                candidate_min_rate=0.6,
            )
            try:
                self.assertFalse(
                    session.try_mutation(
                        "test",
                        "rate-rejected mutation",
                        _write_candidate("rejected.txt"),
                    )
                )
                self.assertEqual(2, runner.calls)
                self.assertEqual(2, session.stats.candidate_samples)
                self.assertEqual(1, session.stats.candidate_early_rejections)
                self.assertEqual(8, session.stats.candidate_samples_saved)
            finally:
                session.close()

    def test_count_threshold_accepts_early_and_final_validation_still_runs_fully(
        self,
    ) -> None:
        passing = RunResult(1, "ORIGINAL_FAILURE", "", 0.01)
        with tempfile.TemporaryDirectory() as directory:
            runner = _SequenceRunner([passing] * 7)
            session = self._session(
                Path(directory),
                runner,
                candidate_runs=5,
                candidate_min_passes=2,
            )
            try:
                self.assertTrue(
                    session.try_mutation(
                        "test", "accepted early", _write_candidate("kept.txt")
                    )
                )
                self.assertEqual(2, runner.calls)
                self.assertEqual(2, session.stats.candidate_samples)
                self.assertEqual(2, session.stats.candidate_passes)
                self.assertEqual(1, session.stats.candidate_early_acceptances)
                self.assertEqual(0, session.stats.candidate_early_rejections)
                self.assertEqual(3, session.stats.candidate_samples_saved)
                event = session.stats.events[-1]
                self.assertEqual(2, event.oracle_runs)
                self.assertEqual(2, event.oracle_passes)
                self.assertTrue(event.oracle_early_acceptance)
                self.assertIsNone(event.oracle_anytime_lower_bound)

                final_samples = session.run_current_repeated()
                self.assertEqual(5, len(final_samples))
                self.assertEqual(7, runner.calls)
            finally:
                session.close()

    def test_rate_threshold_accepts_early_with_both_lower_bounds(self) -> None:
        confidence = 0.8
        minimum_rate = 0.4
        planned_runs = 10
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner("ORIGINAL_FAILURE")
            session = self._session(
                Path(directory),
                runner,
                oracle_min_candidate_rate=minimum_rate,
                oracle_confidence=confidence,
                candidate_runs=planned_runs,
                candidate_min_passes=1,
                candidate_min_rate=minimum_rate,
            )
            try:
                self.assertTrue(
                    session.try_mutation(
                        "test", "rate accepted early", _write_candidate("kept.txt")
                    )
                )

                self.assertGreaterEqual(
                    anytime_lower_bound(5, 5, confidence), minimum_rate
                )
                self.assertFalse(
                    exact_binomial_rate_gate(
                        5,
                        planned_runs,
                        minimum_rate,
                        confidence,
                    )
                )
                self.assertEqual(6, runner.calls)
                self.assertGreaterEqual(
                    anytime_lower_bound(6, 6, confidence), minimum_rate
                )
                self.assertTrue(
                    exact_binomial_rate_gate(
                        6,
                        planned_runs,
                        minimum_rate,
                        confidence,
                    )
                )
                self.assertEqual(1, session.stats.candidate_early_acceptances)
                self.assertEqual(4, session.stats.candidate_samples_saved)
                event = session.stats.events[-1]
                self.assertTrue(event.oracle_early_acceptance)
                self.assertAlmostEqual(
                    anytime_lower_bound(6, 6, confidence),
                    event.oracle_anytime_lower_bound,
                )
                self.assertAlmostEqual(
                    wilson_lower_bound(6, 6, confidence), event.oracle_lower_bound
                )
            finally:
                session.close()

    def test_combined_threshold_waits_for_count_after_rate_is_satisfied(self) -> None:
        confidence = 0.8
        minimum_rate = 0.3
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner("ORIGINAL_FAILURE")
            session = self._session(
                Path(directory),
                runner,
                oracle_min_candidate_rate=minimum_rate,
                oracle_confidence=confidence,
                candidate_runs=10,
                candidate_min_passes=7,
                candidate_min_rate=minimum_rate,
            )
            try:
                self.assertTrue(
                    session.try_mutation(
                        "test", "combined accepted early", _write_candidate("kept.txt")
                    )
                )

                self.assertGreaterEqual(
                    anytime_lower_bound(5, 5, confidence), minimum_rate
                )
                self.assertTrue(
                    exact_binomial_rate_gate(5, 10, minimum_rate, confidence)
                )
                self.assertEqual(7, runner.calls)
                self.assertEqual(7, session.stats.events[-1].oracle_passes)
                self.assertTrue(session.stats.events[-1].oracle_early_acceptance)
                self.assertEqual(3, session.stats.candidate_samples_saved)
            finally:
                session.close()

    def test_rate_candidate_uses_exact_decision_at_planned_run(self) -> None:
        confidence = 0.95
        minimum_rate = 0.4
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner("ORIGINAL_FAILURE")
            session = self._session(
                Path(directory),
                runner,
                oracle_min_candidate_rate=minimum_rate,
                oracle_confidence=confidence,
                candidate_runs=5,
                candidate_min_passes=1,
                candidate_min_rate=minimum_rate,
            )
            try:
                self.assertTrue(
                    session.try_mutation(
                        "test", "accepted at planned size", _write_candidate("kept.txt")
                    )
                )

                self.assertLess(wilson_lower_bound(4, 5, confidence), minimum_rate)
                self.assertEqual(5, runner.calls)
                self.assertEqual(0, session.stats.candidate_early_acceptances)
                self.assertEqual(0, session.stats.candidate_samples_saved)
                event = session.stats.events[-1]
                self.assertFalse(event.oracle_early_acceptance)
                self.assertAlmostEqual(
                    wilson_lower_bound(5, 5, confidence), event.oracle_lower_bound
                )
            finally:
                session.close()

    def test_full_size_rate_rejection_is_not_counted_as_early_stopping(self) -> None:
        confidence = 0.95
        minimum_rate = 0.09
        passing = RunResult(1, "ORIGINAL_FAILURE", "", 0.01)
        different = RunResult(1, "DIFFERENT_FAILURE", "", 0.01)
        with tempfile.TemporaryDirectory() as directory:
            runner = _SequenceRunner([passing, different])
            session = self._session(
                Path(directory),
                runner,
                oracle_min_candidate_rate=minimum_rate,
                oracle_confidence=confidence,
                candidate_runs=2,
                candidate_min_passes=1,
                candidate_min_rate=minimum_rate,
            )
            try:
                self.assertFalse(
                    session.try_mutation(
                        "test",
                        "rejected at planned size",
                        _write_candidate("rejected.txt"),
                    )
                )

                self.assertLess(anytime_lower_bound(1, 1, confidence), minimum_rate)
                self.assertGreaterEqual(
                    wilson_lower_bound(1, 2, confidence), minimum_rate
                )
                self.assertFalse(
                    exact_binomial_rate_gate(1, 2, minimum_rate, confidence)
                )
                self.assertEqual(2, runner.calls)
                self.assertEqual(2, session.stats.candidate_samples)
                self.assertEqual(1, session.stats.candidate_passes)
                self.assertEqual(0, session.stats.candidate_early_acceptances)
                self.assertEqual(0, session.stats.candidate_early_rejections)
                self.assertEqual(0, session.stats.candidate_samples_saved)
                self.assertFalse(session.stats.events)
            finally:
                session.close()

    def test_resource_failure_stops_remaining_candidate_samples(self) -> None:
        exhausted = RunResult(
            137,
            "ORIGINAL_FAILURE",
            "",
            0.01,
            resource_exhausted=True,
            resource_reason="memory",
        )
        with tempfile.TemporaryDirectory() as directory:
            runner = _SequenceRunner([exhausted])
            session = self._session(
                Path(directory),
                runner,
                candidate_runs=5,
                candidate_min_passes=1,
            )
            try:
                self.assertFalse(
                    session.try_mutation(
                        "test",
                        "resource-rejected mutation",
                        _write_candidate("rejected.txt"),
                    )
                )
                self.assertEqual(1, runner.calls)
                self.assertEqual(0, session.stats.candidate_early_acceptances)
                self.assertEqual(1, session.stats.candidate_early_rejections)
                self.assertEqual(4, session.stats.candidate_samples_saved)
            finally:
                session.close()

    def test_command_writes_do_not_enter_accepted_or_exported_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = _WritingRunner()
            session = self._session(root, runner)
            try:
                self.assertTrue(
                    session.try_mutation(
                        "test", "accepted mutation", _write_candidate("accepted.txt")
                    )
                )
                self.assertFalse((session.current / "command-output.txt").exists())

                session.verify_baseline(2)
                self.assertFalse((session.current / "command-output.txt").exists())
                self.assertTrue(session.oracle.accepts(session.run_current()))
                self.assertFalse((session.current / "command-output.txt").exists())

                output = root / "output"
                session.export(output)
                self.assertTrue((output / "accepted.txt").exists())
                self.assertFalse((output / "command-output.txt").exists())
            finally:
                session.close()

    @unittest.skipUnless(
        hasattr(os, "chflags")
        and os.chflags in os.supports_follow_symlinks
        and hasattr(stat, "UF_IMMUTABLE"),
        "immutable filesystem flags are unavailable",
    )
    def test_command_created_immutable_files_are_cleaned_from_private_copies(
        self,
    ) -> None:
        class ProtectingRunner:
            def __init__(self) -> None:
                self.paths = []

            def run(self, cwd: Path) -> RunResult:
                self.paths.append(cwd)
                protected = cwd / "command-protected.txt"
                protected.write_text("generated\n", encoding="utf-8")
                os.chflags(
                    protected,
                    stat.UF_IMMUTABLE,
                    follow_symlinks=False,
                )
                return RunResult(1, "ORIGINAL_FAILURE", "", 0.01)

            def cancel(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = ProtectingRunner()
            session = self._session(root, runner)
            try:
                session.verify_baseline(2)
                self.assertTrue(
                    session.try_mutation(
                        "test",
                        "accepted mutation",
                        _write_candidate("accepted.txt"),
                    )
                )
                self.assertTrue(session.oracle.accepts(session.run_current()))
                self.assertTrue(runner.paths)
                self.assertTrue(all(not path.exists() for path in runner.paths))
                self.assertEqual(
                    [],
                    [
                        path
                        for path in session.root.iterdir()
                        if path.name.startswith(
                            ("baseline-", "trial-", "validation-")
                        )
                    ],
                )
                self.assertFalse(
                    (session.current / "command-protected.txt").exists()
                )
            finally:
                _clear_tree_flags(session.root)
                session.close()

    @unittest.skipIf(os.name == "nt", "POSIX file-mode/mtime semantics")
    def test_uncertified_export_rejects_metadata_loss_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(root, _CountingRunner())
            output = root / "output"

            def lossy_copy(source: Path, destination: Path, ignores) -> None:
                _copy_repository(source, destination, ignores)
                entry = destination / "seed.txt"
                copied = entry.lstat()
                os.utime(
                    entry,
                    ns=(copied.st_atime_ns, copied.st_mtime_ns + 1),
                )

            try:
                with patch(
                    "repomin.session._copy_repository",
                    side_effect=lossy_copy,
                ):
                    with self.assertRaisesRegex(
                        SessionError,
                        "staged export differs from the oracle-validated artifact",
                    ):
                        session.export(output)

                self.assertFalse(output.exists())
                self.assertEqual([], list(root.glob(".repomin-export-*")))
            finally:
                session.close()

    def test_export_copy_failure_cleans_partial_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(root, _CountingRunner())
            output = root / "output"

            def partial_copy(_source: Path, destination: Path, _ignores) -> None:
                destination.mkdir()
                (destination / "partial.txt").write_text(
                    "partial\n", encoding="utf-8"
                )
                raise OSError("simulated copy failure")

            try:
                with patch(
                    "repomin.session._copy_repository",
                    side_effect=partial_copy,
                ):
                    with self.assertRaisesRegex(OSError, "simulated copy failure"):
                        session.export(output)

                self.assertFalse(output.exists())
                self.assertEqual([], list(root.glob(".repomin-export-*")))
            finally:
                session.close()

    def test_export_publish_failure_cleans_read_only_nested_staging(self) -> None:
        import errno

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(root, _CountingRunner())
            output = root / "output"
            read_only = session.current / "read-only"
            nested = read_only / "nested"
            nested.mkdir(parents=True)
            (nested / "payload.txt").write_text("payload\n", encoding="utf-8")
            nested.chmod(0o555)
            read_only.chmod(0o555)

            try:
                publish_error = OSError(
                    errno.EIO,
                    "simulated atomic publish failure",
                )
                with patch(
                    "repomin.session._rename_directory_no_replace",
                    side_effect=publish_error,
                ):
                    with self.assertRaises(OSError) as raised:
                        session.export(output)

                self.assertEqual(errno.EIO, raised.exception.errno)
                self.assertIn("simulated atomic publish failure", str(raised.exception))
                self.assertFalse(output.exists())
                self.assertEqual([], list(root.glob(".repomin-export-*")))
            finally:
                read_only.chmod(0o755)
                nested.chmod(0o755)
                session.close()

    @unittest.skipUnless(
        hasattr(os, "chflags")
        and os.chflags in os.supports_follow_symlinks
        and hasattr(stat, "UF_IMMUTABLE"),
        "immutable file flags are unavailable",
    )
    def test_export_publish_failure_cleans_immutable_staging(self) -> None:
        import errno

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(root, _CountingRunner())
            output = root / "output"
            current_entry = session.current / "seed.txt"
            source_entry = root / "source" / "seed.txt"
            source_flags = source_entry.lstat().st_flags

            def copy_with_flags(source: Path, destination: Path, _ignores) -> None:
                shutil.copytree(source, destination, symlinks=True)

            try:
                os.chflags(
                    current_entry,
                    stat.UF_IMMUTABLE,
                    follow_symlinks=False,
                )
                current_flags = current_entry.lstat().st_flags
                publish_error = OSError(
                    errno.EIO,
                    "simulated atomic publish failure",
                )
                with patch(
                    "repomin.session._tree_digest",
                    return_value="immutable-test-fingerprint",
                ), patch(
                    "repomin.session._copy_repository",
                    side_effect=copy_with_flags,
                ), patch(
                    "repomin.session._rename_directory_no_replace",
                    side_effect=publish_error,
                ):
                    with self.assertRaises(OSError) as raised:
                        session.export(output)

                self.assertEqual(errno.EIO, raised.exception.errno)
                self.assertFalse(output.exists())
                self.assertEqual([], list(root.glob(".repomin-export-*")))
                self.assertEqual(current_flags, current_entry.lstat().st_flags)
                self.assertEqual(source_flags, source_entry.lstat().st_flags)
            finally:
                os.chflags(current_entry, 0, follow_symlinks=False)
                session.close()

    @unittest.skipUnless(
        hasattr(os, "chflags")
        and os.chflags in os.supports_follow_symlinks
        and hasattr(stat, "UF_IMMUTABLE"),
        "immutable file flags are unavailable",
    )
    def test_tool_owned_cleanup_clears_immutable_flags(self) -> None:
        from repomin import session as session_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / ".repomin-export-test"
            nested = staging / "nested"
            nested.mkdir(parents=True)
            payload = nested / "payload.txt"
            payload.write_text("payload\n", encoding="utf-8")

            try:
                os.chflags(payload, stat.UF_IMMUTABLE, follow_symlinks=False)
                os.chflags(nested, stat.UF_IMMUTABLE, follow_symlinks=False)
                os.chflags(staging, stat.UF_IMMUTABLE, follow_symlinks=False)
                session_module._remove_tool_owned_path_without_following(staging)
                self.assertFalse(staging.exists())
            finally:
                if staging.exists():
                    for entry in [staging, *staging.rglob("*")]:
                        os.chflags(entry, 0, follow_symlinks=False)
                    shutil.rmtree(staging)

    def test_tool_owned_cleanup_does_not_follow_symbolic_links(self) -> None:
        from repomin import session as session_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external"
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_text("outside\n", encoding="utf-8")
            external.chmod(0o555)
            external_mode = stat.S_IMODE(external.lstat().st_mode)
            staging = root / ".repomin-export-test"
            staging.mkdir()
            try:
                (staging / "external-link").symlink_to(
                    external,
                    target_is_directory=True,
                )
            except OSError as exc:
                external.chmod(0o755)
                self.skipTest("directory symbolic links are unavailable: %s" % exc)

            try:
                session_module._remove_tool_owned_path_without_following(staging)
                self.assertFalse(staging.exists())
                self.assertEqual("outside\n", sentinel.read_text(encoding="utf-8"))
                self.assertEqual(
                    external_mode,
                    stat.S_IMODE(external.lstat().st_mode),
                )
            finally:
                external.chmod(0o755)

    @unittest.skipUnless(
        hasattr(os, "chflags")
        and os.chflags in os.supports_follow_symlinks
        and hasattr(stat, "UF_IMMUTABLE"),
        "immutable symbolic-link flags are unavailable",
    )
    def test_tool_owned_cleanup_clears_flags_on_symbolic_link_itself(self) -> None:
        from repomin import session as session_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external.txt"
            external.write_text("outside\n", encoding="utf-8")
            staging = root / ".repomin-export-test"
            staging.mkdir()
            link = staging / "external-link"
            link.symlink_to(external)
            try:
                os.chflags(
                    link,
                    stat.UF_IMMUTABLE,
                    follow_symlinks=False,
                )
                session_module._remove_tool_owned_path_without_following(staging)
                self.assertFalse(staging.exists())
                self.assertEqual("outside\n", external.read_text(encoding="utf-8"))
                self.assertEqual(0, external.lstat().st_flags & stat.UF_IMMUTABLE)
            finally:
                if link.is_symlink():
                    os.chflags(link, 0, follow_symlinks=False)
                if staging.exists():
                    session_module._remove_tool_owned_path_without_following(
                        staging
                    )

    @unittest.skipIf(os.name == "nt", "POSIX unlink semantics required")
    def test_tool_owned_cleanup_does_not_chmod_external_hardlink(self) -> None:
        from repomin import session as session_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external.txt"
            external.write_text("outside\n", encoding="utf-8")
            staging = root / ".repomin-export-test"
            staging.mkdir()
            try:
                os.link(external, staging / "external-alias.txt")
            except OSError as exc:
                self.skipTest("hard links are unavailable: %s" % exc)
            external.chmod(0o444)
            external_mode = stat.S_IMODE(external.lstat().st_mode)

            try:
                session_module._remove_tool_owned_path_without_following(staging)
                self.assertEqual("outside\n", external.read_text(encoding="utf-8"))
                self.assertEqual(
                    external_mode,
                    stat.S_IMODE(external.lstat().st_mode),
                )
            finally:
                external.chmod(0o644)

    @unittest.skipUnless(
        hasattr(os, "chflags")
        and os.chflags in os.supports_follow_symlinks
        and hasattr(stat, "UF_IMMUTABLE"),
        "immutable file flags are unavailable",
    )
    def test_tool_owned_cleanup_does_not_clear_external_hardlink_flags(
        self,
    ) -> None:
        from repomin import session as session_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external.txt"
            external.write_text("outside\n", encoding="utf-8")
            staging = root / ".repomin-export-test"
            staging.mkdir()
            try:
                os.link(external, staging / "external-alias.txt")
            except OSError as exc:
                self.skipTest("hard links are unavailable: %s" % exc)

            try:
                os.chflags(external, stat.UF_IMMUTABLE, follow_symlinks=False)
                external_flags = external.lstat().st_flags
                with self.assertRaises(PermissionError):
                    session_module._remove_tool_owned_path_without_following(
                        staging
                    )
                with self.assertRaisesRegex(
                    SessionError,
                    "could not safely remove command working directory",
                ) as raised:
                    session_module._cleanup_tool_owned_paths(
                        [staging],
                        "command working directory",
                    )
                self.assertIsInstance(raised.exception.__cause__, PermissionError)
                self.assertEqual(external_flags, external.lstat().st_flags)
                self.assertEqual("outside\n", external.read_text(encoding="utf-8"))
            finally:
                os.chflags(external, 0, follow_symlinks=False)
                if staging.exists():
                    session_module._remove_tool_owned_path_without_following(
                        staging
                    )

    def test_export_reports_staging_cleanup_failure(self) -> None:
        import errno
        from repomin import session as session_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(root, _CountingRunner())
            output = root / "output"
            remove_staging = (
                session_module._remove_tool_owned_path_without_following
            )
            try:
                publish_error = OSError(errno.EIO, "simulated publish failure")
                with patch(
                    "repomin.session._rename_directory_no_replace",
                    side_effect=publish_error,
                ), patch(
                    "repomin.session._remove_tool_owned_path_without_following",
                    side_effect=PermissionError(
                        errno.EACCES,
                        "simulated staging cleanup failure",
                    ),
                ):
                    with self.assertRaises(SessionError) as raised:
                        session.export(output)

                self.assertIn(
                    "could not remove export staging directory",
                    str(raised.exception),
                )
                self.assertIn("simulated publish failure", str(raised.exception))
                self.assertIsInstance(raised.exception.__cause__, PermissionError)
                self.assertIs(
                    publish_error,
                    raised.exception.__cause__.__context__,
                )
                self.assertFalse(output.exists())
            finally:
                for staging in root.glob(".repomin-export-*"):
                    remove_staging(staging)
                session.close()

    def test_export_does_not_overwrite_a_race_created_output(self) -> None:
        from repomin import session as session_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(root, _CountingRunner())
            output = root / "output"
            publish = session_module._rename_directory_no_replace
            raced_output_inode = []

            def race_publish(staged: Path, destination: Path) -> None:
                destination.mkdir()
                raced_output_inode.append(destination.lstat().st_ino)
                publish(staged, destination)

            try:
                with patch(
                    "repomin.session._rename_directory_no_replace",
                    side_effect=race_publish,
                ):
                    with self.assertRaisesRegex(
                        FileExistsError, "output already exists"
                    ):
                        session.export(output)

                self.assertEqual(raced_output_inode, [output.lstat().st_ino])
                self.assertEqual([], list(output.iterdir()))
                self.assertEqual([], list(root.glob(".repomin-export-*")))
            finally:
                session.close()

    def test_certified_export_remains_verified_and_idempotent(self) -> None:
        passing = RunResult(1, "ORIGINAL_FAILURE", "", 0.01)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                holdout_runs=3,
                holdout_minimum_rate=0.5,
                holdout_confidence=0.5,
            )
            output = root / "output"
            try:
                session.record_final_validation(passing)
                certification = session.run_holdout_certification()
                session.export(output)

                self.assertEqual(
                    certification.artifact_fingerprint,
                    _tree_digest(output, set()),
                )
                with patch(
                    "repomin.session._rename_directory_no_replace",
                    side_effect=AssertionError("idempotent export republished"),
                ):
                    session.export(output)
                self.assertEqual([], list(root.glob(".repomin-export-*")))
            finally:
                session.close()

    def test_repeated_baselines_use_distinct_working_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _PathRecordingRunner()
            session = self._session(Path(directory), runner)
            try:
                session.verify_baseline(2)

                self.assertEqual(2, len(runner.paths))
                self.assertNotEqual(runner.paths[0], runner.paths[1])
                self.assertEqual("repository-0001", runner.paths[0].name)
                self.assertEqual("repository-0002", runner.paths[1].name)
                self.assertFalse((session.current / "command-output.txt").exists())
            finally:
                session.close()

    def test_holdout_runs_full_size_in_fresh_copies_without_polluting_stats(
        self,
    ) -> None:
        passing = RunResult(1, "ORIGINAL_FAILURE", "", 0.01)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = _FreshCopyRunner()
            session = self._session(
                root,
                runner,
                holdout_runs=3,
                holdout_minimum_rate=0.5,
                holdout_confidence=0.5,
            )
            try:
                self.assertTrue(
                    session.try_mutation(
                        "test", "accepted mutation", _write_candidate("accepted.txt")
                    )
                )
                runner.reset_observations()
                session.record_final_validation(passing)
                stats_before = deepcopy(session.stats)

                certification = session.run_holdout_certification()

                self.assertEqual("certified", certification.status)
                self.assertEqual(3, certification.completed_runs)
                self.assertEqual(3, certification.passes)
                self.assertEqual(3, len(runner.paths))
                self.assertEqual(3, len(set(runner.paths)))
                self.assertEqual([False, False, False], runner.saw_existing_marker)
                self.assertTrue(certification.fresh_repository_copy_per_run)
                self.assertFalse(certification.cache_used)
                self.assertFalse(certification.early_stopping)
                self.assertIsNotNone(certification.artifact_fingerprint)
                self.assertEqual(64, len(certification.artifact_fingerprint or ""))
                self.assertEqual(stats_before, session.stats)
                self.assertFalse((session.current / "sample-marker.txt").exists())

                output = root / "output"
                session.export(output)
                self.assertTrue((output / "accepted.txt").is_file())
                self.assertFalse((output / "sample-marker.txt").exists())
            finally:
                session.close()

    @unittest.skipIf(os.name == "nt", "POSIX root-mode semantics")
    def test_certified_checkpoint_rejects_payload_root_mode_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint, identity, _, _ = self._certified_holdout_checkpoint(root)
            current = checkpoint / "workspace" / "current"
            original_mode = current.lstat().st_mode & 0o7777
            current.chmod(0o700 if original_mode != 0o700 else 0o755)
            runner = _CountingRunner("ORIGINAL_FAILURE")

            with self.assertRaisesRegex(
                SessionError,
                "current state fingerprint changed",
            ):
                ReductionSession(
                    root / "source",
                    FailureOracle(runner, FailureSpec("ORIGINAL_FAILURE")),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                    holdout_runs=3,
                    holdout_minimum_rate=0.5,
                    holdout_confidence=0.5,
                )
            self.assertEqual(0, runner.calls)

    @unittest.skipIf(os.name == "nt", "POSIX mtime semantics")
    def test_certified_checkpoint_rejects_payload_mtime_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint, identity, _, _ = self._certified_holdout_checkpoint(root)
            entry = checkpoint / "workspace" / "current" / "seed.txt"
            original = entry.lstat()
            os.utime(
                entry,
                ns=(original.st_atime_ns, original.st_mtime_ns + 1),
            )
            runner = _CountingRunner("ORIGINAL_FAILURE")

            with self.assertRaisesRegex(
                SessionError,
                "current state fingerprint changed",
            ):
                ReductionSession(
                    root / "source",
                    FailureOracle(runner, FailureSpec("ORIGINAL_FAILURE")),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                    holdout_runs=3,
                    holdout_minimum_rate=0.5,
                    holdout_confidence=0.5,
                )
            self.assertEqual(0, runner.calls)

    def test_holdout_timeout_and_resource_exhaustion_are_hard_vetoes(self) -> None:
        passing = RunResult(1, "ORIGINAL_FAILURE", "", 0.01)
        failures = {
            "timeout": RunResult(
                124,
                "ORIGINAL_FAILURE",
                "",
                0.01,
                timed_out=True,
            ),
            "resource": RunResult(
                137,
                "ORIGINAL_FAILURE",
                "",
                0.01,
                resource_exhausted=True,
                resource_reason="memory",
            ),
        }
        for label, special in failures.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                runner = _SequenceRunner([passing, special, passing])
                session = self._session(
                    Path(directory),
                    runner,
                    holdout_runs=3,
                    holdout_minimum_rate=0.5,
                    holdout_confidence=0.5,
                )
                try:
                    session.record_final_validation(passing)
                    with self.assertRaises(HoldoutCertificationError):
                        session.run_holdout_certification()

                    certification = session.holdout_certification
                    self.assertEqual("not_certified", certification.status)
                    self.assertEqual(3, runner.calls)
                    self.assertEqual(3, certification.completed_runs)
                    self.assertEqual(2, certification.passes)
                    self.assertTrue(certification.exact_rate_gate_passed)
                    self.assertEqual(
                        int(label == "timeout"), certification.timed_out_runs
                    )
                    self.assertEqual(
                        int(label == "resource"),
                        certification.resource_exhausted_runs,
                    )
                    self.assertFalse(certification.early_stopping)
                finally:
                    session.close()

    def test_persistent_holdout_resumes_after_interrupt_without_reusing_slot(
        self,
    ) -> None:
        passing = RunResult(1, "ORIGINAL_FAILURE", "", 0.01)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {
                "command": "reproduce",
                "holdout_runs": 4,
                "min_holdout_rate": 0.2,
                "holdout_confidence": 0.5,
            }
            first_runner = _InterruptingRunner(interrupt_at=2)
            session = self._session(
                root,
                first_runner,
                session_path=checkpoint,
                identity=identity,
                holdout_runs=4,
                holdout_minimum_rate=0.2,
                holdout_confidence=0.5,
            )
            try:
                session.record_final_validation(passing)
                with self.assertRaises(KeyboardInterrupt):
                    session.run_holdout_certification()
            finally:
                session.close()

            state = json.loads((checkpoint / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(3, state["schema_version"])
            self.assertEqual(
                TREE_FINGERPRINT_POLICY,
                state["tree_fingerprint_policy"],
            )
            saved = state["holdout_certification"]
            self.assertEqual("running", saved["status"])
            self.assertEqual(2, saved["completed_runs"])
            self.assertIsNone(saved["in_flight_index"])
            self.assertEqual(
                ["passed", "interrupted"],
                [sample["outcome"] for sample in saved["samples"]],
            )

            resumed_runner = _CountingRunner("ORIGINAL_FAILURE")
            resumed = ReductionSession(
                root / "source",
                FailureOracle(resumed_runner, FailureSpec("ORIGINAL_FAILURE")),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
                holdout_runs=4,
                holdout_minimum_rate=0.2,
                holdout_confidence=0.5,
            )
            try:
                certification = resumed.run_holdout_certification()
                self.assertEqual("certified", certification.status)
                self.assertTrue(certification.resumed)
                self.assertEqual(2, resumed_runner.calls)
                self.assertEqual(4, certification.completed_runs)
                self.assertEqual(3, certification.passes)
                self.assertEqual(1, certification.interrupted_runs)
                self.assertEqual(
                    ["passed", "interrupted", "passed", "passed"],
                    [sample.outcome for sample in certification.samples],
                )
            finally:
                resumed.close()

    def test_persistent_holdout_rejects_tampered_certification_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint, identity, state_path, original = (
                self._certified_holdout_checkpoint(root)
            )

            def sample_outcome(state):
                state["holdout_certification"]["samples"][0]["outcome"] = "failed"

            def required_passes(state):
                state["holdout_certification"]["required_passes"] += 1

            def observed_rate(state):
                state["holdout_certification"]["observed_rate"] = 0.5

            def exact_lower_bound(state):
                state["holdout_certification"]["exact_lower_bound"] = 0.0

            def exact_p_value(state):
                state["holdout_certification"]["exact_p_value"] = 1.0

            def exact_rate_gate(state):
                state["holdout_certification"]["exact_rate_gate_passed"] = False

            def terminal_status(state):
                state["holdout_certification"]["status"] = "not_certified"

            def oracle_identity(state):
                state["holdout_certification"]["oracle_identity_sha256"] = "0" * 64

            def rejected_representative(state):
                state["holdout_certification"]["representative_run"]["stdout"] = (
                    "DIFFERENT_FAILURE"
                )

            def unlinked_representative(state):
                state["holdout_certification"]["representative_run"][
                    "duration_seconds"
                ] = 0.02

            def rejected_final_validation(state):
                state["final_validation_run"]["stdout"] = "DIFFERENT_FAILURE"

            def artifact_fingerprint(state):
                state["holdout_certification"]["artifact_fingerprint"] = "0" * 64

            def fresh_copy_policy(state):
                state["holdout_certification"][
                    "fresh_repository_copy_per_run"
                ] = False

            def cache_policy(state):
                state["holdout_certification"]["cache_used"] = True

            def early_stopping_policy(state):
                state["holdout_certification"]["early_stopping"] = True

            tamper_cases = (
                ("sample outcome", sample_outcome),
                ("required passes", required_passes),
                ("observed rate", observed_rate),
                ("exact lower bound", exact_lower_bound),
                ("exact p-value", exact_p_value),
                ("exact rate gate", exact_rate_gate),
                ("terminal status", terminal_status),
                ("oracle identity", oracle_identity),
                ("rejected representative", rejected_representative),
                ("unlinked representative", unlinked_representative),
                ("rejected final validation", rejected_final_validation),
                ("artifact fingerprint", artifact_fingerprint),
                ("fresh-copy policy", fresh_copy_policy),
                ("cache policy", cache_policy),
                ("early-stopping policy", early_stopping_policy),
            )
            for label, tamper in tamper_cases:
                with self.subTest(label=label):
                    state = deepcopy(original)
                    tamper(state)
                    state_path.write_text(
                        json.dumps(state, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    resumed = None
                    try:
                        with self.assertRaises(SessionError):
                            resumed = self._resume_holdout_checkpoint(
                                root,
                                checkpoint,
                                identity,
                            )
                    finally:
                        if resumed is not None:
                            resumed.close()

    def test_persistent_holdout_rejects_premature_plan_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {
                "command": "reproduce",
                "holdout_runs": 3,
                "min_holdout_rate": 0.5,
                "holdout_confidence": 0.5,
            }
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                session_path=checkpoint,
                identity=identity,
                holdout_runs=3,
                holdout_minimum_rate=0.5,
                holdout_confidence=0.5,
            )
            session.close()
            state_path = checkpoint / "state.json"
            original = json.loads(state_path.read_text(encoding="utf-8"))
            mutations = (
                ("attempt_id", "premature-attempt"),
                ("required_passes", 3),
                ("artifact_fingerprint", "0" * 64),
                ("oracle_identity_sha256", "0" * 64),
            )

            for field, value in mutations:
                with self.subTest(field=field):
                    state = deepcopy(original)
                    state["holdout_certification"][field] = value
                    state_path.write_text(
                        json.dumps(state, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    resumed = None
                    try:
                        with self.assertRaises(SessionError):
                            resumed = self._resume_holdout_checkpoint(
                                root,
                                checkpoint,
                                identity,
                            )
                    finally:
                        if resumed is not None:
                            resumed.close()

    def test_disabled_holdout_rejects_noncanonical_plan_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce"}
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                session_path=checkpoint,
                identity=identity,
            )
            session.close()
            state_path = checkpoint / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["holdout_certification"]["planned_runs"] = 1
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(SessionError):
                ReductionSession(
                    root / "source",
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                    ),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                )

    def test_persistent_holdout_burns_a_write_ahead_slot_after_hard_stop(
        self,
    ) -> None:
        passing = RunResult(1, "ORIGINAL_FAILURE", "", 0.01)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {
                "command": "reproduce",
                "holdout_runs": 3,
                "min_holdout_rate": 0.2,
                "holdout_confidence": 0.5,
            }
            first_runner = _HardStopRunner()
            session = self._session(
                root,
                first_runner,
                session_path=checkpoint,
                identity=identity,
                holdout_runs=3,
                holdout_minimum_rate=0.2,
                holdout_confidence=0.5,
            )
            session.record_final_validation(passing)
            with self.assertRaisesRegex(SystemExit, "simulated process termination"):
                session.run_holdout_certification()
            session.close()

            state = json.loads((checkpoint / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(1, state["holdout_certification"]["in_flight_index"])
            self.assertEqual(0, state["holdout_certification"]["completed_runs"])

            resumed_runner = _CountingRunner("ORIGINAL_FAILURE")
            resumed = ReductionSession(
                root / "source",
                FailureOracle(resumed_runner, FailureSpec("ORIGINAL_FAILURE")),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
                holdout_runs=3,
                holdout_minimum_rate=0.2,
                holdout_confidence=0.5,
            )
            try:
                certification = resumed.run_holdout_certification()
                self.assertEqual("certified", certification.status)
                self.assertEqual(2, resumed_runner.calls)
                self.assertEqual(3, certification.completed_runs)
                self.assertEqual(2, certification.passes)
                self.assertEqual(1, certification.interrupted_runs)
                self.assertEqual("interrupted", certification.samples[0].outcome)
            finally:
                resumed.close()

    def test_terminal_failed_holdout_is_not_rerun_after_resume(self) -> None:
        passing = RunResult(1, "ORIGINAL_FAILURE", "", 0.01)
        different = RunResult(1, "DIFFERENT_FAILURE", "", 0.01)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {
                "command": "reproduce",
                "holdout_runs": 3,
                "min_holdout_rate": 0.5,
                "holdout_confidence": 0.5,
            }
            first_runner = _SequenceRunner([passing, different, different])
            session = self._session(
                root,
                first_runner,
                session_path=checkpoint,
                identity=identity,
                holdout_runs=3,
                holdout_minimum_rate=0.5,
                holdout_confidence=0.5,
            )
            try:
                session.record_final_validation(passing)
                with self.assertRaises(HoldoutCertificationError):
                    session.run_holdout_certification()
                self.assertEqual(3, first_runner.calls)
                self.assertEqual("not_certified", session.holdout_certification.status)
            finally:
                session.close()

            resumed_runner = _CountingRunner("ORIGINAL_FAILURE")
            resumed = ReductionSession(
                root / "source",
                FailureOracle(resumed_runner, FailureSpec("ORIGINAL_FAILURE")),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
                holdout_runs=3,
                holdout_minimum_rate=0.5,
                holdout_confidence=0.5,
            )
            try:
                with self.assertRaises(HoldoutCertificationError):
                    resumed.run_holdout_certification()
                self.assertEqual(0, resumed_runner.calls)
                self.assertTrue(resumed.holdout_certification.resumed)
                self.assertEqual(
                    "not_certified", resumed.holdout_certification.status
                )
            finally:
                resumed.close()

    def test_certified_holdout_rejects_a_tampered_payload(self) -> None:
        passing = RunResult(1, "ORIGINAL_FAILURE", "", 0.01)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                holdout_runs=3,
                holdout_minimum_rate=0.5,
                holdout_confidence=0.5,
            )
            try:
                session.record_final_validation(passing)
                self.assertEqual(
                    "certified", session.run_holdout_certification().status
                )
                (session.current / "seed.txt").write_text(
                    "tampered\n", encoding="utf-8"
                )

                output = root / "output"
                with self.assertRaisesRegex(SessionError, "artifact changed"):
                    session.export(output)
                self.assertFalse(output.exists())
                self.assertEqual("aborted", session.holdout_certification.status)
            finally:
                session.close()

    def test_failed_mutation_cleans_partial_trial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = _CountingRunner()
            session = self._session(Path(directory), runner)

            def mutation(root: Path) -> bool:
                (root / "partial.txt").write_text("partial\n", encoding="utf-8")
                raise RuntimeError("mutation failed")

            try:
                with self.assertRaisesRegex(RuntimeError, "mutation failed"):
                    session.try_mutation("test", "broken", mutation)
                self.assertEqual([], list(session.root.glob("trial-*")))
                self.assertFalse((session.current / "partial.txt").exists())
            finally:
                session.close()

    def test_session_can_use_a_docker_shared_temporary_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "shared"
            shared.mkdir()
            runner = _CountingRunner()
            session = self._session(root, runner, temporary_parent=shared)
            session_path = session.root
            try:
                self.assertEqual(shared, session_path.parent)
                self.assertTrue(session_path.name.startswith(".repomin-session-"))
            finally:
                session.close()
            self.assertFalse(session_path.exists())

    def test_persistent_session_restores_accepted_state_and_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            checkpoint = root / "checkpoint"
            identity = {"command": "python reproduce.py", "match": "ORIGINAL_FAILURE"}
            runner = _CountingRunner("ORIGINAL_FAILURE")
            oracle = FailureOracle(runner, FailureSpec("ORIGINAL_FAILURE"))
            session = ReductionSession(
                source,
                oracle,
                ReductionStats(source_files=1, source_bytes=5),
                session_path=checkpoint,
                identity=identity,
            )
            session.verify_baseline(1)
            self.assertTrue(
                session.try_mutation(
                    "files", "keep accepted file", _write_candidate("accepted.txt")
                )
            )
            session.mark_phase_completed("files")
            session.close()

            state = json.loads((checkpoint / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(3, state["schema_version"])
            self.assertEqual(
                TREE_FINGERPRINT_POLICY,
                state["tree_fingerprint_policy"],
            )
            self.assertEqual(["files"], state["completed_phases"])
            self.assertEqual(1, state["stats"]["accepted"])
            self.assertEqual(1, state["stats"]["phase_stats"][0]["accepted"])
            self.assertIsNotNone(state["baseline"])

            resumed = ReductionSession(
                source,
                FailureOracle(runner, FailureSpec("ORIGINAL_FAILURE")),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=dict(
                    identity,
                    min_baseline_rate=None,
                    min_candidate_rate=None,
                    confidence=0.95,
                    java_analysis_classpath=[],
                ),
            )
            try:
                self.assertTrue(resumed.resumed)
                self.assertTrue((resumed.current / "accepted.txt").exists())
                self.assertEqual(1, resumed.stats.attempts)
                self.assertEqual(1, resumed.stats.accepted)
                self.assertEqual(1, resumed.stats.phase_stats["files"].accepted)
                self.assertEqual("files", resumed.current_phase)
                self.assertTrue(resumed.phase_completed("files"))
                self.assertIsNotNone(resumed.baseline)
            finally:
                resumed.close()

    def test_persistent_session_restores_and_validates_process_signature(self) -> None:
        class SignalRunner:
            def run(self, _cwd: Path) -> RunResult:
                return RunResult(-int(signal.SIGABRT), "", "", 0.01)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            checkpoint = root / "checkpoint"
            identity = {"command": "crash", "process_failure": True}
            spec = FailureSpec(None, process_failure=True)
            session = ReductionSession(
                source,
                FailureOracle(SignalRunner(), spec),
                ReductionStats(source_files=1, source_bytes=5),
                session_path=checkpoint,
                identity=identity,
            )
            try:
                session.verify_baseline(1)
            finally:
                session.close()

            with self.assertRaisesRegex(SessionError, "session configuration changed"):
                ReductionSession(
                    source,
                    FailureOracle(SignalRunner(), spec),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity={"command": "crash"},
                )

            resumed_oracle = FailureOracle(SignalRunner(), spec)
            resumed = ReductionSession(
                source,
                resumed_oracle,
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
            )
            try:
                self.assertEqual(
                    int(signal.SIGABRT),
                    resumed_oracle.process_failure_signature.code,
                )
                self.assertTrue(
                    resumed_oracle.accepts(
                        RunResult(-int(signal.SIGABRT), "", "", 0.01)
                    )
                )
                self.assertFalse(
                    resumed_oracle.accepts(
                        RunResult(-int(signal.SIGTERM), "", "", 0.01)
                    )
                )
            finally:
                resumed.close()

            state_path = checkpoint / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["oracle"]["process_failure_signature"]["code"] = 999
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OracleError, "invalid process failure signature"):
                ReductionSession(
                    source,
                    FailureOracle(SignalRunner(), spec),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                )

    def test_persistent_session_has_an_exclusive_lifetime_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce"}
            first = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                session_path=checkpoint,
                identity=identity,
            )
            try:
                with self.assertRaisesRegex(SessionError, "already in use"):
                    ReductionSession(
                        root / "source",
                        FailureOracle(
                            _CountingRunner("ORIGINAL_FAILURE"),
                            FailureSpec("ORIGINAL_FAILURE"),
                        ),
                        ReductionStats(source_files=0, source_bytes=0),
                        session_path=checkpoint,
                        resume=True,
                        identity=identity,
                    )
            finally:
                first.close()

            resumed = ReductionSession(
                root / "source",
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                ),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
            )
            resumed.close()

    def test_legacy_checkpoint_is_rejected_before_workspace_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce"}
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                session_path=checkpoint,
                identity=identity,
            )
            session.close()

            state_path = checkpoint / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 2
            state.pop("tree_fingerprint_policy")
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            current = checkpoint / "workspace" / "current"
            previous = checkpoint / "workspace" / "previous-000001"
            current.rename(previous)

            with self.assertRaisesRegex(
                SessionError,
                "incompatible tree fingerprint policy",
            ):
                ReductionSession(
                    root / "source",
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                    ),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                )
            self.assertFalse(current.exists())
            self.assertTrue(previous.is_dir())

    def test_checkpoint_requires_the_current_tree_fingerprint_policy(self) -> None:
        for saved_policy in (None, "tree-sha256-v1"):
            with self.subTest(
                saved_policy=saved_policy
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                checkpoint = root / "checkpoint"
                identity = {"command": "reproduce"}
                session = self._session(
                    root,
                    _CountingRunner("ORIGINAL_FAILURE"),
                    session_path=checkpoint,
                    identity=identity,
                )
                session.close()

                state_path = checkpoint / "state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if saved_policy is None:
                    state.pop("tree_fingerprint_policy")
                else:
                    state["tree_fingerprint_policy"] = saved_policy
                state_path.write_text(
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    SessionError,
                    "tree fingerprint policy is missing or incompatible",
                ):
                    ReductionSession(
                        root / "source",
                        FailureOracle(
                            _CountingRunner("ORIGINAL_FAILURE"),
                            FailureSpec("ORIGINAL_FAILURE"),
                        ),
                        ReductionStats(source_files=0, source_bytes=0),
                        session_path=checkpoint,
                        resume=True,
                        identity=identity,
                    )

    def test_persistent_session_restores_adaptive_sampling_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce", "candidate_runs": 5}
            session = ReductionSession(
                source,
                FailureOracle(
                    _CountingRunner("DIFFERENT_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                ),
                ReductionStats(source_files=1, source_bytes=5),
                session_path=checkpoint,
                identity=identity,
                candidate_runs=5,
                candidate_min_passes=5,
            )
            self.assertFalse(
                session.try_mutation(
                    "test", "adaptive rejection", _write_candidate("rejected.txt")
                )
            )
            session.close()

            resumed = ReductionSession(
                source,
                FailureOracle(
                    _CountingRunner("DIFFERENT_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                ),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
                candidate_runs=5,
                candidate_min_passes=5,
            )
            try:
                self.assertEqual(1, resumed.stats.candidate_early_rejections)
                self.assertEqual(0, resumed.stats.candidate_early_acceptances)
                self.assertEqual(4, resumed.stats.candidate_samples_saved)
                self.assertEqual(1, resumed.stats.candidate_samples)
            finally:
                resumed.close()

    def test_persistent_session_restores_baseline_exact_rate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce", "min_baseline_rate": 0.3}
            oracle = FailureOracle(
                _CountingRunner("ORIGINAL_FAILURE"),
                FailureSpec("ORIGINAL_FAILURE"),
                min_baseline_rate=0.3,
                confidence=0.8,
            )
            session = ReductionSession(
                source,
                oracle,
                ReductionStats(source_files=1, source_bytes=5),
                session_path=checkpoint,
                identity=identity,
            )
            try:
                session.verify_baseline(3, minimum_passes=1, minimum_rate=0.3)
            finally:
                session.close()

            state = json.loads((checkpoint / "state.json").read_text(encoding="utf-8"))
            for container in (state["stats"], state["oracle"]):
                self.assertEqual(3, container["baseline_rate_evidence_runs"])
                self.assertEqual(3, container["baseline_rate_evidence_passes"])
                self.assertTrue(container["baseline_exact_rate_gate_passed"])

            resumed_oracle = FailureOracle(
                _CountingRunner("ORIGINAL_FAILURE"),
                FailureSpec("ORIGINAL_FAILURE"),
                min_baseline_rate=0.3,
                confidence=0.8,
            )
            resumed = ReductionSession(
                source,
                resumed_oracle,
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
            )
            try:
                self.assertEqual(3, resumed.stats.baseline_rate_evidence_runs)
                self.assertEqual(3, resumed.stats.baseline_rate_evidence_passes)
                self.assertTrue(resumed.stats.baseline_exact_rate_gate_passed)
                self.assertEqual(
                    resumed_oracle.baseline_exact_p_value,
                    resumed.stats.baseline_exact_p_value,
                )
            finally:
                resumed.close()

    def test_persistent_session_reconstructs_legacy_baseline_rate_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce", "min_baseline_rate": 0.3}
            session = ReductionSession(
                source,
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                    min_baseline_rate=0.3,
                ),
                ReductionStats(source_files=1, source_bytes=5),
                session_path=checkpoint,
                identity=identity,
            )
            session.verify_baseline(3, minimum_passes=1, minimum_rate=0.3)
            session.close()

            state_path = checkpoint / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            for container in (state["stats"], state["oracle"]):
                for key in BASELINE_RATE_EVIDENCE_FIELDS:
                    container.pop(key)
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            resumed = ReductionSession(
                source,
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                    min_baseline_rate=0.3,
                ),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
            )
            try:
                self.assertEqual(3, resumed.stats.baseline_rate_evidence_runs)
                self.assertEqual(3, resumed.stats.baseline_rate_evidence_passes)
                self.assertIsNotNone(resumed.stats.baseline_exact_lower_bound)
                self.assertIsNotNone(resumed.stats.baseline_exact_p_value)
                self.assertTrue(resumed.stats.baseline_exact_rate_gate_passed)
            finally:
                resumed.close()

    def test_persistent_session_rejects_explicit_null_baseline_rate_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce", "min_baseline_rate": 0.3}
            session = ReductionSession(
                source,
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                    min_baseline_rate=0.3,
                ),
                ReductionStats(source_files=1, source_bytes=5),
                session_path=checkpoint,
                identity=identity,
            )
            session.verify_baseline(3, minimum_passes=1, minimum_rate=0.3)
            session.close()

            state_path = checkpoint / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            for container in (state["stats"], state["oracle"]):
                for key in BASELINE_RATE_EVIDENCE_FIELDS:
                    container[key] = None
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                OracleError,
                "incomplete baseline rate evidence",
            ):
                ReductionSession(
                    source,
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                        min_baseline_rate=0.3,
                    ),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                )

    def test_persistent_session_rejects_inconsistent_baseline_rate_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce", "min_baseline_rate": 0.3}
            session = ReductionSession(
                source,
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                    min_baseline_rate=0.3,
                    confidence=0.8,
                ),
                ReductionStats(source_files=1, source_bytes=5),
                session_path=checkpoint,
                identity=identity,
            )
            try:
                session.verify_baseline(3, minimum_passes=1, minimum_rate=0.3)
            finally:
                session.close()

            state_path = checkpoint / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["oracle"]["baseline_exact_p_value"] = 1.0
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                OracleError,
                "inconsistent baseline rate evidence",
            ):
                ReductionSession(
                    source,
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                        min_baseline_rate=0.3,
                        confidence=0.8,
                    ),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                )

    def test_persistent_session_restores_early_acceptance_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            confidence = 0.8
            minimum_rate = 0.4
            identity = {
                "command": "reproduce",
                "candidate_runs": 10,
                "min_candidate_rate": minimum_rate,
                "confidence": confidence,
            }
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                oracle_min_candidate_rate=minimum_rate,
                oracle_confidence=confidence,
                session_path=checkpoint,
                identity=identity,
                candidate_runs=10,
                candidate_min_passes=1,
                candidate_min_rate=minimum_rate,
            )
            self.assertTrue(
                session.try_mutation(
                    "test", "adaptive acceptance", _write_candidate("accepted.txt")
                )
            )
            session.close()

            state = json.loads((checkpoint / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(1, state["stats"]["candidate_early_acceptances"])
            self.assertEqual(4, state["stats"]["candidate_samples_saved"])
            self.assertEqual(6, state["stats"]["candidate_samples"])
            event = state["stats"]["events"][-1]
            self.assertTrue(event["oracle_early_acceptance"])
            expected_anytime = anytime_lower_bound(6, 6, confidence)
            self.assertAlmostEqual(
                expected_anytime, event["oracle_anytime_lower_bound"]
            )

            resumed = ReductionSession(
                root / "source",
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                    min_candidate_rate=minimum_rate,
                    confidence=confidence,
                ),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
                candidate_runs=10,
                candidate_min_passes=1,
                candidate_min_rate=minimum_rate,
            )
            try:
                self.assertEqual(1, resumed.stats.candidate_early_acceptances)
                self.assertEqual(4, resumed.stats.candidate_samples_saved)
                restored_event = resumed.stats.events[-1]
                self.assertTrue(restored_event.oracle_early_acceptance)
                self.assertAlmostEqual(
                    expected_anytime, restored_event.oracle_anytime_lower_bound
                )
            finally:
                resumed.close()

    def test_persistent_session_defaults_missing_early_acceptance_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce"}
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                session_path=checkpoint,
                identity=identity,
            )
            self.assertTrue(
                session.try_mutation(
                    "test", "legacy acceptance", _write_candidate("accepted.txt")
                )
            )
            session.close()

            state_path = checkpoint / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["stats"].pop("candidate_early_acceptances")
            state["stats"].pop("phase_stats")
            state["stats"].pop("phase_statistics_complete")
            for event in state["stats"]["events"]:
                event.pop("oracle_anytime_lower_bound")
                event.pop("oracle_early_acceptance")
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            resumed = ReductionSession(
                root / "source",
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                ),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
            )
            try:
                self.assertEqual(0, resumed.stats.candidate_early_acceptances)
                self.assertFalse(resumed.stats.phase_statistics_complete)
                self.assertEqual({}, resumed.stats.phase_stats)
                restored_event = resumed.stats.events[-1]
                self.assertFalse(restored_event.oracle_early_acceptance)
                self.assertIsNone(restored_event.oracle_anytime_lower_bound)
            finally:
                resumed.close()

    def test_persistent_session_rejects_invalid_semantic_timeout_provenance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            checkpoint = root / "checkpoint"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            identity = {
                "command": "reproduce",
                "semantic_reducer": "http",
                "semantic_model": "model-a",
                "semantic_endpoint": "http://localhost:8000/v1/chat/completions",
                "semantic_timeout": 60.0,
            }
            stats = ReductionStats(
                source_files=1,
                source_bytes=5,
                semantic_reducer="http",
                semantic_model="model-a",
                semantic_endpoint="http://localhost:8000/v1/chat/completions",
                semantic_timeout=60.0,
            )
            session = ReductionSession(
                source,
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"),
                    FailureSpec("ORIGINAL_FAILURE"),
                ),
                stats,
                session_path=checkpoint,
                identity=identity,
            )
            session.close()

            state_path = checkpoint / "state.json"
            original = json.loads(state_path.read_text(encoding="utf-8"))
            cases = (
                ("missing identity", "identity", None, "configuration changed"),
                (
                    "missing stats",
                    "stats",
                    None,
                    "inconsistent semantic reducer configuration",
                ),
                (
                    "changed stats",
                    "stats",
                    30.0,
                    "inconsistent semantic reducer configuration",
                ),
            )
            for name, section, value, error in cases:
                with self.subTest(name=name):
                    state = json.loads(json.dumps(original))
                    if value is None:
                        state[section].pop("semantic_timeout")
                    else:
                        state[section]["semantic_timeout"] = value
                    state_path.write_text(
                        json.dumps(state, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        SessionError,
                        error,
                    ):
                        ReductionSession(
                            source,
                            FailureOracle(
                                _CountingRunner("ORIGINAL_FAILURE"),
                                FailureSpec("ORIGINAL_FAILURE"),
                            ),
                            ReductionStats(source_files=0, source_bytes=0),
                            session_path=checkpoint,
                            resume=True,
                            identity=identity,
                        )

    def test_persistent_session_rejects_source_or_configuration_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            checkpoint = root / "checkpoint"
            identity = {"command": "python reproduce.py", "match": "ORIGINAL_FAILURE"}
            session = ReductionSession(
                source,
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"), FailureSpec("ORIGINAL_FAILURE")
                ),
                ReductionStats(source_files=1, source_bytes=5),
                session_path=checkpoint,
                identity=identity,
            )
            session.close()

            with self.assertRaisesRegex(SessionError, "configuration changed"):
                ReductionSession(
                    source,
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                    ),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity={"command": "different", "match": "ORIGINAL_FAILURE"},
                )

            with self.assertRaisesRegex(SessionError, "configuration changed"):
                ReductionSession(
                    source,
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                    ),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=dict(
                        identity,
                        java_analysis_classpath=[
                            {
                                "path": "/tmp/example.jar",
                                "kind": "file",
                                "fingerprint": "changed",
                            }
                        ],
                    ),
                )

            (checkpoint / "workspace" / "current" / "seed.txt").write_text(
                "tampered\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                SessionError, "current state fingerprint changed"
            ):
                ReductionSession(
                    source,
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                    ),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                )

            (checkpoint / "workspace" / "current" / "seed.txt").write_text(
                "seed\n", encoding="utf-8"
            )
            (source / "seed.txt").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(SessionError, "source fingerprint changed"):
                ReductionSession(
                    source,
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                    ),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                )

    def test_persistent_session_rejects_a_missing_reduction_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            identity = {
                "command": "reproduce",
                "reduction_strategy": "hierarchical-fixed-point-v2",
            }
            session = self._session(
                root,
                _CountingRunner("ORIGINAL_FAILURE"),
                session_path=checkpoint,
                identity=identity,
            )
            session.close()

            state_path = checkpoint / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["identity"].pop("reduction_strategy")
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SessionError, "configuration changed"):
                ReductionSession(
                    root / "source",
                    FailureOracle(
                        _CountingRunner("ORIGINAL_FAILURE"),
                        FailureSpec("ORIGINAL_FAILURE"),
                    ),
                    ReductionStats(source_files=0, source_bytes=0),
                    session_path=checkpoint,
                    resume=True,
                    identity=identity,
                )

    def test_persistent_session_recovers_an_interrupted_directory_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            checkpoint = root / "checkpoint"
            identity = {"command": "reproduce", "match": "ORIGINAL_FAILURE"}
            session = ReductionSession(
                source,
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"), FailureSpec("ORIGINAL_FAILURE")
                ),
                ReductionStats(source_files=1, source_bytes=5),
                session_path=checkpoint,
                identity=identity,
            )
            session.close()

            workspace = checkpoint / "workspace"
            current = workspace / "current"
            previous = workspace / "previous-000001"
            promoted = workspace / "promoted-000001"
            repeated = workspace / "repeat-000001-001"
            current.rename(previous)
            shutil.copytree(previous, promoted)
            shutil.copytree(previous, repeated)

            resumed = ReductionSession(
                source,
                FailureOracle(
                    _CountingRunner("ORIGINAL_FAILURE"), FailureSpec("ORIGINAL_FAILURE")
                ),
                ReductionStats(source_files=0, source_bytes=0),
                session_path=checkpoint,
                resume=True,
                identity=identity,
            )
            try:
                self.assertTrue((resumed.current / "seed.txt").is_file())
                self.assertFalse(previous.exists())
                self.assertFalse(promoted.exists())
                self.assertFalse(repeated.exists())
            finally:
                resumed.close()


if __name__ == "__main__":
    unittest.main()
