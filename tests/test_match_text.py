"""Literal failure messages retain their meaning through reduction and replay."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from repomin.cli import _build_doctor_parser, build_parser, main
from repomin.replay import replay_report
from repomin.report import validate_report_file


_FAILURE_TEXT = "FAIL [checkout]: total (12)"
# This would match the unescaped regular expression above, despite being a
# different message. Keep the same exit code to test the message identity.
_NEAR_MATCH = "FAIL c: total 12"
_REPRODUCER = """\
from pathlib import Path

if Path("required.txt").exists():
    print("FAIL [checkout]: total (12)")
else:
    print("FAIL c: total 12")
raise SystemExit(1)
"""


def _python_command(script: str) -> str:
    argv = [sys.executable, script]
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


class MatchTextTest(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        source = root / "project"
        source.mkdir()
        (source / "reproduce.py").write_text(_REPRODUCER, encoding="utf-8")
        (source / "required.txt").write_text("needed\n", encoding="utf-8")
        (source / "noise.txt").write_text("unrelated\n", encoding="utf-8")
        return source

    def _failure_arguments(self, source: Path) -> list[str]:
        return [
            str(source),
            "--command",
            _python_command("reproduce.py"),
            "--match-text",
            _FAILURE_TEXT,
            "--exit-code",
            "1",
            "--baseline-runs",
            "1",
            "--adapter",
            "none",
            "--source-reducer",
            "none",
        ]

    def test_doctor_accepts_the_literal_message_and_rejects_a_near_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._fixture(Path(directory))
            arguments = ["doctor", *self._failure_arguments(source), "--json"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(
                io.StringIO()
            ):
                code = main(arguments)
            self.assertEqual(0, code)
            diagnosis = json.loads(output.getvalue())
            self.assertEqual("pass", diagnosis["baseline"]["status"])

            (source / "required.txt").unlink()
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(
                io.StringIO()
            ):
                code = main(arguments)
            self.assertEqual(1, code)
            diagnosis = json.loads(output.getvalue())
            self.assertEqual("fail", diagnosis["baseline"]["status"])
            self.assertEqual(0, diagnosis["baseline"]["passes"])

    def test_reduction_report_and_replay_preserve_the_literal_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._fixture(root)
            output = root / "result"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                code = main(
                    [
                        "reduce",
                        *self._failure_arguments(source),
                        "--keep",
                        "reproduce.py",
                        "--max-attempts",
                        "20",
                        "--max-duration",
                        "10",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, code)
            self.assertEqual(
                ["reproduce.py", "required.txt"],
                sorted(path.name for path in output.iterdir()),
            )
            report_path = root / "result.repomin" / "report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            escaped = re.escape(_FAILURE_TEXT)
            self.assertEqual(escaped, report["failure_match"])
            self.assertEqual(escaped, report["failure_spec"]["match"])
            self.assertIsNotNone(re.search(escaped, _FAILURE_TEXT))
            self.assertIsNone(re.search(escaped, _NEAR_MATCH))
            validate_report_file(report_path, payload=output)
            reproduced, replay = replay_report(report_path, output, runs=2)
            self.assertTrue(reproduced)
            self.assertEqual(2, replay["passes"])

    def test_literal_parsing_preserves_surrounding_whitespace_and_unicode(self) -> None:
        message = "  结账失败 [总额]: (12)\\path\n"
        for parser in (build_parser(), _build_doctor_parser()):
            with self.subTest(parser=parser.prog):
                args = parser.parse_args(["--command", "false", "--match-text", message])
                self.assertEqual(re.escape(message), args.match)
                self.assertIsNotNone(re.search(args.match, message))
                self.assertIsNone(re.search(args.match, message.strip()))

    def test_existing_match_still_accepts_a_regular_expression(self) -> None:
        for parser in (build_parser(), _build_doctor_parser()):
            with self.subTest(parser=parser.prog):
                args = parser.parse_args(
                    ["--command", "false", "--match", r"FAIL [a-z]+: total \d+"]
                )
                self.assertEqual(r"FAIL [a-z]+: total \d+", args.match)
                self.assertIsNotNone(re.search(args.match, _NEAR_MATCH))

    def test_literal_and_regex_options_are_mutually_exclusive(self) -> None:
        for parser in (build_parser(), _build_doctor_parser()):
            for options in (
                ["--match", "FAIL", "--match-text", _FAILURE_TEXT],
                ["--match-text", _FAILURE_TEXT, "--match", "FAIL"],
            ):
                with self.subTest(parser=parser.prog, options=options):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as raised:
                            parser.parse_args(["--command", "false", *options])
                    self.assertEqual(2, raised.exception.code)
                    self.assertIn("not allowed with argument", stderr.getvalue())

    def test_literal_rejects_empty_whitespace_and_nul(self) -> None:
        for parser in (build_parser(), _build_doctor_parser()):
            for message in ("", " \t\n", "failure\x00message"):
                with self.subTest(parser=parser.prog, message=repr(message)):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as raised:
                            parser.parse_args(
                                ["--command", "false", "--match-text", message]
                            )
                    self.assertEqual(2, raised.exception.code)
                    self.assertIn("failure text must not", stderr.getvalue())

    def test_literal_remains_a_rejected_semantic_override_with_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._fixture(root)
            config = root / "spec.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "failure": {"command": "false", "match": "ORIGINAL"},
                    }
                ),
                encoding="utf-8",
            )
            for command in ("doctor", "reduce"):
                with self.subTest(command=command):
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                        stderr
                    ):
                        code = main(
                            [
                                command,
                                str(source),
                                "--config",
                                str(config),
                                "--match-text",
                                _FAILURE_TEXT,
                            ]
                        )
                    self.assertEqual(2, code)
                    self.assertIn("--config", stderr.getvalue())
                    self.assertIn("--match-text", stderr.getvalue())
            self.assertFalse((root / "project-minimal").exists())


if __name__ == "__main__":
    unittest.main()
