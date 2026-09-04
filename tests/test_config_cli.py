"""End-to-end tests for versioned reduction configuration files."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repomin.cli import main


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
    argv = [sys.executable, script]
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


class ConfigCliTest(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        source = root / "project"
        source.mkdir()
        (source / "reproduce.py").write_text(_REPRODUCER, encoding="utf-8")
        (source / "required.txt").write_text("required\n", encoding="utf-8")
        (source / "noise.txt").write_text("noise\n", encoding="utf-8")
        config = root / "reduction.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "failure": {
                        "command": _python_command("reproduce.py"),
                        "match": "ORIGINAL_FAILURE",
                        "exit_code": 7,
                    },
                    "execution": {"timeout_seconds": 30, "jobs": 1},
                    "sampling": {
                        "baseline_runs": 1,
                        "candidate_runs": 1,
                    },
                    "reduction": {
                        "adapter": "none",
                        "source_reducer": "none",
                        "max_attempts": 20,
                    },
                    "inputs": {"keep_paths": ["required.txt"]},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return source, config

    def test_config_drives_doctor_and_reduction_without_semantic_cli_options(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, config = self._fixture(root)
            doctor_output = io.StringIO()
            with contextlib.redirect_stdout(doctor_output), contextlib.redirect_stderr(
                io.StringIO()
            ):
                doctor_exit = main(
                    [
                        "doctor",
                        str(source),
                        "--config",
                        str(config),
                        "--output",
                        str(root / "doctor-output"),
                        "--json",
                    ]
                )

            self.assertEqual(0, doctor_exit)
            diagnosis = json.loads(doctor_output.getvalue())
            self.assertTrue(diagnosis["ok"])
            self.assertEqual("pass", diagnosis["baseline"]["status"])
            self.assertEqual(1, diagnosis["baseline"]["runs"])
            self.assertEqual(1, diagnosis["baseline"]["passes"])

            output = root / "result"
            reduction_stdout = io.StringIO()
            with contextlib.redirect_stdout(
                reduction_stdout
            ), contextlib.redirect_stderr(io.StringIO()):
                reduction_exit = main(
                    [
                        str(source),
                        "--config",
                        str(config),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, reduction_exit)
            self.assertEqual(
                output.resolve(), Path(reduction_stdout.getvalue().strip())
            )
            self.assertEqual(
                ["reproduce.py", "required.txt"],
                sorted(path.name for path in output.iterdir()),
            )
            report = json.loads(
                (root / "result.repomin" / "report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(7, report["failure_spec"]["exit_code"])
            self.assertEqual(["required.txt"], report["execution"]["keep_paths"])
            self.assertEqual(20, report["execution"]["max_attempts"])

    def test_config_rejects_a_semantic_cli_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, config = self._fixture(root)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        str(source),
                        "--config",
                        str(config),
                        "--command",
                        "false",
                        "--output",
                        str(root / "result"),
                    ]
                )

            self.assertEqual(2, exit_code)
            self.assertIn("--command", stderr.getvalue())
            self.assertIn("--config", stderr.getvalue())
            self.assertFalse((root / "result").exists())

    def test_config_path_can_precede_the_source_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, config = self._fixture(root)
            output = root / "result"

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                exit_code = main(
                    [
                        "--config",
                        str(config),
                        str(source),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertTrue(output.is_dir())

    def test_config_ignores_semantic_reducer_environment_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, config = self._fixture(root)
            output = root / "result"
            environment = {
                "REPOMIN_SEMANTIC_REDUCER": "http",
                "REPOMIN_SEMANTIC_ENDPOINT": "http://127.0.0.1:1/v1/chat/completions",
                "REPOMIN_SEMANTIC_MODEL": "environment-model",
                "REPOMIN_SEMANTIC_TIMEOUT": "not-a-number",
            }

            with patch.dict(os.environ, environment), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        str(source),
                        "--config",
                        str(config),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, exit_code)
            report = json.loads(
                (root / "result.repomin" / "report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("none", report["execution"]["semantic_reducer"])
            self.assertIsNone(report["execution"]["semantic_endpoint"])
            self.assertIsNone(report["execution"]["semantic_model"])


if __name__ == "__main__":
    unittest.main()
