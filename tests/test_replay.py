"""Tests for fresh-copy replay of recorded failure reports."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repomin.cli import main
from repomin.model import (
    FailureSpec,
    ProcessFailureSignature,
    ReductionResult,
    ReductionStats,
    RunResult,
)
from repomin.replay import ReplayError, format_replay, replay_report
from repomin.report import ReportValidationError, _build_report, measure_tree
from repomin.session import _tree_digest


_REPRODUCER = """\
from pathlib import Path
import os
import sys

if Path.cwd().name != "stable-replay-name":
    print("WRONG_DIRECTORY", file=sys.stderr)
    raise SystemExit(2)
if Path("run-marker.txt").exists():
    print("STALE_COPY", file=sys.stderr)
    raise SystemExit(3)
Path("run-marker.txt").write_text("created by command\\n", encoding="utf-8")
expected = os.environ.get("REPLAY_TEST_TOKEN")
if expected is not None and expected != "correct-secret":
    print("WRONG_ENVIRONMENT", file=sys.stderr)
    raise SystemExit(4)
print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(7)
"""


def _python_command(script: str) -> str:
    argv = [sys.executable, script]
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _environment_digest(environment: dict) -> str:
    encoded = "".join(
        "%s=%s\0" % (name, environment[name]) for name in sorted(environment)
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ReplayTest(unittest.TestCase):
    def _fixture(
        self,
        *,
        spec: FailureSpec = FailureSpec("ORIGINAL_FAILURE"),
        environment: dict = None,
        process_signature: ProcessFailureSignature = None,
    ) -> tuple:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        payload = root / "payload"
        payload.mkdir()
        (payload / "reproduce.py").write_text(_REPRODUCER, encoding="utf-8")
        files, size = measure_tree(payload)
        configured_environment = dict(environment or {})
        stats = ReductionStats(
            source_files=files,
            source_bytes=size,
            backend="host",
            working_directory_policy="host-output-basename-v1",
            working_directory_basename="stable-replay-name",
            output_files=files,
            output_bytes=size,
            environment_names=sorted(configured_environment),
            environment_sha256=_environment_digest(configured_environment),
        )
        result = ReductionResult(
            output=payload,
            stats=stats,
            baseline=RunResult(7, "", "ORIGINAL_FAILURE", 0.01),
            final_run=RunResult(7, "", "ORIGINAL_FAILURE", 0.01),
            process_failure_signature=process_signature,
        )
        report = _build_report(
            result,
            _python_command("reproduce.py"),
            spec.match,
            failure_spec=spec,
            timeout_seconds=10.0,
        )
        metadata = root / "payload.repomin"
        metadata.mkdir()
        report_path = metadata / "report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return payload, report_path, report

    def test_replay_uses_fresh_copies_and_preserves_payload(self) -> None:
        payload, report_path, _report = self._fixture()

        reproduced, result = replay_report(report_path, payload, runs=2)

        self.assertTrue(reproduced)
        self.assertTrue(result["reproduced"])
        self.assertEqual(2, result["passes"])
        self.assertTrue(result["fingerprint_verified"])
        self.assertTrue(result["fresh_repository_copy_per_run"])
        self.assertFalse(result["cache_used"])
        self.assertFalse((payload / "run-marker.txt").exists())
        self.assertEqual(["passed", "passed"], [
            sample["outcome"] for sample in result["samples"]
        ])

    def test_replay_accepts_archive_mtime_drift_with_content_evidence(self) -> None:
        payload, report_path, _report = self._fixture()
        entry = payload / "reproduce.py"
        status = entry.stat()
        os.utime(entry, ns=(status.st_atime_ns, status.st_mtime_ns + 1000000))
        self.assertNotEqual(
            json.loads(report_path.read_text(encoding="utf-8"))["output"][
                "tree_sha256"
            ],
            _tree_digest(payload, set()),
        )

        reproduced, result = replay_report(report_path, payload)

        self.assertTrue(reproduced)
        self.assertEqual("content", result["fingerprint_mode"])
        self.assertTrue(result["metadata_drift_possible"])
        self.assertTrue(result["fingerprint_verified"])
        self.assertIsNotNone(result["actual_content_fingerprint"])

    def test_completed_mismatch_is_evidence_not_a_setup_error(self) -> None:
        payload, report_path, report = self._fixture(
            spec=FailureSpec("ORIGINAL_FAILURE", exit_code=7)
        )
        report["command"] = _python_command("-c") + " pass"
        report_path.write_text(json.dumps(report), encoding="utf-8")

        reproduced, result = replay_report(report_path, payload)

        self.assertFalse(reproduced)
        self.assertEqual(0, result["passes"])
        self.assertEqual("exit_code", result["samples"][0]["mismatch_reason"])
        self.assertEqual(7, result["samples"][0]["expected_exit_code"])
        self.assertEqual(0, result["samples"][0]["actual_exit_code"])
        self.assertIn("exit code expected 7, actual 0", format_replay(result))
        self.assertNotIn("ORIGINAL_FAILURE", json.dumps(result))

    def test_environment_names_and_digest_must_match_without_leaking_values(self) -> None:
        environment = {"REPLAY_TEST_TOKEN": "correct-secret"}
        payload, report_path, _report = self._fixture(environment=environment)
        with self.assertRaisesRegex(ReplayError, "environment names"):
            replay_report(report_path, payload)
        with self.assertRaisesRegex(ReplayError, "environment values"):
            replay_report(
                report_path,
                payload,
                environment={"REPLAY_TEST_TOKEN": "wrong-secret"},
            )

        reproduced, result = replay_report(
            report_path,
            payload,
            environment=environment,
        )

        self.assertTrue(reproduced)
        self.assertNotIn("correct-secret", json.dumps(result))

    def test_environment_names_without_a_digest_are_rejected(self) -> None:
        payload, report_path, report = self._fixture(
            environment={"REPLAY_TEST_TOKEN": "correct-secret"}
        )
        report["execution"].pop("environment_sha256")
        report_path.write_text(json.dumps(report), encoding="utf-8")

        with self.assertRaisesRegex(ReplayError, "environment digest"):
            replay_report(
                report_path,
                payload,
                environment={"REPLAY_TEST_TOKEN": "any-value"},
            )

    def test_case_insensitive_environment_name_collisions_are_rejected_on_windows(
        self,
    ) -> None:
        payload, report_path, report = self._fixture()
        report["execution"]["environment_names"] = ["Path", "PATH"]
        report["execution"]["environment_sha256"] = _environment_digest(
            {"Path": "one", "PATH": "two"}
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")

        fake_os = mock.Mock()
        fake_os.name = "nt"
        with mock.patch("repomin.replay.os", fake_os):
            with self.assertRaisesRegex(ValueError, "(case-insensitive|ambiguous)"):
                replay_report(
                    report_path,
                    payload,
                    environment={"Path": "one", "PATH": "two"},
                )

    def test_present_invalid_working_directory_basename_is_rejected(self) -> None:
        payload, report_path, report = self._fixture()
        for value in (None, "", "../escape", "nested/name"):
            with self.subTest(value=value):
                report["execution"]["working_directory_basename"] = value
                report_path.write_text(json.dumps(report), encoding="utf-8")
                with self.assertRaisesRegex(ReplayError, "basename"):
                    replay_report(report_path, payload)

    def test_tampered_payload_is_rejected_before_execution(self) -> None:
        payload, report_path, _report = self._fixture()
        (payload / "reproduce.py").write_text(
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ReportValidationError, "fingerprint differs"):
            replay_report(report_path, payload)

        self.assertFalse((payload / "run-marker.txt").exists())

    def test_legacy_plain_exit_code_requires_an_explicit_contract(self) -> None:
        spec = FailureSpec(None, exit_code=7)
        payload, report_path, report = self._fixture(spec=spec)
        report.pop("failure_spec")
        report_path.write_text(json.dumps(report), encoding="utf-8")

        with self.assertRaisesRegex(ReplayError, "legacy report"):
            replay_report(report_path, payload)

        reproduced, result = replay_report(
            report_path,
            payload,
            legacy_exit_code=7,
        )
        self.assertTrue(reproduced)
        self.assertEqual("legacy_inferred", result["oracle_source"])

    def test_process_signature_is_pinned_instead_of_relearned(self) -> None:
        signature = ProcessFailureSignature("exit_code", 7)
        spec = FailureSpec(None, process_failure=True)
        payload, report_path, report = self._fixture(
            spec=spec,
            process_signature=signature,
        )

        reproduced, result = replay_report(report_path, payload)
        self.assertTrue(reproduced)
        self.assertEqual("process_failure", result["oracle_mode"])

        report["process_failure_signature"]["code"] = 8
        report_path.write_text(json.dumps(report), encoding="utf-8")
        reproduced, result = replay_report(report_path, payload)
        self.assertFalse(reproduced)
        self.assertEqual("signature", result["samples"][0]["mismatch_reason"])
        self.assertIsNone(result["samples"][0]["expected_exit_code"])
        self.assertEqual(7, result["samples"][0]["actual_exit_code"])
        self.assertIn("no exact exit-code contract", format_replay(result))

    def test_malformed_signature_null_is_rejected_without_traceback(self) -> None:
        spec = FailureSpec(None, process_failure=True)
        payload, report_path, report = self._fixture(
            spec=spec,
            process_signature=ProcessFailureSignature("exit_code", 7),
        )
        report["process_failure_signature"] = None
        report_path.write_text(json.dumps(report), encoding="utf-8")

        with self.assertRaisesRegex(ReportValidationError, "process_failure_signature"):
            replay_report(report_path, payload)

    def test_modern_failure_spec_requires_match_and_exit_code_fields(self) -> None:
        payload, report_path, report = self._fixture()
        report["failure_spec"].pop("match")
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ReportValidationError, "failure_spec.match"):
            replay_report(report_path, payload)

        payload, report_path, report = self._fixture()
        report["failure_spec"].pop("exit_code")
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ReportValidationError, "failure_spec.exit_code"):
            replay_report(report_path, payload)

    def test_malformed_docker_limits_are_rejected_before_runner_setup(self) -> None:
        payload, report_path, report = self._fixture()
        report["execution"].update(
            {
                "backend": "docker",
                "image_id": "sha256:" + "a" * 64,
                "limits": {"cpus": 10**1000},
            }
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")

        with self.assertRaisesRegex(ReportValidationError, "cpus"):
            replay_report(report_path, payload)

        report["execution"]["limits"] = {"pids": None}
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ReportValidationError, "pids"):
            replay_report(report_path, payload)

    def test_docker_replay_rejects_mutable_recorded_image_without_override(self) -> None:
        payload, report_path, report = self._fixture()
        report["execution"].update(
            {
                "backend": "docker",
                "image_id": "fixture:latest",
            }
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")

        with self.assertRaisesRegex(ReplayError, "immutable image ID"):
            replay_report(report_path, payload)

    def test_nonfinite_and_unrepresentable_replay_timeout_is_rejected(self) -> None:
        payload, report_path, report = self._fixture()
        report["execution"]["timeout_seconds"] = 10**1000
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ReportValidationError, "timeout_seconds"):
            replay_report(report_path, payload)

        report["execution"].pop("timeout_seconds")
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ReplayError, "timeout"):
            replay_report(report_path, payload, timeout_seconds=10**1000)

    def test_command_with_nul_is_rejected_as_invalid_report(self) -> None:
        payload, report_path, report = self._fixture()
        report["command"] = "printf 'bad\x00command'"
        report_path.write_text(json.dumps(report), encoding="utf-8")

        with self.assertRaisesRegex(ReportValidationError, "command.*NUL"):
            replay_report(report_path, payload)

    def test_cli_requires_confirmation_and_emits_private_json(self) -> None:
        payload, report_path, _report = self._fixture()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "report",
                    "replay",
                    str(report_path),
                    "--payload",
                    str(payload),
                ]
            )
        self.assertEqual(2, exit_code)
        self.assertIn("pass --yes", stderr.getvalue())

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "report",
                    "replay",
                    str(report_path),
                    "--payload",
                    str(payload),
                    "--runs",
                    "2",
                    "--yes",
                    "--json",
                ]
            )
        self.assertEqual(0, exit_code)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["reproduced"])
        self.assertNotIn("ORIGINAL_FAILURE", stdout.getvalue())

    def test_rejects_invalid_runs_and_payload_root_symlink(self) -> None:
        payload, report_path, _report = self._fixture()
        with self.assertRaisesRegex(ReplayError, "runs"):
            replay_report(report_path, payload, runs=0)
        if os.name == "nt":
            return
        link = payload.parent / "payload-link"
        link.symlink_to(payload, target_is_directory=True)
        with self.assertRaisesRegex(ReplayError, "symbolic link"):
            replay_report(report_path, link)


if __name__ == "__main__":
    unittest.main()
