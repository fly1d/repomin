"""Reject unavailable export parents before spending oracle samples."""

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
from repomin.report import validate_report_file


class OutputPreflightTest(unittest.TestCase):
    def _fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        source = root / "source"
        source.mkdir()
        (source / "reproduce.py").write_text(
            "from pathlib import Path\n"
            "if not Path('required.txt').is_file():\n"
            "    print('DIFFERENT_FAILURE')\n"
            "    raise SystemExit(2)\n"
            "print('ORIGINAL_FAILURE')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        (source / "required.txt").write_text("required\n", encoding="utf-8")
        (source / "unused.txt").write_text("noise\n", encoding="utf-8")
        command = [sys.executable, "reproduce.py"]
        command_text = (
            subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        )
        arguments = [
            str(source), "--command", command_text,
            "--match", "ORIGINAL_FAILURE", "--exit-code", "1",
            "--adapter", "none", "--source-reducer", "none", "--baseline-runs", "1",
        ]
        return root, source, arguments

    def _call(self, mode, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main([mode, *arguments])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_unavailable_output_parent_fails_before_runner_setup(self):
        for kind in ("missing", "file", "file-ancestor"):
            with self.subTest(kind=kind):
                root, source, arguments = self._fixture()
                parent = root / "parent"
                if kind != "missing":
                    parent.write_text("existing file\n", encoding="utf-8")
                output = parent / "result"
                if kind == "file-ancestor":
                    output = parent / "nested" / "result"
                before = {path.name: path.read_bytes() for path in source.iterdir()}
                arguments += ["--output", str(output)]
                for mode, expected, runner in (
                    ("doctor", 1, "repomin.doctor._runner"),
                    ("reduce", 2, "repomin.cli._build_runner"),
                ):
                    with patch(runner) as build_runner:
                        status, stdout, stderr = self._call(mode, arguments)
                    self.assertEqual(expected, status, stdout + stderr)
                    self.assertIn("output parent must be an existing directory", stdout + stderr)
                    self.assertIn("mkdir", stdout + stderr)
                    build_runner.assert_not_called()
                self.assertEqual(
                    before, {path.name: path.read_bytes() for path in source.iterdir()}
                )
                if kind == "missing":
                    self.assertFalse(parent.exists())
                else:
                    self.assertEqual(b"existing file\n", parent.read_bytes())

    def test_creating_parent_allows_doctor_export_validation_and_replay(self):
        root, source, arguments = self._fixture()
        parent = root / "created-parent"
        output = parent / "result"
        arguments += ["--output", str(output)]
        before = {path.name: path.read_bytes() for path in source.iterdir()}
        status, stdout, stderr = self._call("doctor", [*arguments, "--json"])
        self.assertEqual(1, status, stdout + stderr)
        self.assertEqual("not_run", json.loads(stdout)["baseline"]["status"])
        self.assertFalse(parent.exists())

        parent.mkdir()
        status, stdout, stderr = self._call("doctor", [*arguments, "--json"])
        self.assertEqual(0, status, stdout + stderr)
        self.assertEqual("pass", json.loads(stdout)["baseline"]["status"])
        self.assertEqual([], list(parent.iterdir()))
        status, stdout, stderr = self._call("reduce", arguments)
        self.assertEqual(0, status, stdout + stderr)
        self.assertEqual({"reproduce.py", "required.txt"}, {p.name for p in output.iterdir()})
        metadata = output.with_name(output.name + ".repomin")
        report_path = metadata / "report.json"
        self.assertTrue((metadata / "REPOMIN.md").is_file())
        self.assertFalse((output / "report.json").exists())
        validate_report_file(report_path, payload=output)
        status, stdout, stderr = self._call(
            "report", ["replay", str(report_path), "--payload", str(output), "--yes", "--runs", "2", "--json"]
        )
        self.assertEqual(0, status, stdout + stderr)
        self.assertEqual(2, json.loads(stdout)["passes"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in source.iterdir()})

    def test_available_parent_does_not_allow_overwrite_or_output_inside_source(self):
        root, source, arguments = self._fixture()
        existing = root / "existing"
        existing.mkdir()
        (existing / "marker.txt").write_text("preserve\n", encoding="utf-8")
        for output, message in (
            (existing, "output already exists"),
            (source / "result", "output must not be inside the source repository"),
        ):
            with self.subTest(output=output.name):
                with patch("repomin.cli._build_runner") as build_runner:
                    status, stdout, stderr = self._call(
                        "reduce", [*arguments, "--output", str(output)]
                    )
                self.assertEqual(2, status, stdout + stderr)
                self.assertIn(message, stderr)
                build_runner.assert_not_called()
        self.assertEqual("preserve\n", (existing / "marker.txt").read_text(encoding="utf-8"))
        self.assertFalse((source / "result").exists())


if __name__ == "__main__":
    unittest.main()
