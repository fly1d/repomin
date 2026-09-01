import argparse
import contextlib
import io
import json
import os
import threading
import signal
import shlex
import subprocess
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

from repomin.cli import (
    _build_runner,
    _parse_byte_size,
    _parse_confidence,
    _parse_environment,
    _parse_ignore_name,
    _parse_ignore_path,
    _load_gitignore,
    _parse_rate,
    _run_fixed_point,
    _working_directory_configuration,
    build_parser,
    main,
)
from repomin.model import TREE_FINGERPRINT_POLICY, ReductionStats, RunResult
from repomin.oracle import (
    FailureOracle,
    clopper_pearson_lower_bound,
    exact_binomial_upper_tail,
)
from repomin.report import validate_report_document
from repomin.session import _tree_digest


SCRIPT = """\
from pathlib import Path
import sys

if not Path("required.txt").exists():
    print("DIFFERENT_FAILURE", file=sys.stderr)
    raise SystemExit(2)
print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(1)
"""

JAVA_EXCEPTION_SCRIPT = """\
from pathlib import Path
import sys

if Path("required.txt").exists():
    print("java.lang.NoSuchMethodError: demo.Target.missing()", file=sys.stderr)
    print("    at demo.Trigger.run(Trigger.java:42)", file=sys.stderr)
else:
    print("java.lang.NoSuchMethodError: demo.Other.missing()", file=sys.stderr)
    print("    at demo.Other.run(Other.java:9)", file=sys.stderr)
raise SystemExit(1)
"""

GRADLE_SCRIPT = """\
from pathlib import Path
import sys

settings = Path("settings.gradle.kts").read_text(encoding="utf-8")
if '":app"' not in settings:
    print("DIFFERENT_FAILURE", file=sys.stderr)
    raise SystemExit(2)
print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(1)
"""

PYTHON_MANIFEST_SCRIPT = """\
from pathlib import Path
import sys

manifest = Path("pyproject.toml").read_text(encoding="utf-8")
if "fastapi>=0.100" not in manifest:
    print("DIFFERENT_FAILURE", file=sys.stderr)
    raise SystemExit(2)
print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(1)
"""

PYTHON_EXCEPTION_SCRIPT = """\
from pathlib import Path

def target_failure():
    raise ValueError("payment failed")

def fallback_failure():
    raise ValueError("payment failed")

if Path("required.txt").exists():
    target_failure()
else:
    fallback_failure()
"""

PYTHON_SOURCE_SCRIPT = """\
from pathlib import Path
import json

def unused():
    return "unused"

def target():
    if not Path("required.txt").exists():
        raise SystemExit(2)
    print("ORIGINAL_FAILURE")
    raise SystemExit(1)

target()
"""

PROCESS_FAILURE_SCRIPT = """\
import os
from pathlib import Path
import resource
import signal

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
selected = signal.SIGABRT if Path("required.txt").exists() else signal.SIGTERM
os.kill(os.getpid(), selected)
"""


def _metadata_output(output: Path) -> Path:
    return output.with_name(output.name + ".repomin")


def _report(output: Path) -> dict:
    return json.loads(
        (_metadata_output(output) / "report.json").read_text(encoding="utf-8")
    )


class _CheckpointRunner:
    def __init__(self, interrupt_at=None):
        self.calls = 0
        self.interrupt_at = interrupt_at

    def run(self, cwd: Path) -> RunResult:
        self.calls += 1
        if self.interrupt_at is not None and self.calls >= self.interrupt_at:
            raise KeyboardInterrupt
        return RunResult(1, "ORIGINAL_FAILURE", "", 0.01)


class _PrefixPassRunner:
    def __init__(self, passing_calls: int):
        self.calls = 0
        self.passing_calls = passing_calls

    def run(self, cwd: Path) -> RunResult:
        self.calls += 1
        output = (
            "ORIGINAL_FAILURE"
            if self.calls <= self.passing_calls
            else "DIFFERENT_FAILURE"
        )
        return RunResult(1, output, "", 0.01)


class _PathRecordingRunner:
    def __init__(self) -> None:
        self.paths = []

    def run(self, cwd: Path) -> RunResult:
        self.paths.append(cwd)
        return RunResult(1, "ORIGINAL_FAILURE", "", 0.01)


class _SemanticStubHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        requested_model = body.get("model")
        inner = json.dumps(
            {"edits": [{"path": "data.txt", "replace": "NEEDLE\n"}]}
        )
        payload = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": inner,
                        }
                    }
                ],
                "model": requested_model,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class CliTest(unittest.TestCase):
    def test_missing_keep_path_reports_an_actionable_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "false",
                        "--match",
                        "failure",
                        "--keep",
                        "docs/LICENSE",
                    ]
                )

            self.assertEqual(2, exit_code)
            self.assertIn(
                "keep path does not exist in the source repository: docs/LICENSE",
                stderr.getvalue(),
            )
            self.assertIn("check --keep docs/LICENSE", stderr.getvalue())

    def test_filesystem_errors_return_actionable_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            output = root / "output"
            stderr = io.StringIO()

            with patch(
                "repomin.cli.write_report",
                side_effect=OSError("simulated disk full"),
            ):
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            str(source),
                            "--command",
                            "python3 reproduce.py",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--adapter",
                            "none",
                            "--source-reducer",
                            "none",
                            "--output",
                            str(output),
                        ]
                    )

            self.assertEqual(2, exit_code)
            self.assertIn("repomin: simulated disk full", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_shell_completion_scripts_are_available(self) -> None:
        parser_options = {
            option
            for action in build_parser()._actions
            for option in action.option_strings
            if option.startswith("--")
        }
        fish_boolean_options = {
            option
            for action in build_parser()._actions
            for option in action.option_strings
            if option.startswith("--") and action.nargs == 0
        }
        for shell, marker in (
            ("bash", "complete -F _repomin repomin"),
            ("zsh", "#compdef repomin"),
            ("fish", "complete -c repomin"),
            ("powershell", "Register-ArgumentCompleter -Native -CommandName repomin"),
        ):
            with self.subTest(shell=shell):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                    stderr
                ):
                    exit_code = main(["completion", shell])
                self.assertEqual(0, exit_code)
                self.assertIn(marker, stdout.getvalue())
                self.assertIn("pipenv", stdout.getvalue())
                self.assertIn("doctor", stdout.getvalue())
                self.assertIn("report", stdout.getvalue())
                self.assertIn("validate", stdout.getvalue())
                self.assertIn("replay", stdout.getvalue())
                for option in sorted(parser_options):
                    with self.subTest(shell=shell, option=option):
                        expected = (
                            option[2:]
                            if shell == "fish" and option in fish_boolean_options
                            else ("-l " + option[2:] if shell == "fish" else option)
                        )
                        self.assertIn(expected, stdout.getvalue())
                if shell == "fish":
                    self.assertIn("-l payload", stdout.getvalue())
                    self.assertIn("-l json", stdout.getvalue())
                else:
                    self.assertIn("--payload", stdout.getvalue())
                    self.assertIn("--json", stdout.getvalue())
                if shell == "powershell":
                    self.assertIn("CompletionResult", stdout.getvalue())
                    self.assertIn("--adapter", stdout.getvalue())
                self.assertEqual("", stderr.getvalue())

    def test_shell_completion_rejects_unknown_shell(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(["completion", "tcsh"])
        self.assertEqual(2, exit_code)
        self.assertIn("unsupported shell", stderr.getvalue())

    def test_shell_completion_help_is_available(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["completion", "--help"])
        self.assertEqual(0, exit_code)
        self.assertIn(
            "usage: repomin completion {bash,zsh,fish,powershell}",
            stdout.getvalue(),
        )

    def test_report_parent_help_lists_subcommands_and_detailed_help(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["report", "--help"])
        self.assertEqual(0, exit_code)
        help_text = stdout.getvalue()
        self.assertIn("usage: repomin report {validate,replay,compare}", help_text)
        self.assertIn("validate  validate report structure", help_text)
        self.assertIn("replay    run the recorded failure", help_text)
        self.assertIn("compare   compare privacy-safe evidence", help_text)
        self.assertIn("repomin report validate --help", help_text)

    def test_report_compare_help_explains_order_and_display_labels(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                main(["report", "compare", "--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = stdout.getvalue()
        self.assertIn("two or more report.json files", help_text)
        self.assertIn("display label", help_text)
        self.assertIn("--format", help_text)

    def test_fish_completion_lists_every_supported_shell(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["completion", "fish"])
        self.assertEqual(0, exit_code)
        self.assertIn("-a 'bash zsh fish powershell'", stdout.getvalue())

    def test_report_validate_completion_includes_output_formats(self) -> None:
        for shell in ("bash", "zsh", "fish", "powershell"):
            with self.subTest(shell=shell):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(["completion", shell])
                self.assertEqual(0, exit_code)
                script = stdout.getvalue()
                self.assertIn("validate", script)
                self.assertIn("-l format" if shell == "fish" else "--format", script)
                self.assertIn("text", script)
                self.assertIn("json", script)
                self.assertIn("markdown", script)

    def test_report_compare_completion_includes_paths_labels_and_formats(self) -> None:
        for shell in ("bash", "zsh", "fish", "powershell"):
            with self.subTest(shell=shell):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(["completion", shell])
                self.assertEqual(0, exit_code)
                script = stdout.getvalue()
                self.assertIn("compare", script)
                self.assertIn("label", script)
                self.assertIn("markdown", script)

    def test_match_is_required_without_process_failure_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [str(source), "--command", "false"]
                )

            self.assertEqual(2, exit_code)
            self.assertIn(
                "--match is required unless --process-failure or --exit-code "
                "is enabled",
                stderr.getvalue(),
            )

    def test_process_failure_cannot_duplicate_an_explicit_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "false",
                        "--process-failure",
                        "--exit-code",
                        "1",
                    ]
                )

            self.assertEqual(2, exit_code)
            self.assertIn("cannot be combined with --exit-code", stderr.getvalue())

    def test_exit_code_can_be_the_only_failure_criterion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            output = Path(directory) / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "if not Path('required.txt').exists():\n"
                "    print('missing required')\n"
                "    raise SystemExit(9)\n"
                "print('unrelated output is allowed')\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--exit-code",
                        "7",
                        "--adapter",
                        "none",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertTrue((output / "required.txt").exists())
            self.assertFalse((output / "unused.txt").exists())
            report = _report(output)
            self.assertIsNone(report["failure_match"])
            self.assertEqual(7, report["baseline_exit_code"])
            self.assertEqual(7, report["final_exit_code"])

    def test_working_directory_configuration_uses_only_valid_stable_basenames(
        self,
    ) -> None:
        self.assertEqual(
            ("host-output-basename-v1", "artifact", "artifact"),
            _working_directory_configuration(Path("/tmp/artifact"), "host"),
        )
        self.assertEqual(
            ("docker-workspace-v1", "workspace", None),
            _working_directory_configuration(Path("/"), "docker"),
        )
        with self.assertRaisesRegex(ValueError, "ordinary directory name"):
            _working_directory_configuration(Path("/"), "host")

    def test_output_symlinks_are_rejected_before_normal_or_resumed_work(self) -> None:
        for resume in (False, True):
            for target_exists in (False, True):
                with self.subTest(resume=resume, target_exists=target_exists):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        source = root / "source"
                        source.mkdir()
                        target = root / "output-target"
                        if target_exists:
                            target.mkdir()
                        output = root / "output"
                        try:
                            output.symlink_to(target, target_is_directory=True)
                        except (NotImplementedError, OSError) as exc:
                            self.skipTest("directory symlinks are unavailable: %s" % exc)

                        arguments = [
                            str(source),
                            "--command",
                            "reproduce",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--output",
                            str(output),
                        ]
                        if resume:
                            arguments.extend(
                                ["--resume", "--session", str(root / "session")]
                            )
                        runner = _CheckpointRunner()
                        stderr = io.StringIO()
                        with patch("repomin.cli._build_runner", return_value=runner):
                            with contextlib.redirect_stderr(stderr):
                                exit_code = main(arguments)

                        self.assertEqual(2, exit_code)
                        self.assertEqual(0, runner.calls)
                        self.assertTrue(output.is_symlink())
                        self.assertEqual(target_exists, target.is_dir())
                        self.assertIn(
                            "output must not be a symbolic link",
                            stderr.getvalue(),
                        )

    def test_metadata_symlinks_are_rejected_before_normal_or_resumed_work(
        self,
    ) -> None:
        for resume in (False, True):
            for target_exists in (False, True):
                with self.subTest(resume=resume, target_exists=target_exists):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        source = root / "source"
                        source.mkdir()
                        output = root / "output"
                        if resume:
                            output.mkdir()
                        target = root / "metadata-target"
                        if target_exists:
                            target.mkdir()
                        metadata = _metadata_output(output)
                        try:
                            metadata.symlink_to(target, target_is_directory=True)
                        except (NotImplementedError, OSError) as exc:
                            self.skipTest("directory symlinks are unavailable: %s" % exc)

                        arguments = [
                            str(source),
                            "--command",
                            "reproduce",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--output",
                            str(output),
                        ]
                        if resume:
                            arguments.extend(
                                ["--resume", "--session", str(root / "session")]
                            )
                        runner = _CheckpointRunner()
                        stderr = io.StringIO()
                        with patch("repomin.cli._build_runner", return_value=runner):
                            with contextlib.redirect_stderr(stderr):
                                exit_code = main(arguments)

                        self.assertEqual(2, exit_code)
                        self.assertEqual(0, runner.calls)
                        self.assertTrue(metadata.is_symlink())
                        self.assertEqual(target_exists, target.is_dir())
                        self.assertEqual(resume, output.is_dir())
                        self.assertIn(
                            "metadata output must not be a symbolic link",
                            stderr.getvalue(),
                        )

    def test_fixed_point_requeues_only_components_dirtied_by_other_changes(self) -> None:
        stats = ReductionStats(source_files=0, source_bytes=0)
        calls = []

        def first() -> None:
            calls.append("first")
            if calls.count("first") == 1:
                stats.accepted += 1

        def second() -> None:
            calls.append("second")
            if calls.count("second") == 1:
                stats.accepted += 1

        progress = []
        _run_fixed_point(
            [("first", first), ("second", second)],
            stats,
            progress.append,
        )

        self.assertEqual(["first", "second", "first"], calls)
        self.assertEqual(
            [
                "fixed-point component: first",
                "fixed-point component: second",
                "fixed-point component: first",
            ],
            progress,
        )

    def test_parses_docker_resource_sizes(self) -> None:
        self.assertEqual(512, _parse_byte_size("512"))
        self.assertEqual(64 * 1024**2, _parse_byte_size("64MiB"))
        self.assertEqual(2 * 1024**3, _parse_byte_size("2g"))

    def test_ignore_names_are_repeatable_and_exact_basenames(self) -> None:
        self.assertEqual("private.env", _parse_ignore_name(" private.env "))
        args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "failure",
                "--ignore",
                "private.env",
                "--ignore",
                "generated",
            ]
        )
        self.assertEqual(["private.env", "generated"], args.ignore_names)
        for value in ("", ".", "..", "nested/generated", "/tmp/generated"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    _parse_ignore_name(value)

    def test_environment_entries_are_parsed_without_losing_equals(self) -> None:
        self.assertEqual(("CI", "a=b"), _parse_environment("CI=a=b"))
        args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "failure",
                "--env",
                "CI=1",
                "--env",
                "EMPTY=",
            ]
        )
        self.assertEqual([("CI", "1"), ("EMPTY", "")], args.environment_entries)
        for value in (
            "CI",
            "-BAD=value",
            "BAD.NAME=value",
            "REPOMIN=override",
            "CI=bad\x00value",
        ):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    _parse_environment(value)

    def test_environment_mapping_rejects_case_collisions_on_windows(self) -> None:
        from repomin.cli import _environment_mapping

        with patch("repomin.cli.os", SimpleNamespace(name="nt")):
            with self.assertRaisesRegex(ValueError, "unambiguous"):
                _environment_mapping([("Path", "one"), ("PATH", "two")])

    def test_ignore_paths_are_exact_repository_relative_paths(self) -> None:
        self.assertEqual("services/api/private", _parse_ignore_path("services/api/private"))
        args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "failure",
                "--ignore-path",
                "services/api/private",
                "--ignore-path",
                "fixtures/large",
            ]
        )
        self.assertEqual(
            ["services/api/private", "fixtures/large"],
            args.ignore_paths,
        )
        for value in ("", ".", "../private", "/private", "services/*"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    _parse_ignore_path(value)

    def test_keep_paths_use_the_same_exact_path_grammar(self) -> None:
        args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "failure",
                "--keep",
                "LICENSE",
                "--keep",
                "fixtures/golden",
            ]
        )
        self.assertEqual(["LICENSE", "fixtures/golden"], args.keep_paths)
        for value in ("", ".", "../LICENSE", "/LICENSE", "fixtures/*"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    _parse_ignore_path(value)

    def test_gitignore_options_are_parsed_independently(self) -> None:
        args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "failure",
                "--gitignore",
                "--gitignore-file",
                ".custom-ignore",
                "--gitignore-file",
                ".custom-ignore",
                "--gitignore-recursive",
            ]
        )
        self.assertTrue(args.gitignore)
        self.assertTrue(args.gitignore_recursive)
        self.assertEqual([".custom-ignore", ".custom-ignore"], args.gitignore_files)

    def test_load_gitignore_reads_rule_files_and_hashes_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / ".gitignore").write_text("*.log\n", encoding="utf-8")
            (source / "extra.ignore").write_text("/cache/\n", encoding="utf-8")

            matcher, files, digest, recursive = _load_gitignore(
                source,
                True,
                ["extra.ignore"],
            )

            self.assertEqual([".gitignore", "extra.ignore"], list(files))
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertFalse(recursive)
            assert matcher is not None
            self.assertTrue(matcher.matches(PurePosixPath("debug.log")))
            self.assertTrue(matcher.matches(PurePosixPath("cache/x")))
            self.assertFalse(matcher.matches(PurePosixPath("keep.txt")))

    def test_load_gitignore_recursive_discovers_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / ".gitignore").write_text("*.log\n", encoding="utf-8")
            nested = source / "services"
            nested.mkdir()
            (nested / ".gitignore").write_text("/private/\n", encoding="utf-8")
            (nested / "unused.log").write_text("x", encoding="utf-8")

            matcher, files, digest, recursive = _load_gitignore(
                source,
                False,
                [],
                recursive=True,
            )

            self.assertTrue(recursive)
            self.assertEqual([".gitignore", "services/.gitignore"], list(files))
            assert matcher is not None
            self.assertTrue(matcher.matches(PurePosixPath("debug.log")))
            self.assertTrue(matcher.matches(PurePosixPath("services/private/x")))
            self.assertFalse(matcher.matches(PurePosixPath("services/keep.txt")))

    def test_recursive_gitignore_skips_directories_ignored_by_root_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / ".gitignore").write_text("/build/\n", encoding="utf-8")
            build = source / "build"
            build.mkdir()
            (build / ".gitignore").write_text("ignored-rule\n", encoding="utf-8")

            matcher, files, digest, recursive = _load_gitignore(
                source,
                False,
                [],
                recursive=True,
            )

            self.assertTrue(recursive)
            self.assertEqual([".gitignore"], list(files))
            assert matcher is not None
            self.assertTrue(matcher.matches(PurePosixPath("build/generated.txt")))

    def test_recursive_gitignore_applies_nested_rules_while_discovering_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / ".gitignore").write_text("\n", encoding="utf-8")
            services = source / "services"
            private = services / "private"
            private.mkdir(parents=True)
            (services / ".gitignore").write_text("/private/\n", encoding="utf-8")
            # This file is malformed on purpose. Because the parent rule
            # excludes ``private/``, recursive discovery must not read it.
            (private / ".gitignore").write_text("[unterminated\n", encoding="utf-8")

            matcher, files, digest, recursive = _load_gitignore(
                source,
                False,
                [],
                recursive=True,
            )

            self.assertTrue(recursive)
            self.assertEqual(
                [".gitignore", "services/.gitignore"], list(files)
            )
            assert matcher is not None
            self.assertTrue(matcher.matches(PurePosixPath("services/private/x")))

    def test_recursive_gitignore_keeps_double_star_negated_subtree_walkable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / ".gitignore").write_text(
                "foo/\n!foo/**/keep.txt\n",
                encoding="utf-8",
            )
            deep = source / "foo" / "x" / "y"
            deep.mkdir(parents=True)
            (deep / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
            (deep / "keep.txt").write_text("keep\n", encoding="utf-8")

            matcher, files, digest, recursive = _load_gitignore(
                source,
                False,
                [],
                recursive=True,
            )

            self.assertTrue(recursive)
            self.assertEqual(
                [".gitignore", "foo/x/y/.gitignore"], list(files)
            )
            assert matcher is not None
            self.assertFalse(
                matcher.matches(PurePosixPath("foo/x/y/keep.txt"))
            )

    def test_rate_options_are_parsed_and_use_a_single_count_minimum_by_default(self) -> None:
        args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "failure",
                "--min-baseline-rate",
                "0.5",
                "--min-candidate-rate",
                "0.9",
                "--confidence",
                "0.8",
                "--run-confidence",
                "0.99",
            ]
        )
        self.assertEqual(0.5, args.min_baseline_rate)
        self.assertEqual(0.9, args.min_candidate_rate)
        self.assertEqual(0.8, args.confidence)
        self.assertEqual(0.99, args.run_confidence)

    def test_max_attempts_is_an_optional_positive_candidate_budget(self) -> None:
        args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "failure",
                "--max-attempts",
                "17",
            ]
        )
        self.assertEqual(17, args.max_attempts)

    def test_max_duration_is_parsed_as_seconds(self) -> None:
        args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "failure",
                "--max-duration",
                "2.5",
            ]
        )
        self.assertEqual(2.5, args.max_duration)

    def test_semantic_timeout_is_parsed_as_seconds(self) -> None:
        args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "failure",
                "--semantic-timeout",
                "12.5",
            ]
        )
        self.assertEqual(12.5, args.semantic_timeout)

    def test_run_confidence_requires_a_candidate_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "false",
                        "--match",
                        "failure",
                        "--run-confidence",
                        "0.95",
                        "--output",
                        str(root / "output"),
                    ]
                )
        self.assertEqual(2, exit_code)
        self.assertIn(
            "--run-confidence requires --min-candidate-rate",
            stderr.getvalue(),
        )

    def test_rejects_invalid_rate_and_confidence_values(self) -> None:
        for value in ("0", "1", "-0.1", "nan", "inf"):
            with self.assertRaises(argparse.ArgumentTypeError):
                _parse_rate(value)
        for value in ("0", "1", "-0.1", "nan", "inf"):
            with self.assertRaises(argparse.ArgumentTypeError):
                _parse_confidence(value)

    def test_holdout_options_must_be_configured_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            for index, arguments in enumerate(
                (
                    ["--holdout-runs", "29"],
                    ["--min-holdout-rate", "0.9"],
                    ["--holdout-confidence", "0.95"],
                )
            ):
                runner = _CheckpointRunner()
                stderr = io.StringIO()
                with patch("repomin.cli._build_runner", return_value=runner):
                    with contextlib.redirect_stderr(stderr):
                        exit_code = main(
                            [
                                str(source),
                                "--command",
                                "reproduce",
                                "--match",
                                "ORIGINAL_FAILURE",
                                "--output",
                                str(root / ("output-%d" % index)),
                            ]
                            + arguments
                        )

                self.assertEqual(2, exit_code)
                self.assertEqual(0, runner.calls)
                if arguments[0] == "--holdout-confidence":
                    self.assertIn(
                        "--holdout-confidence requires --holdout-runs",
                        stderr.getvalue(),
                    )
                else:
                    self.assertIn(
                        "--holdout-runs and --min-holdout-rate",
                        stderr.getvalue(),
                    )

    def test_holdout_rate_attainability_rejects_28_all_pass_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            runner = _CheckpointRunner()
            stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=runner):
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            str(source),
                            "--command",
                            "reproduce",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--holdout-runs",
                            "28",
                            "--min-holdout-rate",
                            "0.9",
                            "--holdout-confidence",
                            "0.95",
                            "--output",
                            str(output),
                        ]
                    )

            self.assertEqual(2, exit_code)
            self.assertEqual(0, runner.calls)
            self.assertFalse(output.exists())
            self.assertIn(
                "minimum holdout rate 0.9 is unattainable with 28 holdout runs",
                stderr.getvalue(),
            )

    def test_java_classpath_entries_are_repeatable_and_atomic(self) -> None:
        args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "failure",
                "--java-classpath",
                "dependencies/api:1.jar",
                "--java-classpath",
                "compiled classes",
            ]
        )

        self.assertEqual(
            ["dependencies/api:1.jar", "compiled classes"],
            args.java_classpath,
        )
        help_text = build_parser().format_help()
        self.assertIn("host path for Java AST attribution", help_text)
        self.assertIn("does not change the reproduction", help_text)
        self.assertIn("command or Docker mounts", help_text)

    def test_java_classpath_does_not_change_runner_command_or_docker_mounts(
        self,
    ) -> None:
        host_args = build_parser().parse_args(
            [
                "--command",
                "printf ORIGINAL_FAILURE",
                "--match",
                "ORIGINAL_FAILURE",
                "--java-classpath",
                "/host/dependency.jar",
            ]
        )
        host_runner = _build_runner(host_args)
        self.assertEqual("printf ORIGINAL_FAILURE", host_runner.command)

        docker_args = build_parser().parse_args(
            [
                "--command",
                "false",
                "--match",
                "ORIGINAL_FAILURE",
                "--backend",
                "docker",
                "--docker-image",
                "example:local",
                "--java-classpath",
                "/host/dependency.jar",
            ]
        )
        with patch("repomin.execution.shutil.which", return_value="/usr/bin/docker"):
            with patch("repomin.cli.DockerRunner.validate") as validate:
                docker_runner = _build_runner(docker_args)

        candidate = Path.cwd() / "repomin-candidate"
        argv = docker_runner.build_argv(candidate, Path.cwd() / "repomin.cid")
        mounts = [
            argv[index + 1]
            for index, value in enumerate(argv)
            if value == "--mount"
        ]
        self.assertEqual("false", docker_runner.command)
        self.assertNotIn("/host/dependency.jar", argv)
        self.assertEqual(
            ["type=bind,source=%s,target=/workspace" % candidate.absolute()],
            mounts,
        )
        validate.assert_called_once_with()

    def test_docker_report_records_reference_and_resolved_image_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            runner = _CheckpointRunner()
            runner.resolved_image_id = "sha256:" + "a" * 64

            with patch("repomin.cli._build_runner", return_value=runner):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "reproduce",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--baseline-runs",
                        "1",
                        "--backend",
                        "docker",
                        "--docker-image",
                        "fixture:mutable",
                        "--adapter",
                        "none",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code)
            execution = _report(output)["execution"]
            self.assertEqual("fixture:mutable", execution["image"])
            self.assertEqual(runner.resolved_image_id, execution["image_id"])
            reproduction = (_metadata_output(output) / "REPOMIN.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Backend: `docker`", reproduction)
            self.assertIn("Docker image reference: `fixture:mutable`", reproduction)
            self.assertIn("Docker image ID: `%s`" % runner.resolved_image_id, reproduction)
            self.assertIn("Docker network policy: `none`", reproduction)

    def test_end_to_end_writes_repro_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                        "--jobs",
                        "2",
                        "--no-cache",
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertTrue((output / "reproduce.py").exists())
            self.assertTrue((output / "required.txt").exists())
            self.assertFalse((output / "unused.txt").exists())
            report = _report(output)
            self.assertEqual("ORIGINAL_FAILURE", report["failure_match"])
            self.assertEqual(3, report["source"]["files"])
            self.assertEqual(2, report["output"]["files"])
            self.assertEqual(2, report["execution"]["jobs"])
            self.assertFalse(report["execution"]["cache_enabled"])
            self.assertEqual("host", report["execution"]["backend"])
            reproduction = (_metadata_output(output) / "REPOMIN.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Backend: `host`", reproduction)
            self.assertIn("## Payload", reproduction)
            self.assertIn("Reduced payload: `2` files", reproduction)
            self.assertIn("reproduce.py", reproduction)
            self.assertIn("required.txt", reproduction)
            self.assertNotIn("unused.txt", reproduction)
            self.assertEqual(0, report["cache_hits"])

    def test_custom_ignore_is_applied_before_baseline_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            generated = source / "generated"
            (generated / "nested").mkdir(parents=True)
            (generated / "ignored.txt").write_text("large\n", encoding="utf-8")
            (generated / "nested" / "more.txt").write_text(
                "large\n", encoding="utf-8"
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--ignore",
                        "generated",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertFalse((output / "generated").exists())
            report = _report(output)
            self.assertIn("generated", report["execution"]["ignored_names"])
            self.assertEqual(2, report["source"]["files"])

    def test_gitignore_is_applied_before_baseline_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / ".gitignore").write_text(
                "*.log\n/cache/\n",
                encoding="utf-8",
            )
            (source / "debug.log").write_text("large\n", encoding="utf-8")
            cache = source / "cache"
            cache.mkdir()
            (cache / "generated.bin").write_text("large\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--gitignore",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertFalse((output / "debug.log").exists())
            self.assertFalse((output / "cache").exists())
            report = _report(output)
            self.assertEqual([".gitignore"], report["execution"]["gitignore_files"])
            self.assertRegex(
                report["execution"]["gitignore_sha256"],
                r"^[0-9a-f]{64}$",
            )
            # The rule file remains an ordinary tracked source file; only the
            # entries its rules exclude disappear from the initial workspace.
            self.assertEqual(3, report["source"]["files"])

    def test_bare_gitignore_directory_rule_keeps_copy_and_fingerprint_consistent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / ".gitignore").write_text("generated\n", encoding="utf-8")
            generated = source / "generated"
            generated.mkdir()
            (generated / "package.json").write_text("{}\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "none",
                        "--source-reducer",
                        "none",
                        "--gitignore",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertFalse((output / "generated").exists())
            report = _report(output)
            self.assertEqual(3, report["source"]["files"])

    def test_keep_path_overrides_gitignore_and_protects_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / ".gitignore").write_text("generated\n", encoding="utf-8")
            generated = source / "generated"
            generated.mkdir()
            (generated / "keep.txt").write_text("keep this artifact\n", encoding="utf-8")
            (generated / "drop.txt").write_text("discard this artifact\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "none",
                        "--source-reducer",
                        "none",
                        "--gitignore",
                        "--keep",
                        "generated/keep.txt",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertTrue((output / "generated" / "keep.txt").is_file())
            self.assertFalse((output / "generated" / "drop.txt").exists())

    def test_recursive_gitignore_applies_nested_rule_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / ".gitignore").write_text("*.log\n", encoding="utf-8")
            services = source / "services"
            services.mkdir()
            (services / ".gitignore").write_text("/private/\n", encoding="utf-8")
            (services / "unused.log").write_text("large\n", encoding="utf-8")
            private = services / "private"
            private.mkdir()
            (private / "secret.txt").write_text("large\n", encoding="utf-8")
            (services / "keep.txt").write_text("small\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--gitignore-recursive",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertFalse((output / "services" / "unused.log").exists())
            self.assertFalse((output / "services" / "private").exists())
            report = _report(output)
            self.assertTrue(report["execution"]["gitignore_recursive"])
            self.assertEqual(
                [".gitignore", "services/.gitignore"],
                report["execution"]["gitignore_files"],
            )

    def test_keep_path_is_preserved_even_when_unused_by_the_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")
            (source / "LICENSE").write_text("license\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--keep",
                        "LICENSE",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertTrue((output / "reproduce.py").exists())
            self.assertTrue((output / "required.txt").exists())
            self.assertTrue((output / "LICENSE").exists())
            self.assertFalse((output / "unused.txt").exists())
            report = _report(output)
            self.assertEqual(["LICENSE"], report["execution"]["keep_paths"])

    def test_max_attempts_stops_reduction_and_marks_the_budget_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--max-attempts",
                        "1",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertTrue((output / "reproduce.py").exists())
            self.assertTrue((output / "required.txt").exists())
            report = _report(output)
            self.assertEqual(1, report["execution"]["max_attempts"])
            self.assertTrue(report["execution"]["budget_exhausted"])

    def test_max_duration_stops_reduction_and_marks_the_budget_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--max-duration",
                        "0.001",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            report = _report(output)
            self.assertEqual(0.001, report["execution"]["max_duration_seconds"])
            self.assertTrue(report["execution"]["budget_exhausted"])

    def test_semantic_reducer_http_endpoint_produces_and_records_an_edit(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SemanticStubHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop)
        endpoint = "http://127.0.0.1:%d/v1/chat/completions" % server.server_port

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "if 'NEEDLE' not in Path('data.txt').read_text():\n"
                "    print('DIFFERENT_FAILURE', file=sys.stderr)\n"
                "    raise SystemExit(2)\n"
                "print('ORIGINAL_FAILURE', file=sys.stderr)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            (source / "data.txt").write_text(
                "noise line\nNEEDLE\nmore noise\n",
                encoding="utf-8",
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--adapter",
                        "none",
                        "--semantic-reducer",
                        "http",
                        "--semantic-endpoint",
                        endpoint,
                        "--semantic-model",
                        "test-model",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertEqual("NEEDLE\n", (output / "data.txt").read_text(encoding="utf-8"))
            report = _report(output)
            self.assertEqual("http", report["execution"]["semantic_reducer"])
            self.assertEqual("test-model", report["execution"]["semantic_model"])
            self.assertEqual(endpoint, report["execution"]["semantic_endpoint"])
            self.assertEqual(1, report["execution"]["semantic_calls"])
            self.assertEqual(1, report["execution"]["semantic_accepted"])

    def test_semantic_reducer_none_is_recorded_without_semantic_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--adapter",
                        "none",
                        "--semantic-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            report = _report(output)
            self.assertEqual("none", report["execution"]["semantic_reducer"])
            self.assertIsNone(report["execution"]["semantic_model"])
            self.assertIsNone(report["execution"]["semantic_endpoint"])
            self.assertEqual(0, report["execution"]["semantic_calls"])
            self.assertEqual(0, report["execution"]["semantic_accepted"])
            phases = {
                phase["phase"] for phase in report["phase_statistics"]["phases"]
            }
            self.assertNotIn("semantic", phases)

    def test_semantic_timeout_must_be_positive_when_http_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "false",
                        "--match",
                        "failure",
                        "--semantic-reducer",
                        "http",
                        "--semantic-endpoint",
                        "http://127.0.0.1:9/v1/chat/completions",
                        "--semantic-model",
                        "model",
                        "--semantic-timeout",
                        "0",
                    ]
                )
            self.assertEqual(2, exit_code)
            self.assertIn(
                "--semantic-timeout must be a positive number of seconds",
                stderr.getvalue(),
            )

    def test_text_file_reducer_shrinks_selected_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "if 'NEEDLE' not in Path('data.txt').read_text():\n"
                "    print('DIFFERENT_FAILURE', file=sys.stderr)\n"
                "    raise SystemExit(2)\n"
                "print('ORIGINAL_FAILURE', file=sys.stderr)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            (source / "data.txt").write_text(
                "noise one\nNEEDLE\nnoise two\n",
                encoding="utf-8",
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--adapter",
                        "none",
                        "--text-file",
                        "data.txt",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertEqual(
                "NEEDLE\n",
                (output / "data.txt").read_text(encoding="utf-8"),
            )
            report = _report(output)
            self.assertEqual(["data.txt"], report["execution"]["text_files"])

    def test_explicit_environment_reaches_host_runner_without_leaking_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(
                "import os\n"
                "import sys\n"
                "if os.environ.get('REPOMIN_TEST_FLAG') != 'enabled':\n"
                "    print('DIFFERENT_FAILURE')\n"
                "    raise SystemExit(2)\n"
                "print('ORIGINAL_FAILURE')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--source-reducer",
                        "none",
                        "--env",
                        "REPOMIN_TEST_FLAG=enabled",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            report_text = (_metadata_output(output) / "report.json").read_text(
                encoding="utf-8"
            )
            report = json.loads(report_text)
            self.assertEqual(["REPOMIN_TEST_FLAG"], report["execution"]["environment_names"])
            self.assertRegex(
                report["execution"]["environment_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertNotIn("REPOMIN_TEST_FLAG=enabled", report_text)

    def test_custom_ignore_path_excludes_only_the_selected_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(
                "from pathlib import Path\n"
                "if not Path('nested-b/private/keep.txt').exists():\n"
                "    print('DIFFERENT_FAILURE')\n"
                "    raise SystemExit(2)\n"
                "print('ORIGINAL_FAILURE')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            (source / "nested-a" / "private").mkdir(parents=True)
            (source / "nested-a" / "private" / "secret.txt").write_text(
                "excluded\n", encoding="utf-8"
            )
            (source / "nested-b" / "private").mkdir(parents=True)
            (source / "nested-b" / "private" / "keep.txt").write_text(
                "required\n", encoding="utf-8"
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "none",
                        "--source-reducer",
                        "none",
                        "--ignore-path",
                        "nested-a/private",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertFalse((output / "nested-a").exists())
            self.assertTrue((output / "nested-b" / "private" / "keep.txt").exists())
            report = _report(output)
            self.assertEqual(["nested-a/private"], report["execution"]["ignored_paths"])

    def test_duplicate_environment_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "false",
                        "--match",
                        "failure",
                        "--env",
                        "DUPLICATE=one",
                        "--env",
                        "DUPLICATE=two",
                    ]
                )
            self.assertEqual(2, exit_code)
            self.assertIn("specified more than once", stderr.getvalue())

    def test_resume_rejects_changed_custom_ignore_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            session = root / "session"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            arguments = [
                str(source),
                "--command",
                "python3 reproduce.py",
                "--match",
                "ORIGINAL_FAILURE",
                "--source-reducer",
                "none",
                "--ignore",
                "generated",
                "--session",
                str(session),
                "--output",
                str(output),
            ]
            first_stderr = io.StringIO()
            with contextlib.redirect_stderr(first_stderr):
                self.assertEqual(0, main(arguments))
            changed = list(arguments)
            changed[changed.index("generated")] = "other-generated"
            changed.extend(["--resume"])
            resumed_stderr = io.StringIO()
            with contextlib.redirect_stderr(resumed_stderr):
                self.assertEqual(2, main(changed))
            self.assertIn("session configuration changed", resumed_stderr.getvalue())

    def test_resume_rejects_changed_custom_ignore_path_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            session = root / "session"
            source.mkdir()
            arguments = [
                str(source),
                "--command",
                "reproduce",
                "--match",
                "ORIGINAL_FAILURE",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--ignore-path",
                "private/fixtures",
                "--session",
                str(session),
                "--output",
                str(output),
            ]
            with patch("repomin.cli._build_runner", return_value=_CheckpointRunner()):
                self.assertEqual(0, main(arguments))
            changed = list(arguments)
            changed[changed.index("private/fixtures")] = "private/other"
            resumed_runner = _CheckpointRunner()
            resumed_stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=resumed_runner):
                with contextlib.redirect_stderr(resumed_stderr):
                    self.assertEqual(2, main(changed + ["--resume"]))
            self.assertEqual(0, resumed_runner.calls)
            self.assertIn("session configuration changed", resumed_stderr.getvalue())

    def test_resume_rejects_changed_environment_value_without_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            session = root / "session"
            source.mkdir()
            arguments = [
                str(source),
                "--command",
                "reproduce",
                "--match",
                "ORIGINAL_FAILURE",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--env",
                "FEATURE_GATE=one",
                "--session",
                str(session),
                "--output",
                str(output),
            ]
            with patch("repomin.cli._build_runner", return_value=_CheckpointRunner()):
                self.assertEqual(0, main(arguments))
            changed = list(arguments)
            changed[changed.index("FEATURE_GATE=one")] = "FEATURE_GATE=two"
            resumed_runner = _CheckpointRunner()
            resumed_stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=resumed_runner):
                with contextlib.redirect_stderr(resumed_stderr):
                    self.assertEqual(2, main(changed + ["--resume"]))
            self.assertEqual(0, resumed_runner.calls)
            self.assertIn("session configuration changed", resumed_stderr.getvalue())

    def test_metadata_sidecar_is_excluded_from_certified_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            runner = _CheckpointRunner()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=runner):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                    stderr
                ):
                    exit_code = main(
                        [
                            str(source),
                            "--command",
                            "reproduce",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--baseline-runs",
                            "1",
                            "--adapter",
                            "none",
                            "--source-reducer",
                            "none",
                            "--holdout-runs",
                            "1",
                            "--min-holdout-rate",
                            "0.5",
                            "--holdout-confidence",
                            "0.5",
                            "--output",
                            str(output),
                        ]
                    )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertEqual(3, runner.calls)
            self.assertEqual([], list(output.iterdir()))
            self.assertFalse((output / ".repomin").exists())
            self.assertFalse((output / "REPOMIN.md").exists())

            metadata = _metadata_output(output)
            self.assertEqual(
                {"report.json", "REPOMIN.md"},
                {path.name for path in metadata.iterdir()},
            )
            report = _report(output)
            self.assertEqual(0, report["output"]["files"])
            self.assertEqual(0, report["output"]["bytes"])
            self.assertEqual("tree-sha256-v2", report["output"]["tree_fingerprint_policy"])
            self.assertEqual(64, len(report["output"]["tree_sha256"]))
            self.assertEqual(
                "tree-content-sha256-v1",
                report["output"]["tree_content_fingerprint_policy"],
            )
            self.assertEqual(64, len(report["output"]["tree_content_sha256"]))
            certification = report["holdout_certification"]
            self.assertEqual("certified", certification["status"])
            self.assertEqual(
                _tree_digest(output, set()),
                certification["artifact_fingerprint"],
            )
            self.assertEqual(
                TREE_FINGERPRINT_POLICY,
                certification["artifact_fingerprint_policy"],
            )
            self.assertEqual(
                "exported-payload-tree-v1",
                certification["artifact_scope"],
            )
            self.assertIn("Metadata: %s" % metadata.resolve(), stderr.getvalue())

    def test_holdout_is_disabled_by_default_without_extra_runner_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            runner = _CheckpointRunner()
            stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=runner):
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            str(source),
                            "--command",
                            "reproduce",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--baseline-runs",
                            "1",
                            "--adapter",
                            "none",
                            "--source-reducer",
                            "none",
                            "--output",
                            str(output),
                        ]
                    )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertEqual(2, runner.calls)
            report = _report(output)
            self.assertEqual(1, report["execution"]["final_runs"])
            certification = report["holdout_certification"]
            self.assertEqual("not_requested", certification["status"])
            self.assertEqual(0, certification["planned_runs"])
            self.assertEqual(0, certification["completed_runs"])
            self.assertEqual([], certification["samples"])

    def test_successful_holdout_report_is_separate_from_final_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            runner = _CheckpointRunner()
            stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=runner):
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            str(source),
                            "--command",
                            "reproduce",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--baseline-runs",
                            "1",
                            "--adapter",
                            "none",
                            "--source-reducer",
                            "none",
                            "--holdout-runs",
                            "29",
                            "--min-holdout-rate",
                            "0.9",
                            "--output",
                            str(output),
                        ]
                    )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertEqual(31, runner.calls)
            report = _report(output)
            validate_report_document(report)
            execution = report["execution"]
            self.assertEqual(1, execution["final_runs"])
            self.assertEqual(1, execution["final_passes"])
            certification = report["holdout_certification"]
            self.assertEqual("certified", certification["status"])
            self.assertEqual(
                "fixed-n-clopper-pearson-one-sided-v1",
                certification["policy"],
            )
            self.assertEqual(29, certification["planned_runs"])
            self.assertEqual(29, certification["completed_runs"])
            self.assertEqual(29, certification["passes"])
            self.assertEqual(0, certification["ordinary_failures"])
            self.assertEqual(29, certification["required_passes"])
            self.assertEqual(0.9, certification["minimum_rate"])
            self.assertEqual(0.95, certification["confidence"])
            self.assertEqual(1.0, certification["observed_rate"])
            self.assertGreaterEqual(certification["exact_lower_bound"], 0.9)
            self.assertLessEqual(certification["exact_p_value"], 0.05)
            self.assertTrue(certification["exact_rate_gate_passed"])
            self.assertTrue(certification["fresh_repository_copy_per_run"])
            self.assertFalse(certification["cache_used"])
            self.assertFalse(certification["early_stopping"])
            self.assertEqual(29, len(certification["samples"]))
            self.assertEqual(
                list(range(1, 30)),
                [sample["index"] for sample in certification["samples"]],
            )

    def test_every_host_runner_copy_uses_the_output_basename_and_unique_parent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "stable-output-name"
            source.mkdir()
            (source / "unused.txt").write_text("unused\n", encoding="utf-8")
            runner = _PathRecordingRunner()
            stderr = io.StringIO()

            with patch("repomin.cli._build_runner", return_value=runner):
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            str(source),
                            "--command",
                            "reproduce",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--baseline-runs",
                            "2",
                            "--candidate-runs",
                            "2",
                            "--adapter",
                            "none",
                            "--source-reducer",
                            "none",
                            "--holdout-runs",
                            "3",
                            "--min-holdout-rate",
                            "0.2",
                            "--session",
                            str(session),
                            "--output",
                            str(output),
                        ]
                    )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertGreaterEqual(len(runner.paths), 9)
            self.assertEqual(
                {output.name},
                {path.name for path in runner.paths},
            )
            self.assertEqual(
                len(runner.paths),
                len({path.parent for path in runner.paths}),
            )
            self.assertTrue(all(not path.parent.exists() for path in runner.paths))
            self.assertEqual(
                {"current"},
                {path.name for path in (session / "workspace").iterdir()},
            )
            self.assertFalse((output / "unused.txt").exists())

            state = json.loads(
                (session / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "host-output-basename-v1",
                state["identity"]["working_directory_policy"],
            )
            self.assertEqual(
                output.name,
                state["identity"]["working_directory_basename"],
            )
            execution = _report(output)["execution"]
            self.assertEqual(
                "host-output-basename-v1",
                execution["working_directory_policy"],
            )
            self.assertEqual(
                output.name,
                execution["working_directory_basename"],
            )

    @unittest.skipUnless(os.name == "posix", "requires POSIX process signals")
    def test_certified_resume_exports_a_basename_sensitive_host_reproduction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "exported-output"
            changed_output = root / "different-output"
            source.mkdir()
            (source / "unused.txt").write_text("unused\n", encoding="utf-8")
            script = (
                "from pathlib import Path; import sys; "
                "name=Path.cwd().name; "
                "print('ORIGINAL_FAILURE' if name == 'exported-output' "
                "else 'WRONG_CWD:' + name, file=sys.stderr); "
                "raise SystemExit(1 if name == 'exported-output' else 2)"
            )
            argv = [sys.executable, "-c", script]
            command = (
                subprocess.list2cmdline(argv)
                if os.name == "nt"
                else shlex.join(argv)
            )
            arguments = [
                str(source),
                "--command",
                command,
                "--match",
                "ORIGINAL_FAILURE",
                "--baseline-runs",
                "1",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--holdout-runs",
                "3",
                "--min-holdout-rate",
                "0.2",
                "--session",
                str(session),
                "--output",
                str(output),
            ]

            first_stderr = io.StringIO()
            with patch(
                "repomin.cli.ReductionSession.export",
                side_effect=KeyboardInterrupt,
            ):
                with contextlib.redirect_stderr(first_stderr):
                    first_exit = main(arguments)

            self.assertEqual(130, first_exit, first_stderr.getvalue())
            self.assertFalse(output.exists())
            state_path = session / "state.json"
            original_state_text = state_path.read_text(encoding="utf-8")
            state = json.loads(original_state_text)
            self.assertEqual("certified", state["holdout_certification"]["status"])
            self.assertGreaterEqual(state["stats"]["accepted"], 1)

            changed_arguments = list(arguments)
            changed_arguments[-1] = str(changed_output)
            changed_stderr = io.StringIO()
            with contextlib.redirect_stderr(changed_stderr):
                changed_exit = main(changed_arguments + ["--resume"])
            self.assertEqual(2, changed_exit)
            self.assertIn("session configuration changed", changed_stderr.getvalue())
            self.assertFalse(changed_output.exists())

            legacy_state = json.loads(original_state_text)
            legacy_state["identity"].pop("working_directory_policy")
            legacy_state["identity"].pop("working_directory_basename")
            state_path.write_text(
                json.dumps(legacy_state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            legacy_stderr = io.StringIO()
            with contextlib.redirect_stderr(legacy_stderr):
                legacy_exit = main(arguments + ["--resume"])
            self.assertEqual(2, legacy_exit)
            self.assertIn("session configuration changed", legacy_stderr.getvalue())
            state_path.write_text(original_state_text, encoding="utf-8")

            resumed_stderr = io.StringIO()
            with contextlib.redirect_stderr(resumed_stderr):
                resumed_exit = main(arguments + ["--resume"])

            self.assertEqual(0, resumed_exit, resumed_stderr.getvalue())
            self.assertFalse((output / "unused.txt").exists())
            reproduced = subprocess.run(
                argv,
                cwd=output,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(1, reproduced.returncode)
            self.assertIn("ORIGINAL_FAILURE", reproduced.stderr)
            self.assertNotIn("WRONG_CWD", reproduced.stderr)

    def test_failed_persistent_holdout_is_terminal_and_does_not_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "output"
            source.mkdir()
            arguments = [
                str(source),
                "--command",
                "reproduce",
                "--match",
                "ORIGINAL_FAILURE",
                "--baseline-runs",
                "1",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--holdout-runs",
                "5",
                "--min-holdout-rate",
                "0.5",
                "--session",
                str(session),
                "--output",
                str(output),
            ]
            first_runner = _PrefixPassRunner(passing_calls=2)
            first_stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=first_runner):
                with contextlib.redirect_stderr(first_stderr):
                    first_exit = main(arguments)

            self.assertEqual(3, first_exit)
            self.assertEqual(7, first_runner.calls)
            self.assertFalse(output.exists())
            self.assertIn("holdout certification failed: 0/5", first_stderr.getvalue())
            checkpoint = json.loads(
                (session / "state.json").read_text(encoding="utf-8")
            )
            certification = checkpoint["holdout_certification"]
            self.assertEqual("not_certified", certification["status"])
            self.assertEqual(5, certification["completed_runs"])
            self.assertEqual(0, certification["passes"])

            resumed_runner = _CheckpointRunner()
            resumed_stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=resumed_runner):
                with contextlib.redirect_stderr(resumed_stderr):
                    resumed_exit = main(arguments + ["--resume"])

            self.assertEqual(3, resumed_exit)
            self.assertEqual(0, resumed_runner.calls)
            self.assertFalse(output.exists())
            self.assertIn(
                "holdout certification failed: 0/5",
                resumed_stderr.getvalue(),
            )

    def test_resume_reuses_certified_export_and_rebuilds_missing_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "output"
            source.mkdir()
            arguments = [
                str(source),
                "--command",
                "reproduce",
                "--match",
                "ORIGINAL_FAILURE",
                "--baseline-runs",
                "1",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--holdout-runs",
                "3",
                "--min-holdout-rate",
                "0.2",
                "--session",
                str(session),
                "--output",
                str(output),
            ]

            first_runner = _CheckpointRunner()
            with patch("repomin.cli._build_runner", return_value=first_runner):
                with patch("repomin.cli.write_report", side_effect=KeyboardInterrupt):
                    first_exit = main(arguments)

            self.assertEqual(130, first_exit)
            self.assertEqual(5, first_runner.calls)
            self.assertTrue(output.is_dir())
            self.assertFalse(_metadata_output(output).exists())

            resumed_runner = _CheckpointRunner()
            with patch("repomin.cli._build_runner", return_value=resumed_runner):
                resumed_exit = main(arguments + ["--resume"])

            self.assertEqual(0, resumed_exit)
            self.assertEqual(0, resumed_runner.calls)
            self.assertEqual("certified", _report(output)["holdout_certification"]["status"])

    def test_resume_accepts_complete_sidecar_written_before_checkpoint_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "output"
            source.mkdir()
            arguments = [
                str(source),
                "--command",
                "reproduce",
                "--match",
                "ORIGINAL_FAILURE",
                "--baseline-runs",
                "1",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--holdout-runs",
                "3",
                "--min-holdout-rate",
                "0.2",
                "--session",
                str(session),
                "--output",
                str(output),
            ]

            first_runner = _CheckpointRunner()
            with patch("repomin.cli._build_runner", return_value=first_runner):
                with patch(
                    "repomin.cli.ReductionSession.mark_completed",
                    side_effect=KeyboardInterrupt,
                ):
                    first_exit = main(arguments)

            self.assertEqual(130, first_exit)
            self.assertTrue(output.is_dir())
            self.assertTrue((_metadata_output(output) / "report.json").is_file())

            resumed_runner = _CheckpointRunner()
            with patch("repomin.cli._build_runner", return_value=resumed_runner):
                resumed_exit = main(arguments + ["--resume"])

            self.assertEqual(0, resumed_exit)
            self.assertEqual(0, resumed_runner.calls)

    def test_resume_rejects_a_mismatched_certified_export_without_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "output"
            source.mkdir()
            arguments = [
                str(source),
                "--command",
                "reproduce",
                "--match",
                "ORIGINAL_FAILURE",
                "--baseline-runs",
                "1",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--holdout-runs",
                "3",
                "--min-holdout-rate",
                "0.2",
                "--session",
                str(session),
                "--output",
                str(output),
            ]

            with patch("repomin.cli._build_runner", return_value=_CheckpointRunner()):
                with patch("repomin.cli.write_report", side_effect=KeyboardInterrupt):
                    self.assertEqual(130, main(arguments))
            (output / "unexpected.txt").write_text("changed\n", encoding="utf-8")

            resumed_runner = _CheckpointRunner()
            stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=resumed_runner):
                with contextlib.redirect_stderr(stderr):
                    resumed_exit = main(arguments + ["--resume"])

            self.assertEqual(2, resumed_exit)
            self.assertEqual(0, resumed_runner.calls)
            self.assertIn("differs from the certified artifact", stderr.getvalue())

    def test_resume_rejects_a_partial_certified_sidecar_without_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "output"
            source.mkdir()
            arguments = [
                str(source),
                "--command",
                "reproduce",
                "--match",
                "ORIGINAL_FAILURE",
                "--baseline-runs",
                "1",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--holdout-runs",
                "3",
                "--min-holdout-rate",
                "0.2",
                "--session",
                str(session),
                "--output",
                str(output),
            ]

            with patch("repomin.cli._build_runner", return_value=_CheckpointRunner()):
                with patch("repomin.cli.write_report", side_effect=KeyboardInterrupt):
                    self.assertEqual(130, main(arguments))
            metadata = _metadata_output(output)
            metadata.mkdir()
            (metadata / "report.json").write_text("{}\n", encoding="utf-8")

            resumed_runner = _CheckpointRunner()
            stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=resumed_runner):
                with contextlib.redirect_stderr(stderr):
                    resumed_exit = main(arguments + ["--resume"])

            self.assertEqual(2, resumed_exit)
            self.assertEqual(0, resumed_runner.calls)
            self.assertIn("metadata output is incomplete", stderr.getvalue())

    def test_resume_rejects_existing_output_before_more_holdout_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "output"
            source.mkdir()
            arguments = [
                str(source),
                "--command",
                "reproduce",
                "--match",
                "ORIGINAL_FAILURE",
                "--baseline-runs",
                "1",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--holdout-runs",
                "3",
                "--min-holdout-rate",
                "0.2",
                "--session",
                str(session),
                "--output",
                str(output),
            ]

            first_runner = _CheckpointRunner(interrupt_at=3)
            with patch("repomin.cli._build_runner", return_value=first_runner):
                self.assertEqual(130, main(arguments))
            output.mkdir()

            resumed_runner = _CheckpointRunner()
            stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=resumed_runner):
                with contextlib.redirect_stderr(stderr):
                    resumed_exit = main(arguments + ["--resume"])

            self.assertEqual(2, resumed_exit)
            self.assertEqual(0, resumed_runner.calls)
            self.assertIn("output already exists", stderr.getvalue())

    def test_repeated_oracle_samples_are_recorded_in_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--baseline-runs",
                        "3",
                        "--min-baseline-passes",
                        "2",
                        "--candidate-runs",
                        "3",
                        "--min-candidate-passes",
                        "2",
                        "--adapter",
                        "none",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            report = _report(output)
            execution = report["execution"]
            self.assertEqual(3, execution["baseline_runs"])
            self.assertEqual(3, execution["baseline_passes"])
            self.assertIsNone(execution["baseline_rate_evidence_runs"])
            self.assertIsNone(execution["baseline_rate_evidence_passes"])
            self.assertIsNone(execution["baseline_exact_lower_bound"])
            self.assertIsNone(execution["baseline_exact_p_value"])
            self.assertIsNone(execution["baseline_exact_rate_gate_passed"])
            self.assertEqual(3, execution["candidate_runs"])
            self.assertEqual(2, execution["candidate_min_passes"])
            self.assertEqual(
                "jeffreys-mixture-cs-exact-terminal-signature-split-v3",
                execution["candidate_sampling_policy"],
            )
            self.assertEqual(
                "hierarchical-fixed-point-v2", execution["reduction_strategy"]
            )
            self.assertGreaterEqual(execution["candidate_samples"], 3)
            self.assertGreater(execution["candidate_passes"], 0)
            self.assertLessEqual(execution["candidate_passes"], execution["candidate_samples"])
            self.assertGreater(execution["candidate_early_rejections"], 0)
            self.assertGreater(execution["candidate_early_acceptances"], 0)
            self.assertGreater(execution["candidate_samples_saved"], 0)
            self.assertEqual(3, execution["final_runs"])
            self.assertEqual(3, execution["final_passes"])
            self.assertFalse(execution["cache_enabled"])
            phase_statistics = report["phase_statistics"]
            self.assertEqual("complete", phase_statistics["coverage"])
            self.assertEqual(
                "sum-run-result-duration-v1",
                phase_statistics["oracle_time_accounting"],
            )
            phases = phase_statistics["phases"]
            self.assertEqual(["files"], [phase["phase"] for phase in phases])
            phase = phases[0]
            self.assertEqual(report["attempts"], phase["attempts"])
            self.assertEqual(
                phase["attempts"],
                phase["no_op"]
                + phase["rejected"]
                + phase["accepted"]
                + phase["superseded"]
                + phase["aborted"],
            )
            self.assertEqual(
                phase["oracle_sample_uses"],
                phase["oracle_samples"] + phase["cache_hits"],
            )
            self.assertTrue(
                any(
                    event["oracle_early_acceptance"]
                    and event["oracle_runs"] < execution["candidate_runs"]
                    for event in report["events"]
                )
            )

    def test_rate_oracle_configuration_and_bounds_are_recorded_in_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--baseline-runs",
                        "3",
                        "--candidate-runs",
                        "5",
                        "--min-baseline-rate",
                        "0.5",
                        "--min-candidate-rate",
                        "0.2",
                        "--confidence",
                        "0.8",
                        "--adapter",
                        "none",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            report = _report(output)
            execution = report["execution"]
            self.assertEqual(0.5, execution["min_baseline_rate"])
            self.assertEqual(0.2, execution["min_candidate_rate"])
            self.assertEqual(0.8, execution["confidence"])
            self.assertEqual(1, execution["candidate_min_passes"])
            self.assertEqual(1.0, execution["baseline_rate"])
            self.assertGreater(execution["baseline_lower_bound"], 0.5)
            self.assertEqual(3, execution["baseline_rate_evidence_runs"])
            self.assertEqual(3, execution["baseline_rate_evidence_passes"])
            self.assertEqual(
                clopper_pearson_lower_bound(3, 3, 0.8),
                execution["baseline_exact_lower_bound"],
            )
            self.assertEqual(
                float(exact_binomial_upper_tail(3, 3, 0.5)),
                execution["baseline_exact_p_value"],
            )
            self.assertTrue(execution["baseline_exact_rate_gate_passed"])
            self.assertEqual(1.0, execution["final_rate"])
            self.assertGreater(execution["final_lower_bound"], 0.2)
            self.assertGreater(execution["candidate_early_rejections"], 0)
            self.assertGreater(execution["candidate_early_acceptances"], 0)
            self.assertGreater(execution["candidate_samples_saved"], 0)
            self.assertTrue(report["events"])
            self.assertTrue(
                all(event["oracle_early_acceptance"] for event in report["events"])
            )
            self.assertTrue(
                all(event["oracle_runs"] < 5 for event in report["events"])
            )
            self.assertTrue(
                all(event["oracle_lower_bound"] > 0.2 for event in report["events"])
            )
            self.assertTrue(
                all(
                    event["oracle_anytime_lower_bound"] > 0.2
                    for event in report["events"]
                )
            )

    def test_run_confidence_is_reported_and_reused_for_final_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")
            observed_confidences = []
            original = FailureOracle.accepts_repeated

            def tracking_accepts(oracle, *args, **kwargs):
                observed_confidences.append(kwargs.get("confidence"))
                return original(oracle, *args, **kwargs)

            stderr = io.StringIO()
            with patch.object(
                FailureOracle,
                "accepts_repeated",
                new=tracking_accepts,
            ):
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            str(source),
                            "--command",
                            "python3 reproduce.py",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--candidate-runs",
                            "10",
                            "--min-candidate-rate",
                            "0.2",
                            "--confidence",
                            "0.5",
                            "--run-confidence",
                            "0.5",
                            "--adapter",
                            "none",
                            "--source-reducer",
                            "none",
                            "--output",
                            str(output),
                        ]
                    )

            self.assertEqual(0, exit_code, stderr.getvalue())
            report = _report(output)
            execution = report["execution"]
            self.assertEqual(0.5, execution["run_confidence"])
            self.assertEqual(
                "harmonic-alpha-spending-v1",
                execution["candidate_family_control_policy"],
            )
            self.assertGreater(execution["candidate_family_count"], 0)
            self.assertGreater(
                execution["candidate_family_alpha_upper_bound"],
                0.0,
            )
            self.assertTrue(report["events"])
            for event in report["events"]:
                self.assertIsNotNone(event["candidate_family_index"])
                self.assertIsNotNone(event["candidate_confidence"])
                self.assertIsNotNone(event["candidate_alpha"])
            self.assertEqual(
                report["events"][-1]["candidate_confidence"],
                observed_confidences[-1],
            )

    def test_refuses_output_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "false",
                        "--match",
                        "failure",
                        "--output",
                        str(source / "result"),
                    ]
                )

            self.assertEqual(2, exit_code)
            self.assertIn("must not be inside", stderr.getvalue())

    def test_refuses_overlapping_session_output_and_metadata_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            cases = (
                (
                    root / "session-output-inside",
                    root
                    / "session-output-inside"
                    / "workspace"
                    / "current"
                    / "nested"
                    / "output",
                ),
                (
                    root / "output-session-inside" / "checkpoint",
                    root / "output-session-inside",
                ),
                (
                    root / "metadata-session-inside.repomin" / "checkpoint",
                    root / "metadata-session-inside",
                ),
            )
            for session, output in cases:
                with self.subTest(session=session, output=output):
                    stderr = io.StringIO()
                    with patch("repomin.cli._build_runner") as build_runner:
                        with contextlib.redirect_stderr(stderr):
                            exit_code = main(
                                [
                                    str(source),
                                    "--command",
                                    "false",
                                    "--match",
                                    "failure",
                                    "--session",
                                    str(session),
                                    "--output",
                                    str(output),
                                ]
                            )

                    self.assertEqual(2, exit_code)
                    self.assertIn("must not overlap", stderr.getvalue())
                    build_runner.assert_not_called()
                    self.assertFalse(session.exists())
                    self.assertFalse(output.exists())
                    self.assertFalse(_metadata_output(output).exists())

    def test_rejects_zero_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "false",
                        "--match",
                        "failure",
                        "--jobs",
                        "0",
                    ]
                )

            self.assertEqual(2, exit_code)
            self.assertIn("jobs must be at least 1", stderr.getvalue())

    def test_rejects_invalid_sample_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            for arguments, message in (
                (["--candidate-runs", "0"], "candidate runs must be at least 1"),
                (
                    ["--baseline-runs", "2", "--min-baseline-passes", "3"],
                    "minimum baseline passes",
                ),
                (
                    ["--candidate-runs", "1", "--min-candidate-rate", "0.2"],
                    "minimum candidate rate 0.2 is unattainable",
                ),
                (
                    [
                        "--baseline-runs",
                        "1",
                        "--min-baseline-rate",
                        "0.01",
                        "--python-exception",
                    ],
                    "minimum baseline rate 0.01 is unattainable",
                ),
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            str(source),
                            "--command",
                            "false",
                            "--match",
                            "failure",
                        ]
                        + arguments
                    )
                self.assertEqual(2, exit_code)
                self.assertIn(message, stderr.getvalue())

    def test_resume_requires_a_persistent_session_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "false",
                        "--match",
                        "failure",
                        "--resume",
                    ]
                )

            self.assertEqual(2, exit_code)
            self.assertIn("--resume requires --session PATH", stderr.getvalue())

    def test_resume_rejects_a_changed_docker_image_resolution_before_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "output"
            source.mkdir()
            arguments = [
                str(source),
                "--command",
                "reproduce",
                "--match",
                "ORIGINAL_FAILURE",
                "--baseline-runs",
                "1",
                "--backend",
                "docker",
                "--docker-image",
                "fixture:mutable",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--session",
                str(session),
                "--output",
                str(output),
            ]
            first_runner = _CheckpointRunner(interrupt_at=2)
            first_runner.resolved_image_id = "sha256:" + "a" * 64
            with patch("repomin.cli._build_runner", return_value=first_runner):
                self.assertEqual(130, main(arguments))

            resumed_runner = _CheckpointRunner()
            resumed_runner.resolved_image_id = "sha256:" + "b" * 64
            stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=resumed_runner):
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(arguments + ["--resume"])

            self.assertEqual(2, exit_code)
            self.assertEqual(0, resumed_runner.calls)
            self.assertIn("session configuration changed", stderr.getvalue())

    def test_cli_can_resume_after_keyboard_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "output"
            source.mkdir()
            for index in range(4):
                (source / ("unused-%d.txt" % index)).write_text(
                    "unused\n", encoding="utf-8"
                )

            first_runner = _CheckpointRunner(interrupt_at=2)
            first_stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=first_runner):
                with contextlib.redirect_stderr(first_stderr):
                    first_exit = main(
                        [
                            str(source),
                            "--command",
                            "reproduce",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--baseline-runs",
                            "1",
                            "--min-baseline-rate",
                            "0.04",
                            "--min-candidate-rate",
                            "0.04",
                            "--adapter",
                            "none",
                            "--source-reducer",
                            "none",
                            "--session",
                            str(session),
                            "--output",
                            str(output),
                        ]
                    )
            self.assertEqual(130, first_exit)
            self.assertIn("checkpoint retained", first_stderr.getvalue())
            self.assertTrue((session / "state.json").is_file())
            checkpoint = json.loads(
                (session / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(0.04, checkpoint["identity"]["min_baseline_rate"])
            self.assertEqual(0.04, checkpoint["oracle"]["min_candidate_rate"])
            self.assertEqual(
                "jeffreys-mixture-cs-exact-terminal-signature-split-v3",
                checkpoint["identity"]["candidate_sampling_policy"],
            )
            self.assertGreater(checkpoint["stats"]["baseline_lower_bound"], 0.04)
            for container in (checkpoint["stats"], checkpoint["oracle"]):
                self.assertEqual(1, container["baseline_rate_evidence_runs"])
                self.assertEqual(1, container["baseline_rate_evidence_passes"])
                self.assertEqual(
                    clopper_pearson_lower_bound(1, 1, 0.95),
                    container["baseline_exact_lower_bound"],
                )
                self.assertEqual(
                    float(exact_binomial_upper_tail(1, 1, 0.04)),
                    container["baseline_exact_p_value"],
                )
                self.assertTrue(container["baseline_exact_rate_gate_passed"])
            self.assertIn("candidate_samples_saved", checkpoint["stats"])
            self.assertIn("candidate_early_acceptances", checkpoint["stats"])
            self.assertFalse(output.exists())

            second_runner = _CheckpointRunner()
            second_stderr = io.StringIO()
            with patch("repomin.cli._build_runner", return_value=second_runner):
                with contextlib.redirect_stderr(second_stderr):
                    second_exit = main(
                        [
                            str(source),
                            "--command",
                            "reproduce",
                            "--match",
                            "ORIGINAL_FAILURE",
                            "--baseline-runs",
                            "1",
                            "--min-baseline-rate",
                            "0.04",
                            "--min-candidate-rate",
                            "0.04",
                            "--adapter",
                            "none",
                            "--source-reducer",
                            "none",
                            "--session",
                            str(session),
                            "--resume",
                            "--output",
                            str(output),
                        ]
                    )
            self.assertEqual(0, second_exit, second_stderr.getvalue())
            self.assertTrue((_metadata_output(output) / "REPOMIN.md").is_file())
            report = _report(output)
            self.assertTrue(report["execution"]["resumed"])
            self.assertEqual(str(session.resolve()), report["execution"]["session_path"])
            self.assertEqual(0.04, report["execution"]["min_candidate_rate"])
            self.assertEqual(1, report["execution"]["baseline_rate_evidence_runs"])
            self.assertEqual(1, report["execution"]["baseline_rate_evidence_passes"])
            self.assertTrue(
                report["execution"]["baseline_exact_rate_gate_passed"]
            )
            self.assertGreater(report["execution"]["final_lower_bound"], 0.04)

    def test_resume_rejects_changed_java_analysis_classpath_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "output"
            classpath_entry = root / "dependency.jar"
            source.mkdir()
            (source / "unused.txt").write_text("unused\n", encoding="utf-8")
            classpath_entry.write_bytes(b"first dependency contents")
            common_args = [
                str(source),
                "--command",
                "reproduce",
                "--match",
                "ORIGINAL_FAILURE",
                "--baseline-runs",
                "1",
                "--adapter",
                "none",
                "--source-reducer",
                "none",
                "--java-classpath",
                str(classpath_entry),
                "--session",
                str(session),
                "--output",
                str(output),
            ]

            with patch(
                "repomin.cli._build_runner",
                return_value=_CheckpointRunner(interrupt_at=2),
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(130, main(common_args))

            state = json.loads((session / "state.json").read_text(encoding="utf-8"))
            saved_classpath = state["identity"]["java_analysis_classpath"]
            self.assertEqual(str(classpath_entry.resolve()), saved_classpath[0]["path"])
            self.assertEqual("file", saved_classpath[0]["kind"])

            classpath_entry.write_bytes(b"changed dependency contents")
            stderr = io.StringIO()
            with patch(
                "repomin.cli._build_runner",
                return_value=_CheckpointRunner(),
            ):
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(common_args + ["--resume"])

            self.assertEqual(2, exit_code)
            self.assertIn("session configuration changed", stderr.getvalue())

    def test_resume_rejects_changed_java_classpath_paths_or_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            session = root / "session"
            output = root / "output"
            first = root / "first.jar"
            second = root / "second.jar"
            replacement = root / "replacement.jar"
            source.mkdir()
            (source / "unused.txt").write_text("unused\n", encoding="utf-8")
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            replacement.write_bytes(second.read_bytes())

            def arguments(classpath: list) -> list:
                result = [
                    str(source),
                    "--command",
                    "reproduce",
                    "--match",
                    "ORIGINAL_FAILURE",
                    "--baseline-runs",
                    "1",
                    "--adapter",
                    "none",
                    "--source-reducer",
                    "none",
                    "--session",
                    str(session),
                    "--output",
                    str(output),
                ]
                for entry in classpath:
                    result.extend(["--java-classpath", str(entry)])
                return result

            with patch(
                "repomin.cli._build_runner",
                return_value=_CheckpointRunner(interrupt_at=2),
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(130, main(arguments([first, second])))

            for changed in ([second, first], [first, replacement]):
                with self.subTest(classpath=changed):
                    stderr = io.StringIO()
                    with patch(
                        "repomin.cli._build_runner",
                        return_value=_CheckpointRunner(),
                    ):
                        with contextlib.redirect_stderr(stderr):
                            exit_code = main(arguments(changed) + ["--resume"])
                    self.assertEqual(2, exit_code)
                    self.assertIn("session configuration changed", stderr.getvalue())

    def test_java_exception_signature_prevents_broad_regex_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(
                JAVA_EXCEPTION_SCRIPT, encoding="utf-8"
            )
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "NoSuchMethodError",
                        "--java-exception",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertTrue((output / "required.txt").exists())
            self.assertFalse((output / "unused.txt").exists())
            report = _report(output)
            signature = report["java_exception_signature"]
            self.assertEqual("java.lang.NoSuchMethodError", signature["class"])
            self.assertEqual("demo.Target.missing()", signature["message"])
            self.assertEqual(["demo.Trigger.run"], signature["frames"])
            self.assertIn("Preserving Java exception", stderr.getvalue())

    def test_docker_backend_requires_an_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "false",
                        "--match",
                        "failure",
                        "--backend",
                        "docker",
                    ]
                )

            self.assertEqual(2, exit_code)
            self.assertIn("requires --docker-image", stderr.getvalue())

    def test_docker_resource_option_requires_docker_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "false",
                        "--match",
                        "failure",
                        "--docker-memory",
                        "64MiB",
                    ]
                )

            self.assertEqual(2, exit_code)
            self.assertIn("--docker-memory requires --backend docker", stderr.getvalue())

    def test_python_exception_signature_prevents_broad_regex_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(
                PYTHON_EXCEPTION_SCRIPT, encoding="utf-8"
            )
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ValueError",
                        "--python-exception",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertTrue((output / "required.txt").exists())
            self.assertFalse((output / "unused.txt").exists())
            report = _report(output)
            signature = report["python_exception_signature"]
            self.assertEqual("ValueError", signature["class"])
            self.assertEqual("payment failed", signature["message"])
            self.assertEqual(
                ["reproduce.py:target_failure", "reproduce.py:<module>"],
                signature["frames"],
            )
            self.assertIn("Preserving Python exception", stderr.getvalue())
            reproduction = (_metadata_output(output) / "REPOMIN.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Expected Python exception", reproduction)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process signals")
    def test_process_failure_reduces_a_silent_native_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "crash.py").write_text(
                PROCESS_FAILURE_SCRIPT,
                encoding="utf-8",
            )
            (source / "required.txt").write_text("keep\n", encoding="utf-8")
            (source / "unused.txt").write_text("remove\n", encoding="utf-8")
            command = "exec %s crash.py" % shlex.quote(sys.executable)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        command,
                        "--process-failure",
                        "--adapter",
                        "none",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertTrue((output / "required.txt").exists())
            self.assertFalse((output / "unused.txt").exists())
            report = _report(output)
            self.assertIsNone(report["failure_match"])
            self.assertEqual(
                {
                    "kind": "posix_signal",
                    "code": int(signal.SIGABRT),
                    "name": "SIGABRT",
                },
                report["process_failure_signature"],
            )
            self.assertIn("Preserving process failure", stderr.getvalue())
            reproduction = (_metadata_output(output) / "REPOMIN.md").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("Expected output match", reproduction)
            self.assertIn("Expected process failure", reproduction)

            final = subprocess.run(
                ["/bin/sh", "-c", command],
                cwd=str(output),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(-int(signal.SIGABRT), final.returncode)

    def test_python_source_reducer_runs_through_cli_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(PYTHON_SOURCE_SCRIPT, encoding="utf-8")
            (source / "required.txt").write_text("keep\n", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "none",
                        "--source-reducer",
                        "python",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            script = (output / "reproduce.py").read_text(encoding="utf-8")
            self.assertIn("def target", script)
            self.assertNotIn("def unused", script)
            report = _report(output)
            self.assertIn(
                "python-source", [event["phase"] for event in report["events"]]
            )

    def test_gradle_adapter_runs_through_cli_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "settings.gradle.kts").write_text(
                'include(":app", ":unused")\n', encoding="utf-8"
            )
            (source / "reproduce.py").write_text(GRADLE_SCRIPT, encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "gradle",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            settings = (output / "settings.gradle.kts").read_text(encoding="utf-8")
            self.assertIn(":app", settings)
            self.assertNotIn(":unused", settings)
            report = _report(output)
            self.assertIn("gradle", [event["phase"] for event in report["events"]])

    def test_python_adapter_runs_through_cli_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "pyproject.toml").write_text(
                '[project]\ndependencies = ["fastapi>=0.100", "unused>=1"]\n',
                encoding="utf-8",
            )
            (source / "reproduce.py").write_text(
                PYTHON_MANIFEST_SCRIPT, encoding="utf-8"
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "python",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            manifest = (output / "pyproject.toml").read_text(encoding="utf-8")
            self.assertIn("fastapi>=0.100", manifest)
            self.assertNotIn("unused>=1", manifest)
            report = _report(output)
            self.assertIn(
                "python-manifest", [event["phase"] for event in report["events"]]
            )

    def test_node_adapter_runs_through_cli_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "package.json").write_text(
                '{"name":"fixture","dependencies":{"required":"1","unused":"2"}}\n',
                encoding="utf-8",
            )
            (source / "reproduce.py").write_text(
                "import json\n"
                "package = json.load(open('package.json', encoding='utf-8'))\n"
                "if package['dependencies'].get('required') != '1':\n"
                "    raise SystemExit(2)\n"
                "print('ORIGINAL_FAILURE')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "node",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            manifest = json.loads(
                (output / "package.json").read_text(encoding="utf-8")
            )
            self.assertEqual("1", manifest["dependencies"]["required"])
            self.assertNotIn("unused", manifest["dependencies"])
            report = _report(output)
            self.assertIn("node-manifest", [event["phase"] for event in report["events"]])

    def test_composer_adapter_runs_through_cli_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "composer.json").write_text(
                '{"name":"fixture","require":{"required":"1","unused":"2"}}\n',
                encoding="utf-8",
            )
            (source / "reproduce.py").write_text(
                "import json\n"
                "manifest = json.load(open('composer.json', encoding='utf-8'))\n"
                "if manifest['require'].get('required') != '1':\n"
                "    raise SystemExit(2)\n"
                "print('ORIGINAL_FAILURE')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "composer",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            manifest = json.loads(
                (output / "composer.json").read_text(encoding="utf-8")
            )
            self.assertEqual("1", manifest["require"]["required"])
            self.assertNotIn("unused", manifest["require"])
            report = _report(output)
            self.assertIn(
                "composer-manifest", [event["phase"] for event in report["events"]]
            )

    def test_dotnet_adapter_runs_through_cli_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "fixture.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk">\n'
                "  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n"
                "  <ItemGroup>\n"
                '    <PackageReference Include="Required" Version="1" />\n'
                '    <PackageReference Include="Unused" Version="2" />\n'
                "  </ItemGroup>\n"
                "</Project>\n",
                encoding="utf-8",
            )
            (source / "reproduce.py").write_text(
                "from pathlib import Path\n"
                "text = Path('fixture.csproj').read_text(encoding='utf-8')\n"
                "if 'Include=\"Required\"' not in text:\n"
                "    raise SystemExit(2)\n"
                "print('ORIGINAL_FAILURE')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "dotnet",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            manifest = (output / "fixture.csproj").read_text(encoding="utf-8")
            self.assertIn('Include="Required"', manifest)
            self.assertNotIn('Include="Unused"', manifest)
            report = _report(output)
            self.assertIn(
                "dotnet-manifest", [event["phase"] for event in report["events"]]
            )

    def test_ruby_adapter_runs_through_cli_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "Gemfile").write_text(
                'source "https://rubygems.org"\n'
                'gem "required"\n'
                'gem "unused"\n',
                encoding="utf-8",
            )
            (source / "reproduce.rb").write_text(
                "text = File.read('Gemfile')\n"
                "unless text.include?('gem \"required\"')\n"
                "  exit 2\n"
                "end\n"
                "puts 'ORIGINAL_FAILURE'\n"
                "exit 1\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "ruby reproduce.rb",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "ruby",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            manifest = (output / "Gemfile").read_text(encoding="utf-8")
            self.assertIn('gem "required"', manifest)
            self.assertNotIn('gem "unused"', manifest)
            report = _report(output)
            self.assertIn(
                "ruby-manifest", [event["phase"] for event in report["events"]]
            )

    def test_cargo_adapter_runs_through_cli_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "Cargo.toml").write_text(
                "[package]\nname = 'fixture'\nversion = '0.1.0'\n\n"
                "[dependencies]\nrequired = '1'\nunused = '2'\n",
                encoding="utf-8",
            )
            (source / "reproduce.py").write_text(
                "text = open('Cargo.toml', encoding='utf-8').read()\n"
                "if \"required = '1'\" not in text:\n"
                "    raise SystemExit(2)\n"
                "print('ORIGINAL_FAILURE')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "cargo",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            manifest = (output / "Cargo.toml").read_text(encoding="utf-8")
            self.assertIn("required = '1'", manifest)
            self.assertNotIn("unused = '2'", manifest)
            report = _report(output)
            self.assertIn("cargo-manifest", [event["phase"] for event in report["events"]])

    def test_pipenv_adapter_runs_through_cli_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "Pipfile").write_text(
                "[[source]]\nurl = 'https://pypi.org/simple'\n\n"
                "[packages]\nrequired = '*'\nunused = '*'\n\n"
                "[dev-packages]\nunused-test = '*'\n\n"
                "[requires]\npython_version = '3.11'\n",
                encoding="utf-8",
            )
            (source / "reproduce.py").write_text(
                "text = open('Pipfile', encoding='utf-8').read()\n"
                "if 'required = ' not in text:\n"
                "    raise SystemExit(2)\n"
                "print('ORIGINAL_FAILURE')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "pipenv",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            manifest = (output / "Pipfile").read_text(encoding="utf-8")
            self.assertIn("required = '*'", manifest)
            self.assertNotIn("unused = '*'", manifest)
            self.assertNotIn("unused-test = '*'", manifest)
            self.assertIn("[[source]]", manifest)
            report = _report(output)
            self.assertIn("pipenv-manifest", [event["phase"] for event in report["events"]])

    def test_forced_pipenv_adapter_requires_pipfile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "reproduce.py").write_text(
                "print('ORIGINAL_FAILURE')\nraise SystemExit(1)\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "pipenv",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(2, exit_code)
            self.assertIn("--adapter pipenv requires at least one Pipfile", stderr.getvalue())

    def test_go_adapter_runs_through_cli_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "go.mod").write_text(
                "module example.com/fixture\n\n"
                "go 1.22\n\n"
                "require (\n"
                "    example.com/required v1.0.0\n"
                "    example.com/unused v1.0.0\n"
                ")\n",
                encoding="utf-8",
            )
            (source / "reproduce.py").write_text(
                "text = open('go.mod', encoding='utf-8').read()\n"
                "if 'example.com/required' not in text:\n"
                "    raise SystemExit(2)\n"
                "print('ORIGINAL_FAILURE')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--command",
                        "python3 reproduce.py",
                        "--match",
                        "ORIGINAL_FAILURE",
                        "--adapter",
                        "go",
                        "--source-reducer",
                        "none",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code, stderr.getvalue())
            manifest = (output / "go.mod").read_text(encoding="utf-8")
            self.assertIn("example.com/required", manifest)
            self.assertNotIn("example.com/unused v1.0.0", manifest)
            report = _report(output)
            self.assertIn("go-manifest", [event["phase"] for event in report["events"]])


if __name__ == "__main__":
    unittest.main()
