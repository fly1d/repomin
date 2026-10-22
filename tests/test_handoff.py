"""A verified Doctor invocation can be copied into a bounded first reduction."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import repomin
from repomin.cli import _build_doctor_parser, build_parser, main
from repomin.config import expand_config_args
from repomin.handoff import format_reduction_command, reduction_arguments
from repomin.report import validate_report_file


_MESSAGE = "FAIL [checkout]: total (12)"


def _python_command(script: str) -> str:
    arguments = [sys.executable, script]
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def _capture(arguments: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


def _suggestion(output: str) -> str:
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Next: run a reduction"):
            return lines[index + 1].strip()
    raise AssertionError("Doctor did not print a copyable reduction command")


class HandoffTest(unittest.TestCase):
    def _fixture(self, root: Path, message: str = _MESSAGE) -> tuple[Path, Path, list[str]]:
        source = root / "project O'Brien with spaces"
        source.mkdir()
        script = "reproduce file.py"
        (source / script).write_text(
            "from pathlib import Path\n"
            "if Path('required.txt').exists():\n"
            "    print(%r)\n" % message
            + "else:\n    print('DIFFERENT_FAILURE')\nraise SystemExit(1)\n",
            encoding="utf-8",
        )
        (source / "required.txt").write_text("required\n", encoding="utf-8")
        (source / "noise.txt").write_text("unrelated\n", encoding="utf-8")
        output = root / "result O'Brien with spaces"
        arguments = [
            str(source), "--command", _python_command(script),
            "--match-text", message, "--exit-code", "1",
            "--adapter", "none", "--source-reducer", "none",
            "--baseline-runs", "1", "--keep", script, "--output", str(output),
        ]
        return source, output, arguments

    def _check_export(self, output: Path) -> dict:
        report = validate_report_file(
            output.with_name(output.name + ".repomin") / "report.json", output
        )
        self.assertTrue((output / "required.txt").is_file())
        self.assertFalse((output / "noise.txt").exists())
        self.assertEqual(25, report["execution"]["max_attempts"])
        self.assertEqual(300, report["execution"]["max_duration_seconds"])
        return report

    def test_ready_text_handoff_runs_a_bounded_reduction_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _source, output, arguments = self._fixture(Path(directory))
            code, text, errors = _capture(["doctor", *arguments])
            self.assertEqual(0, code, text + errors)
            self.assertIn("ready to reduce", text)
            expected = reduction_arguments(arguments, _build_doctor_parser())
            self.assertIsNotNone(expected)
            self.assertEqual(format_reduction_command(expected), _suggestion(text))
            code, text, errors = _capture(expected[1:])
            self.assertEqual(0, code, text + errors)
            self._check_export(output)

    def test_static_failed_and_shareable_formats_have_no_copyable_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, arguments = self._fixture(Path(directory))
            code, text, _errors = _capture(["doctor", str(source)])
            self.assertEqual(0, code)
            self.assertIn("failure not verified", text)
            self.assertNotIn("Next: run a reduction", text)
            wrong = list(arguments)
            wrong[wrong.index("--match-text") + 1] = "MISSING_TARGET"
            code, text, _errors = _capture(["doctor", *wrong])
            self.assertEqual(1, code)
            self.assertNotIn("Next: run a reduction", text)
            for controls in (["--json"], ["--format", "json"], ["--format=markdown"]):
                with self.subTest(controls=controls):
                    code, text, errors = _capture(["doctor", *arguments, *controls])
                    self.assertEqual(0, code, text + errors)
                    self.assertNotIn("Next: run a reduction", text)
                    self.assertNotIn("Start-Process", text)
                    if controls[-1] in {"--json", "json"}:
                        result = json.loads(text)
                        self.assertEqual("pass", result["baseline"]["status"])
                        self.assertNotIn("next_command", result)
                    else:
                        self.assertTrue(text.startswith("# ReproMin Doctor summary\n"))
                        self.assertNotIn(str(source), text)
                        self.assertNotIn(_MESSAGE, text)
            self.assertFalse(output.exists())

    def test_explicit_environment_suppresses_the_whole_command_and_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _source, _output, arguments = self._fixture(Path(directory))
            secret = "local-secret-$()-O'Brien"
            for controls in (
                ["--env", "HANDOFF_TEST_TOKEN=" + secret],
                ["--env=HANDOFF_TEST_TOKEN=" + secret],
                ["--en=HANDOFF_TEST_TOKEN=" + secret],
            ):
                with self.subTest(controls=controls):
                    supplied = [*arguments, *controls]
                    self.assertIsNone(reduction_arguments(supplied, _build_doctor_parser()))
                    code, text, errors = _capture(["doctor", *supplied])
                    self.assertEqual(0, code, text + errors)
                    self.assertIn("command is omitted when --env is used", text)
                    self.assertNotIn("Next: run a reduction", text)
                    self.assertNotIn(secret, text + errors)

    def test_config_handoff_keeps_the_reviewed_limits_without_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, arguments = self._fixture(root)
            config = root / "spec O'Brien.json"
            config.write_text(json.dumps({
                "schema_version": 1,
                "failure": {"command": arguments[2], "match": "FAIL", "exit_code": 1},
                "sampling": {"baseline_runs": 1},
                "reduction": {"adapter": "none", "source_reducer": "none",
                              "max_attempts": 7, "max_duration_seconds": 12},
            }), encoding="utf-8")
            supplied = [str(source), "--config", str(config), "--output", str(output)]
            code, text, errors = _capture(["doctor", *supplied])
            self.assertEqual(0, code, text + errors)
            handoff = reduction_arguments(supplied, _build_doctor_parser())
            self.assertEqual(format_reduction_command(handoff), _suggestion(text))
            self.assertFalse(any(value.startswith("--max-") for value in handoff))
            self.assertNotIn("--semantic-reducer=none", handoff)
            parsed = build_parser().parse_args(expand_config_args(handoff[2:], command="reduce"))
            self.assertEqual(7, parsed.max_attempts)
            self.assertEqual(12, parsed.max_duration)
            self.assertIn("--config=" + str(config), handoff)
            code, text, errors = _capture(handoff[1:])
            self.assertEqual(0, code, text + errors)
            report = validate_report_file(
                output.with_name(output.name + ".repomin") / "report.json", output
            )
            self.assertEqual(7, report["execution"]["max_attempts"])
            self.assertEqual(12, report["execution"]["max_duration_seconds"])

    def test_tokens_preserve_values_abbreviations_equals_and_delimiters(self) -> None:
        cases = [
            ["project", "--command=--json", "--match-text=--format", "--format=text"],
            ["project", "--comm=false", "--match-text=x", "--for", "text", "--exit-c=-9"],
            ["--command=false", "--match-text=--json", "--format=text", "--", "--env"],
            ["--command=false", "--match-text=--format", "--json", "--", "--format"],
            ["project", "--command", "false", "--match-text", "x", "--keep=a", "--keep", "b"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                doctor = _build_doctor_parser()
                original = doctor.parse_args(arguments)
                handoff = reduction_arguments(arguments, doctor)
                self.assertIsNotNone(handoff)
                reduced = build_parser().parse_args(handoff[2:])
                for name in ("source", "command", "match", "exit_code", "keep_paths"):
                    self.assertEqual(getattr(original, name), getattr(reduced, name))
                self.assertEqual(25, reduced.max_attempts)
                self.assertEqual(300, reduced.max_duration)

    @unittest.skipIf(os.name == "nt", "Bash/Zsh quoting is exercised on POSIX")
    def test_posix_shell_executes_hostile_looking_values_as_literal_data(self) -> None:
        shells = [path for name in ("bash", "zsh") if (path := shutil.which(name))]
        if not shells:
            self.skipTest("Bash and Zsh are unavailable")
        message = "FAIL [checkout]: (12) O'Brien $(touch injected-dollar) `touch injected-tick`"
        for shell in shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _source, output, arguments = self._fixture(root, message)
                code, text, errors = _capture(["doctor", *arguments])
                self.assertEqual(0, code, text + errors)
                environment = dict(os.environ)
                environment["PYTHONPATH"] = str(Path(repomin.__file__).resolve().parent.parent)
                completed = subprocess.run(
                    [shell, "-c", _suggestion(text)], cwd=root, env=environment,
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                report = self._check_export(output)
                self.assertEqual(arguments[2], report["command"])
                self.assertEqual(re.escape(message), report["failure_spec"]["match"])
                self.assertFalse((root / "injected-dollar").exists())
                self.assertFalse((root / "injected-tick").exists())

    @unittest.skipUnless(os.name == "nt", "Native Start-Process needs Windows")
    def test_native_powershell_executes_quoted_handoff_and_keeps_parent_alive(self) -> None:
        shells = [path for name in ("powershell", "pwsh") if (path := shutil.which(name))]
        if not shells:
            self.skipTest("PowerShell is unavailable")
        for shell in shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _source, output, arguments = self._fixture(root)
                self.assertIn('"', arguments[2])
                code, text, errors = _capture(["doctor", *arguments])
                self.assertEqual(0, code, text + errors)
                command = _suggestion(text)
                self.assertIn("Start-Process", command)
                environment = dict(os.environ)
                environment["PYTHONPATH"] = str(Path(repomin.__file__).resolve().parent.parent)
                completed = subprocess.run(
                    [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                     command + "; Write-Output 'REPOMIN_PARENT_ALIVE'"],
                    cwd=root, env=environment, capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertEqual(["0", "REPOMIN_PARENT_ALIVE"], completed.stdout.splitlines()[-2:])
                self._check_export(output)


if __name__ == "__main__":
    unittest.main()
