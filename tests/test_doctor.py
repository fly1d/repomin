"""Tests for the read-only ``repomin doctor`` preflight."""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from repomin.cli import main
from repomin.doctor import (
    _runner,
    doctor_oracle_mode,
    format_doctor,
    format_doctor_markdown,
    run_doctor,
)
from repomin.execution import RunResult
from repomin.input_paths import (
    normalize_ignore_path,
    normalize_text_file_path,
    validate_keep_paths,
    validate_text_file_paths,
)


_REPRODUCER = """\
from pathlib import Path
import sys

if not Path("required.txt").exists():
    print("DIFFERENT_FAILURE", file=sys.stderr)
    raise SystemExit(2)
print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(7)
"""


def _python_command(script: str) -> str:
    """Build a shell command for the platform-specific command runner."""
    argv = [sys.executable, script]
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


class DoctorTest(unittest.TestCase):
    def _source(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        source = directory / "project"
        source.mkdir()
        (source / "reproduce.py").write_text(_REPRODUCER, encoding="utf-8")
        (source / "required.txt").write_text("required\n", encoding="utf-8")
        (source / "pyproject.toml").write_text(
            "[project]\nname = 'doctor-fixture'\n", encoding="utf-8"
        )
        return source

    def test_static_doctor_detects_python_project_without_writing(self) -> None:
        source = self._source()
        before = sorted(path.relative_to(source).as_posix() for path in source.rglob("*"))
        ok, result = run_doctor(source)
        self.assertTrue(ok)
        self.assertTrue(result["adapters"]["python"]["detected"])
        self.assertEqual("not_run", result["baseline"]["status"])
        rendered = format_doctor(result)
        self.assertTrue(
            rendered.startswith(
                "ReproMin doctor: static checks passed; failure not verified\n"
            )
        )
        self.assertIn(
            "Next: add --command and a failure signal",
            rendered,
        )
        self.assertIn(
            "| `status` | `static_checks_passed` |",
            format_doctor_markdown(result),
        )
        after = sorted(path.relative_to(source).as_posix() for path in source.rglob("*"))
        self.assertEqual(before, after)

    def test_doctor_runs_baseline_in_fresh_copies(self) -> None:
        source = self._source()
        command = _python_command("reproduce.py")
        ok, result = run_doctor(
            source,
            command=command,
            match="ORIGINAL_FAILURE",
            exit_code=7,
            adapter="python",
            source_reducer="python",
            baseline_runs=2,
        )
        self.assertTrue(ok)
        self.assertEqual("pass", result["baseline"]["status"])
        self.assertEqual(2, result["baseline"]["runs"])
        self.assertEqual(2, result["baseline"]["passes"])
        self.assertEqual(2, result["baseline"]["minimum_passes"])
        self.assertEqual(0.95, result["baseline"]["confidence"])
        self.assertFalse((source / "doctor").exists())
        self.assertTrue(
            format_doctor(result).startswith("ReproMin doctor: ready to reduce\n")
        )
        self.assertIn(
            "Next: rerun the same failure options with `repomin`",
            format_doctor(result),
        )

    def test_doctor_rejects_empty_or_whitespace_command_without_running(self) -> None:
        source = self._source()

        for command in ("", " \t "):
            with self.subTest(command=repr(command)):
                with patch("repomin.doctor._runner") as runner:
                    ok, result = run_doctor(
                        source,
                        command=command,
                        match="ORIGINAL_FAILURE",
                    )

                self.assertFalse(ok)
                self.assertEqual("not_run", result["baseline"]["status"])
                self.assertTrue(
                    any(
                        check["name"] == "oracle"
                        and check["status"] == "fail"
                        and "command must not be empty or whitespace"
                        in check["message"]
                        for check in result["checks"]
                    )
                )
                runner.assert_not_called()

    def test_doctor_cli_rejects_empty_or_whitespace_command(self) -> None:
        source = self._source()

        for command in ("", " \t "):
            with self.subTest(command=repr(command)):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        main(
                            [
                                "doctor",
                                str(source),
                                "--command",
                                command,
                                "--match",
                                "ORIGINAL_FAILURE",
                            ]
                        )

                self.assertEqual(2, raised.exception.code)
                self.assertIn(
                    "command must not be empty or whitespace",
                    stderr.getvalue(),
                )

    def test_doctor_applies_flaky_baseline_count_and_rate_policy(self) -> None:
        source = self._source()

        class SequenceRunner:
            def __init__(self) -> None:
                self.outputs = iter(
                    [
                        "ORIGINAL_FAILURE",
                        "ORIGINAL_FAILURE",
                        "ORIGINAL_FAILURE",
                        "ORIGINAL_FAILURE",
                        "DIFFERENT_FAILURE",
                    ]
                )

            def run(self, _cwd: Path) -> RunResult:
                return RunResult(1, next(self.outputs), "", 0.01)

        with patch("repomin.doctor._runner", return_value=SequenceRunner()):
            ok, result = run_doctor(
                source,
                command="reproduce",
                match="ORIGINAL_FAILURE",
                baseline_runs=5,
                min_baseline_passes=3,
                min_baseline_rate=0.3,
                confidence=0.95,
            )

        self.assertTrue(ok)
        baseline = result["baseline"]
        self.assertEqual("pass", baseline["status"])
        self.assertEqual(4, baseline["passes"])
        self.assertEqual(3, baseline["minimum_passes"])
        self.assertEqual(0.3, baseline["minimum_rate"])
        self.assertEqual(5, baseline["rate_evidence_runs"])
        self.assertEqual(4, baseline["rate_evidence_passes"])
        self.assertTrue(baseline["exact_rate_gate_passed"])

    def test_doctor_forwards_all_docker_resource_limits(self) -> None:
        with patch("repomin.doctor.DockerRunner") as docker_runner:
            instance = docker_runner.return_value

            actual = _runner(
                "reproduce",
                30,
                "docker",
                "example:test",
                "none",
                1.5,
                32 * 1024 * 1024,
                64,
                16 * 1024 * 1024,
                128 * 1024 * 1024,
                {"MODE": "test"},
                True,
            )

        self.assertIs(instance, actual)
        docker_runner.assert_called_once_with(
            "reproduce",
            30,
            image="example:test",
            network="none",
            environment={"MODE": "test"},
            collect_java_diagnostics=True,
            cpus=1.5,
            memory_bytes=32 * 1024 * 1024,
            pids_limit=64,
            tmpfs_bytes=16 * 1024 * 1024,
            workspace_limit_bytes=128 * 1024 * 1024,
        )
        instance.validate.assert_called_once_with()

    def test_doctor_uses_effective_ignores_for_size_and_detection(self) -> None:
        source = self._source()
        ignored = source / "generated"
        ignored.mkdir()
        (ignored / "Cargo.toml").write_text(
            "[package]\nname = 'ignored'\n", encoding="utf-8"
        )
        ok, result = run_doctor(
            source,
            adapter="cargo",
            ignore_names=("generated",),
        )
        self.assertFalse(ok)
        self.assertFalse(result["adapters"]["cargo"]["detected"])
        self.assertEqual(3, result["source_files"])
        self.assertIn("generated", result["ignored_names"])

    def test_doctor_applies_root_gitignore_before_detection(self) -> None:
        source = self._source()
        generated = source / "generated"
        generated.mkdir()
        (generated / "package.json").write_text(
            '{"name": "ignored"}\n', encoding="utf-8"
        )
        (source / ".gitignore").write_text("/generated/\n", encoding="utf-8")

        ok, result = run_doctor(source, gitignore=True)

        self.assertTrue(ok)
        self.assertFalse(result["adapters"]["node"]["detected"])
        self.assertEqual([".gitignore"], result["gitignore_files"])
        self.assertRegex(result["gitignore_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(result["gitignore_recursive"])
        self.assertEqual(4, result["source_files"])

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                ["doctor", str(source), "--gitignore", "--format", "markdown"]
            )
        self.assertEqual(0, exit_code)
        self.assertIn("| `gitignore_files` | `1` |", stdout.getvalue())
        self.assertIn("| `gitignore` | `pass` |", stdout.getvalue())

    def test_doctor_keeps_same_named_file_for_directory_only_rule(self) -> None:
        source = self._source()
        (source / "generated").write_text("ordinary file\n", encoding="utf-8")
        (source / ".gitignore").write_text("generated/\n", encoding="utf-8")

        ok, result = run_doctor(source, gitignore=True)

        self.assertTrue(ok)
        self.assertEqual(5, result["source_files"])

    def test_doctor_applies_nested_gitignore_to_baseline_and_reports_rules(self) -> None:
        source = self._source()
        services = source / "services"
        private = services / "private"
        private.mkdir(parents=True)
        (source / ".gitignore").write_text("\n", encoding="utf-8")
        (services / ".gitignore").write_text("/private/\n", encoding="utf-8")
        (private / "package.json").write_text(
            '{"name": "ignored"}\n', encoding="utf-8"
        )
        command = _python_command("reproduce.py")

        ok, result = run_doctor(
            source,
            command=command,
            match="ORIGINAL_FAILURE",
            exit_code=7,
            gitignore=True,
            gitignore_recursive=True,
        )

        self.assertTrue(ok)
        self.assertEqual("pass", result["baseline"]["status"])
        self.assertEqual([".gitignore", "services/.gitignore"], result["gitignore_files"])
        self.assertTrue(result["gitignore_recursive"])
        self.assertRegex(result["gitignore_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("services/private/package.json", result["adapters"]["node"]["files"])

    def test_cli_accepts_doctor_gitignore_options(self) -> None:
        source = self._source()
        custom = source / "custom.ignore"
        custom.write_text("generated/\n", encoding="utf-8")
        generated = source / "generated"
        generated.mkdir()
        (generated / "package.json").write_text("{}\n", encoding="utf-8")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(
                [
                    "doctor",
                    str(source),
                    "--gitignore-file",
                    "custom.ignore",
                    "--json",
                ]
            )

        self.assertEqual(0, exit_code)
        result = json.loads(output.getvalue())
        self.assertEqual(["custom.ignore"], result["gitignore_files"])
        self.assertFalse(result["adapters"]["node"]["detected"])

    def test_doctor_skips_unconfigured_text_targets(self) -> None:
        source = self._source()

        ok, result = run_doctor(source)

        self.assertTrue(ok)
        self.assertEqual([], result["keep_paths"])
        self.assertEqual([], result["text_files"])
        text_check = next(
            check for check in result["checks"] if check["name"] == "text-targets"
        )
        self.assertEqual("skip", text_check["status"])
        self.assertIn("[SKIP] text-targets:", format_doctor(result))

    def test_cli_normalizes_and_checks_keep_and_text_paths(self) -> None:
        source = self._source()
        (source / "alpha.txt").write_text("alpha\n", encoding="utf-8")
        (source / "zeta.txt").write_text("zeta\n", encoding="utf-8")
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "doctor",
                    str(source),
                    "--keep",
                    "zeta.txt",
                    "--keep",
                    "alpha.txt",
                    "--keep",
                    "zeta.txt",
                    "--text-file",
                    "zeta.txt",
                    "--text-file",
                    "alpha.txt",
                    "--text-file",
                    "zeta.txt",
                    "--json",
                ]
            )

        self.assertEqual(0, exit_code)
        result = json.loads(stdout.getvalue())
        self.assertEqual(["alpha.txt", "zeta.txt"], result["keep_paths"])
        self.assertEqual(["alpha.txt", "zeta.txt"], result["text_files"])
        checks = {check["name"]: check for check in result["checks"]}
        self.assertEqual("pass", checks["keep-paths"]["status"])
        self.assertEqual("pass", checks["text-targets"]["status"])
        self.assertIn("[PASS] text-targets:", format_doctor(result))

    def test_doctor_keep_overrides_ignore_for_scan_detection_and_baseline(self) -> None:
        source = self._source()
        generated = source / "generated"
        generated.mkdir()
        (generated / "failure.txt").write_text("required\n", encoding="utf-8")
        (generated / "kept.py").write_text("value = 1\n", encoding="utf-8")
        (generated / "package.json").write_text(
            '{"name": "kept-node-project"}\n', encoding="utf-8"
        )
        (source / "reproduce.py").write_text(
            """\
from pathlib import Path
import sys

if Path("generated/failure.txt").read_text(encoding="utf-8") != "required\\n":
    raise SystemExit(2)
print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(7)
""",
            encoding="utf-8",
        )

        ok, result = run_doctor(
            source,
            command=_python_command("reproduce.py"),
            match="ORIGINAL_FAILURE",
            exit_code=7,
            ignore_names=("generated",),
            keep_paths=("generated",),
            text_files=("generated/failure.txt",),
        )

        self.assertTrue(ok)
        self.assertEqual(["generated"], result["keep_paths"])
        self.assertEqual(["generated/failure.txt"], result["text_files"])
        self.assertEqual(6, result["source_files"])
        self.assertTrue(result["adapters"]["node"]["detected"])
        self.assertIn("generated/package.json", result["adapters"]["node"]["files"])
        self.assertIn(
            "generated/kept.py", result["source_reducers"]["python"]["files"]
        )
        checks = {check["name"]: check for check in result["checks"]}
        self.assertEqual("pass", checks["text-targets"]["status"])
        self.assertEqual("pass", result["baseline"]["status"])

    def test_cli_reports_invalid_text_targets_without_running_baseline(self) -> None:
        cases = (
            ("missing", "missing.txt", "does not exist"),
            ("directory", "selected", "must be a regular file"),
            ("non-utf8", "selected.txt", "is not UTF-8"),
            (
                "default-ignore",
                "build/selected.txt",
                "excluded by the effective ignore rules",
            ),
        )
        for kind, text_file, expected in cases:
            with self.subTest(kind=kind):
                source = self._source()
                selected = source / text_file
                if kind == "directory":
                    selected.mkdir()
                elif kind == "non-utf8":
                    selected.write_bytes(b"\xff\xfe")
                elif kind != "missing":
                    selected.parent.mkdir(parents=True, exist_ok=True)
                    selected.write_text("selected\n", encoding="utf-8")
                stdout = io.StringIO()

                with patch("repomin.doctor._runner") as runner:
                    with contextlib.redirect_stdout(stdout):
                        exit_code = main(
                            [
                                "doctor",
                                str(source),
                                "--command",
                                _python_command("reproduce.py"),
                                "--match",
                                "ORIGINAL_FAILURE",
                                "--text-file",
                                text_file,
                                "--json",
                            ]
                        )

                self.assertEqual(1, exit_code)
                result = json.loads(stdout.getvalue())
                text_check = next(
                    check
                    for check in result["checks"]
                    if check["name"] == "text-targets"
                )
                self.assertEqual("fail", text_check["status"])
                self.assertIn(expected, text_check["message"])
                self.assertEqual("not_run", result["baseline"]["status"])
                runner.assert_not_called()

    def test_cli_reports_invalid_keep_without_running_baseline(self) -> None:
        source = self._source()
        stdout = io.StringIO()

        with patch("repomin.doctor._runner") as runner:
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "doctor",
                        str(source),
                        "--command",
                        _python_command("reproduce.py"),
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--keep",
                        "missing.txt",
                        "--json",
                    ]
                )

        self.assertEqual(1, exit_code)
        result = json.loads(stdout.getvalue())
        keep_check = next(
            check for check in result["checks"] if check["name"] == "keep-paths"
        )
        self.assertEqual("fail", keep_check["status"])
        self.assertIn("does not exist", keep_check["message"])
        self.assertEqual("not_run", result["baseline"]["status"])
        runner.assert_not_called()

    def test_cli_rejects_invalid_doctor_selection_path_syntax(self) -> None:
        source = self._source()
        for option in ("--keep", "--text-file"):
            for value in ("../outside", "C:/outside.txt", "C:outside.txt"):
                with self.subTest(option=option, value=value):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as raised:
                            main(["doctor", str(source), option, value])

                    self.assertEqual(2, raised.exception.code)
                    self.assertIn(
                        "exact relative path without glob syntax", stderr.getvalue()
                    )

    def test_selection_path_normalizers_reject_windows_drive_paths(self) -> None:
        normalizers = (
            ("keep", normalize_ignore_path),
            ("text", normalize_text_file_path),
        )
        for name, normalize in normalizers:
            for value in ("C:/outside.txt", "C:outside.txt"):
                with self.subTest(normalizer=name, value=value):
                    with self.assertRaisesRegex(ValueError, "exact relative path"):
                        normalize(value)

            with self.subTest(normalizer=name, value="nested/name:variant.txt"):
                self.assertEqual(
                    "nested/name:variant.txt",
                    normalize("nested/name:variant.txt"),
                )

    def test_run_doctor_rejects_selection_paths_outside_the_repository(self) -> None:
        source = self._source()
        outside = source.parent / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")

        for keyword, check_name in (
            ("keep_paths", "keep-paths"),
            ("text_files", "text-targets"),
        ):
            with self.subTest(keyword=keyword):
                ok, result = run_doctor(source, **{keyword: ("../outside.txt",)})

                self.assertFalse(ok)
                check = next(
                    item for item in result["checks"] if item["name"] == check_name
                )
                self.assertEqual("fail", check["status"])
                self.assertIn("exact relative path", check["message"])
                self.assertEqual([], result[keyword])

    @unittest.skipIf(os.name == "nt", "symlink creation is not generally available")
    def test_doctor_does_not_open_text_targets_through_unsafe_symlinks(self) -> None:
        source = self._source()
        outside = source.parent / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
        (source / "linked").symlink_to(outside, target_is_directory=True)

        with patch.object(Path, "open", side_effect=AssertionError("unexpected read")) as opened:
            ok, result = run_doctor(source, text_files=("linked/secret.txt",))

        self.assertFalse(ok)
        opened.assert_not_called()
        checks = {check["name"]: check for check in result["checks"]}
        self.assertEqual("fail", checks["source"]["status"])
        self.assertIn("symbolic link", checks["source"]["message"])
        self.assertEqual("fail", checks["text-targets"]["status"])

    @unittest.skipIf(os.name == "nt", "symlink creation is not generally available")
    def test_selection_validators_reject_intermediate_symbolic_links(self) -> None:
        source = self._source()
        outside = source.parent / "outside"
        outside.mkdir()
        (outside / "selected.txt").write_text("secret\n", encoding="utf-8")
        alias = source / "alias"
        alias.symlink_to(outside, target_is_directory=True)

        validators = (
            ("keep", lambda: validate_keep_paths(source, ("alias/selected.txt",))),
            (
                "text",
                lambda: validate_text_file_paths(source, ("alias/selected.txt",)),
            ),
        )
        for name, validate in validators:
            with self.subTest(validator=name):
                with self.assertRaisesRegex(ValueError, "symbolic link") as raised:
                    validate()
                self.assertIn("alias", str(raised.exception))

    def test_selection_validators_reject_intermediate_reparse_points(self) -> None:
        source = self._source()
        alias = source / "alias"
        alias.mkdir()
        (alias / "selected.txt").write_text("selected\n", encoding="utf-8")
        original_lstat = Path.lstat

        def lstat_with_reparse(path):
            status = original_lstat(path)
            if path == alias:
                return SimpleNamespace(
                    st_mode=status.st_mode,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return status

        validators = (
            ("keep", lambda: validate_keep_paths(source, ("alias/selected.txt",))),
            (
                "text",
                lambda: validate_text_file_paths(source, ("alias/selected.txt",)),
            ),
        )
        with patch.object(Path, "lstat", lstat_with_reparse):
            for name, validate in validators:
                with self.subTest(validator=name):
                    with self.assertRaisesRegex(ValueError, "reparse point") as raised:
                        validate()
                    self.assertIn("alias", str(raised.exception))

    def test_doctor_detection_matches_adapter_and_source_patterns(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name) / "project"
        source.mkdir()
        (source / "custom.gradle").write_text("dependencies {}\n", encoding="utf-8")
        requirements = source / "requirements"
        requirements.mkdir()
        (requirements / "base.txt").write_text("required==1\n", encoding="utf-8")
        (source / "requirements.in").write_text("ignored==1\n", encoding="utf-8")
        (source / "Gemfile.lock").write_text("GEM\n", encoding="utf-8")
        (source / "UPPER.PY").write_text("pass\n", encoding="utf-8")

        ok, result = run_doctor(source)

        self.assertTrue(ok)
        self.assertTrue(result["adapters"]["gradle"]["detected"])
        self.assertTrue(result["adapters"]["python"]["detected"])
        self.assertFalse(result["adapters"]["ruby"]["detected"])
        self.assertFalse(result["source_reducers"]["python"]["detected"])

    def test_doctor_baseline_preserves_the_output_basename(self) -> None:
        source = self._source()
        (source / "reproduce.py").write_text(
            """\
from pathlib import Path
import sys

if Path.cwd().name != "stable-output-name":
    raise SystemExit(2)
print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(7)
""",
            encoding="utf-8",
        )
        command = _python_command("reproduce.py")
        ok, result = run_doctor(
            source,
            command=command,
            match="ORIGINAL_FAILURE",
            exit_code=7,
            output=str(source.parent / "stable-output-name"),
        )
        self.assertTrue(ok)
        self.assertEqual("pass", result["baseline"]["status"])

    def test_doctor_rejects_existing_output_and_sidecar(self) -> None:
        source = self._source()
        output = source.parent / "result"
        output.mkdir()
        ok, result = run_doctor(source, output=str(output))
        self.assertFalse(ok)
        self.assertTrue(
            any(
                check["name"] == "output"
                and check["status"] == "fail"
                and "already exists" in check["message"]
                for check in result["checks"]
            )
        )

        output.rmdir()
        metadata = output.with_name(output.name + ".repomin")
        metadata.mkdir()
        ok, result = run_doctor(source, output=str(output))
        self.assertFalse(ok)
        self.assertTrue(
            any(
                check["name"] == "output"
                and "metadata output already exists" in check["message"]
                for check in result["checks"]
            )
        )

    @unittest.skipIf(os.name == "nt", "symlink creation is not generally available")
    def test_doctor_rejects_a_dangling_output_symlink(self) -> None:
        source = self._source()
        output = source.parent / "result"
        output.symlink_to(source.parent / "missing-target", target_is_directory=True)

        ok, result = run_doctor(source, output=str(output))

        self.assertFalse(ok)
        self.assertTrue(
            any(
                check["name"] == "output"
                and check["status"] == "fail"
                and "symbolic link" in check["message"]
                for check in result["checks"]
            )
        )

    def test_doctor_rejects_invalid_baseline_and_host_docker_options(self) -> None:
        source = self._source()
        ok, result = run_doctor(
            source,
            baseline_runs=0,
            timeout=math.inf,
            docker_image="example:local",
            docker_network="bridge",
            docker_cpus=1.0,
            docker_memory=32 * 1024 * 1024,
            docker_pids_limit=64,
            docker_tmpfs_size=16 * 1024 * 1024,
            docker_workspace_limit=128 * 1024 * 1024,
        )
        self.assertFalse(ok)
        failures = [
            check["message"]
            for check in result["checks"]
            if check["status"] == "fail"
        ]
        self.assertTrue(any("baseline runs" in message for message in failures))
        self.assertTrue(any("finite number" in message for message in failures))
        self.assertTrue(any("requires --backend docker" in message for message in failures))

    def test_doctor_rejects_invalid_docker_limits_without_running(self) -> None:
        source = self._source()
        invalid = (
            ({"docker_cpus": True}, "CPU"),
            ({"docker_cpus": math.inf}, "CPU"),
            ({"docker_memory": 1024}, "memory"),
            ({"docker_memory": 8.0 * 1024 * 1024}, "memory"),
            ({"docker_pids_limit": False}, "PID"),
            ({"docker_tmpfs_size": 0}, "tmpfs"),
            ({"docker_workspace_limit": "1GiB"}, "workspace"),
        )

        for options, expected in invalid:
            with self.subTest(options=options):
                with patch("repomin.doctor._runner") as runner:
                    ok, result = run_doctor(
                        source,
                        backend="docker",
                        docker_image="example:local",
                        **options,
                    )

                self.assertFalse(ok)
                self.assertEqual("not_run", result["baseline"]["status"])
                self.assertTrue(
                    any(
                        check["name"] == "backend"
                        and check["status"] == "fail"
                        and expected in check["message"]
                        for check in result["checks"]
                    )
                )
                self.assertFalse(
                    any(
                        check["name"] == "backend"
                        and check["status"] == "pass"
                        for check in result["checks"]
                    )
                )
                runner.assert_not_called()

    def test_doctor_rejects_oversized_numeric_values_without_raising(self) -> None:
        source = self._source()
        enormous = 10**10000

        for options, expected in (
            ({"min_baseline_rate": enormous}, "minimum baseline rate"),
            ({"confidence": enormous}, "confidence"),
            ({"timeout": enormous}, "timeout"),
            (
                {
                    "backend": "docker",
                    "docker_image": "example:local",
                    "docker_cpus": enormous,
                },
                "CPU",
            ),
        ):
            with self.subTest(options=options):
                ok, result = run_doctor(source, **options)
                self.assertFalse(ok)
                self.assertTrue(
                    any(
                        check["status"] == "fail"
                        and expected in check["message"]
                        for check in result["checks"]
                    )
                )

    def test_doctor_rejects_unattainable_baseline_rate_without_running(self) -> None:
        source = self._source()

        with patch("repomin.doctor._runner") as runner:
            ok, result = run_doctor(
                source,
                command="reproduce",
                match="ORIGINAL_FAILURE",
                baseline_runs=1,
                min_baseline_rate=0.5,
                confidence=0.95,
            )

        self.assertFalse(ok)
        self.assertEqual("not_run", result["baseline"]["status"])
        self.assertTrue(
            any(
                check["name"] == "baseline"
                and "unattainable" in check["message"]
                for check in result["checks"]
            )
        )
        runner.assert_not_called()

    def test_doctor_reports_bad_oracle_and_output_without_running(self) -> None:
        source = self._source()
        ok, result = run_doctor(
            source,
            command="false",
            output=str(source / "inside"),
            adapter="cargo",
            text_files=("missing.txt",),
        )
        self.assertFalse(ok)
        failures = {
            check["name"]: check["message"]
            for check in result["checks"]
            if check["status"] == "fail"
        }
        self.assertIn("output", failures)
        self.assertIn("adapter", failures)
        self.assertIn("oracle", failures)
        self.assertIn("text-targets", failures)
        self.assertEqual("not_run", result["baseline"]["status"])

    def test_cli_emits_json_doctor_result(self) -> None:
        source = self._source()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["doctor", str(source), "--json"])
        self.assertEqual(0, exit_code)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(str(source.resolve()), result["source"])

    def test_doctor_output_formats_preserve_text_and_json_behavior(self) -> None:
        source = self._source()

        default_output = io.StringIO()
        with contextlib.redirect_stdout(default_output):
            default_code = main(["doctor", str(source)])
        explicit_text = io.StringIO()
        with contextlib.redirect_stdout(explicit_text):
            text_code = main(["doctor", str(source), "--format", "text"])
        json_alias = io.StringIO()
        with contextlib.redirect_stdout(json_alias):
            alias_code = main(["doctor", str(source), "--json"])
        explicit_json = io.StringIO()
        with contextlib.redirect_stdout(explicit_json):
            json_code = main(["doctor", str(source), "--format=json"])

        self.assertEqual(0, default_code)
        self.assertEqual(0, text_code)
        self.assertEqual(default_output.getvalue(), explicit_text.getvalue())
        self.assertEqual(0, alias_code)
        self.assertEqual(0, json_code)
        self.assertEqual(json_alias.getvalue(), explicit_json.getvalue())

    def test_doctor_rejects_multiple_output_selectors(self) -> None:
        source = self._source()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "doctor",
                        str(source),
                        "--json",
                        "--format",
                        "markdown",
                    ]
                )
        self.assertEqual(2, raised.exception.code)
        self.assertIn("not allowed with argument --json", stderr.getvalue())

    def test_doctor_markdown_is_deterministic_and_reports_aggregate_evidence(
        self,
    ) -> None:
        source = self._source()

        class PassingRunner:
            def run(self, _cwd: Path) -> RunResult:
                return RunResult(7, "ORIGINAL_FAILURE", "", 0.01)

        with patch("repomin.doctor._runner", return_value=PassingRunner()):
            ok, result = run_doctor(
                source,
                command="PRIVATE_COMMAND",
                match="ORIGINAL_FAILURE",
                exit_code=7,
                adapter="python",
                source_reducer="python",
                baseline_runs=2,
                min_baseline_passes=2,
            )
        self.assertTrue(ok)
        options = {
            "requested_adapter": "python",
            "requested_source_reducer": "python",
            "backend": "host",
            "oracle_mode": doctor_oracle_mode(
                command="PRIVATE_COMMAND",
                match="ORIGINAL_FAILURE",
                exit_code=7,
                java_exception=False,
                python_exception=False,
                process_failure=False,
            ),
        }
        first = format_doctor_markdown(result, **options)
        second = format_doctor_markdown(result, **options)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("# ReproMin Doctor summary\n"))
        self.assertTrue(first.endswith("\n"))
        self.assertIn("| `status` | `ready_to_reduce` |", first)
        self.assertIn("| `oracle_mode` | `match_and_exit_code` |", first)
        self.assertIn("| `source_files` | `3` |", first)
        self.assertIn("| `baseline_runs` | `2` |", first)
        self.assertIn("| `baseline_passes` | `2` |", first)
        self.assertIn("| `baseline` | `pass` |", first)
        self.assertNotIn("PRIVATE_COMMAND", first)
        self.assertNotIn(str(source), first)

    def test_doctor_oracle_mode_covers_each_private_contract_shape(self) -> None:
        defaults = {
            "command": "private command",
            "match": None,
            "exit_code": None,
            "java_exception": False,
            "python_exception": False,
            "process_failure": False,
        }
        cases = (
            ({"command": None}, "not_requested"),
            ({"match": "private match"}, "match"),
            ({"exit_code": 7}, "exit_code"),
            ({"match": "private match", "exit_code": 7}, "match_and_exit_code"),
            ({"match": "private match", "java_exception": True}, "java_exception"),
            (
                {"match": "private match", "python_exception": True},
                "python_exception",
            ),
            ({"process_failure": True}, "process_failure"),
            ({}, "invalid"),
            ({"process_failure": True, "exit_code": 7}, "invalid"),
        )
        for updates, expected in cases:
            with self.subTest(expected=expected, updates=updates):
                options = dict(defaults)
                options.update(updates)
                self.assertEqual(expected, doctor_oracle_mode(**options))

    def test_doctor_markdown_exposes_signature_rate_evidence_denominator(
        self,
    ) -> None:
        source = self._source()

        class FailingProcessRunner:
            def run(self, _cwd: Path) -> RunResult:
                return RunResult(7, "PRIVATE_PROCESS_OUTPUT", "", 0.01)

        with patch("repomin.doctor._runner", return_value=FailingProcessRunner()):
            ok, result = run_doctor(
                source,
                command="PRIVATE_COMMAND",
                process_failure=True,
                baseline_runs=3,
                min_baseline_rate=0.01,
            )
        rendered = format_doctor_markdown(
            result,
            requested_adapter="auto",
            requested_source_reducer="auto",
            backend="host",
            oracle_mode="process_failure",
            gitignore_requested=False,
        )

        self.assertTrue(ok)
        self.assertEqual(3, result["baseline"]["runs"])
        self.assertEqual(2, result["baseline"]["rate_evidence_runs"])
        self.assertEqual(2, result["baseline"]["rate_evidence_passes"])
        self.assertIn("| `baseline_runs` | `3` |", rendered)
        self.assertIn("| `baseline_rate_evidence_runs` | `2` |", rendered)
        self.assertIn("| `baseline_rate_evidence_passes` | `2` |", rendered)
        self.assertIn("| `baseline_exact_rate_gate_passed` | `true` |", rendered)
        self.assertNotIn("PRIVATE", rendered)

    def test_doctor_markdown_ignores_private_and_malicious_result_fields(self) -> None:
        private = "PRIVATE|`VALUE\nNEXT_ROW"
        result = {
            "ok": False,
            "source": "/private/%s" % private,
            "output": "/private/output/%s" % private,
            "metadata": "/private/metadata/%s" % private,
            "source_files": 4,
            "source_bytes": 40,
            "checks": [
                {"name": "source", "status": "fail", "message": private},
                {"name": private, "status": "pass", "message": private},
                {"name": "oracle", "status": private, "message": private},
            ],
            "adapters": {
                "python": {"detected": True, "files": [private]},
            },
            "detected_adapters": ["python", private],
            "source_reducers": {
                "python": {"available": True, "files": [private]},
                "java": {"available": False, "files": [private]},
            },
            "ignored_names": [private],
            "ignored_paths": [private],
            "keep_paths": [private],
            "text_files": [private],
            "gitignore_files": [private],
            "gitignore_sha256": private,
            "gitignore_recursive": True,
            "baseline": {"status": "fail", "message": private},
            "command": private,
            "match": private,
            "environment": {private: private},
        }

        rendered = format_doctor_markdown(
            result,
            requested_adapter=private,
            requested_source_reducer=private,
            backend=private,
            oracle_mode=private,
        )

        self.assertNotIn("PRIVATE", rendered)
        self.assertNotIn("NEXT_ROW", rendered)
        self.assertNotIn("/private", rendered)
        self.assertNotIn("message", rendered)
        self.assertIn("| `status` | `needs_attention` |", rendered)
        self.assertIn("| `detected_adapters` | `python` |", rendered)
        self.assertIn("| `available_source_reducers` | `python` |", rendered)
        self.assertIn("| `effective_ignore_names` | `1` |", rendered)
        self.assertIn("| `source` | `fail` |", rendered)
        self.assertIn("| `oracle` | `not_run` |", rendered)

    def test_doctor_markdown_rejects_unbounded_or_invalid_aggregate_values(
        self,
    ) -> None:
        rendered = format_doctor_markdown(
            {
                "ok": "yes",
                "source_files": True,
                "source_bytes": 1 << 500,
                "checks": [
                    {"name": ["source"], "status": "pass"},
                    {"name": "source", "status": ["pass"]},
                ],
                "baseline": {
                    "status": "PRIVATE_STATUS",
                    "runs": -1,
                    "passes": True,
                    "rate": math.nan,
                    "minimum_rate": math.inf,
                    "confidence": 2.0,
                },
            },
            requested_adapter="PRIVATE_ADAPTER",
            requested_source_reducer="PRIVATE_REDUCER",
            backend="PRIVATE_BACKEND",
            oracle_mode="PRIVATE_ORACLE",
        )

        self.assertNotIn("PRIVATE", rendered)
        self.assertIn("| `status` | `needs_attention` |", rendered)
        self.assertIn("| `source_files` | `n/a` |", rendered)
        self.assertIn("| `source_bytes` | `n/a` |", rendered)
        self.assertIn("| `baseline_status` | `unknown` |", rendered)
        self.assertIn("| `baseline_rate` | `n/a` |", rendered)
        self.assertIn("| `source` | `not_run` |", rendered)

    def test_doctor_readiness_fails_closed_for_inconsistent_baseline(self) -> None:
        result = {"ok": True, "checks": [], "baseline": {"status": "fail"}}

        self.assertTrue(
            format_doctor(result).startswith("ReproMin doctor: needs attention\n")
        )
        self.assertIn(
            "| `status` | `needs_attention` |",
            format_doctor_markdown(result),
        )

    def test_doctor_does_not_recommend_reduction_when_another_check_failed(
        self,
    ) -> None:
        result = {
            "ok": False,
            "source": "/private/source",
            "checks": [
                {"name": "output", "status": "fail", "message": "already exists"}
            ],
            "baseline": {
                "status": "pass",
                "runs": 2,
                "passes": 2,
                "exit_code": 1,
            },
        }

        rendered = format_doctor(result)
        self.assertTrue(rendered.startswith("ReproMin doctor: needs attention\n"))
        self.assertIn("Baseline: 2/2 passes, exit code 1", rendered)
        self.assertIn("Next: fix failed checks", rendered)
        self.assertNotIn("rerun the same failure options", rendered)

    def test_failing_doctor_markdown_does_not_render_private_source_path(
        self,
    ) -> None:
        private_source = self._source().parent / "PRIVATE_SOURCE_DO_NOT_SHARE"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "doctor",
                    str(private_source),
                    "--gitignore",
                    "--format",
                    "markdown",
                ]
            )

        self.assertEqual(1, exit_code)
        self.assertNotIn("PRIVATE_SOURCE_DO_NOT_SHARE", stdout.getvalue())
        self.assertIn("| `status` | `needs_attention` |", stdout.getvalue())
        self.assertIn(
            "| `available_source_reducers` | `unknown` |", stdout.getvalue()
        )
        self.assertIn("| `source` | `fail` |", stdout.getvalue())
        self.assertIn("| `gitignore` | `not_run` |", stdout.getvalue())

    def test_cli_accepts_doctor_config_with_markdown_format(self) -> None:
        source = self._source()
        config = source.parent / "doctor.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "failure": {"command": "false", "exit_code": 1},
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with patch(
            "repomin.cli.run_doctor",
            return_value=(
                True,
                {
                    "ok": True,
                    "checks": [],
                    "baseline": {"status": "not_run"},
                    "gitignore_files": [],
                    "gitignore_recursive": False,
                    "keep_paths": [],
                    "text_files": [],
                },
            ),
        ):
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "doctor",
                        str(source),
                        "--config",
                        str(config),
                        "--format",
                        "markdown",
                    ]
                )
        self.assertEqual(0, exit_code)
        self.assertTrue(stdout.getvalue().startswith("# ReproMin Doctor summary\n"))

    def test_cli_returns_one_when_baseline_does_not_reproduce(self) -> None:
        source = self._source()
        stdout = io.StringIO()
        command = _python_command("reproduce.py")
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "doctor",
                    str(source),
                    "--command",
                    command,
                    "--match",
                    "NO_SUCH_FAILURE",
                    "--json",
                ]
            )
        self.assertEqual(1, exit_code)
        result = json.loads(stdout.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("fail", result["baseline"]["status"])
        self.assertEqual(2, result["baseline"]["runs"])
        self.assertEqual(0, result["baseline"]["passes"])
        self.assertEqual(0.0, result["baseline"]["rate"])

        markdown = io.StringIO()
        with contextlib.redirect_stdout(markdown):
            markdown_code = main(
                [
                    "doctor",
                    str(source),
                    "--command",
                    command,
                    "--match",
                    "NO_SUCH_FAILURE",
                    "--format",
                    "markdown",
                ]
            )
        self.assertEqual(1, markdown_code)
        self.assertIn("| `baseline_status` | `fail` |", markdown.getvalue())
        self.assertIn("| `baseline_runs` | `2` |", markdown.getvalue())
        self.assertIn("| `baseline_passes` | `0` |", markdown.getvalue())


if __name__ == "__main__":
    unittest.main()
