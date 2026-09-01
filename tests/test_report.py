import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from repomin.cli import main
from repomin.model import FailureSpec, ReductionResult, ReductionStats, RunResult
from repomin.report import (
    ReportValidationError,
    _build_report,
    _reproduction_markdown,
    validate_report_document,
    validate_report_file,
)
from repomin.session import _tree_content_digest, _tree_digest


def _report() -> dict:
    return {
        "schema_version": 1,
        "command": "python3 reproduce.py",
        "failure_match": "FAIL",
        "baseline_exit_code": 1,
        "final_exit_code": 1,
        "source": {"files": 2, "bytes": 10},
        "output": {"files": 1, "bytes": 5},
        "attempts": 1,
        "accepted_mutations": 1,
        "cache_hits": 0,
        "execution": {"backend": "host", "jobs": 1},
        "phase_statistics": {
            "schema_version": 1,
            "coverage": "complete",
            "phases": [
                {
                    "phase": "files",
                    "attempts": 1,
                    "no_op": 0,
                    "rejected": 0,
                    "accepted": 1,
                    "superseded": 0,
                    "aborted": 0,
                    "oracle_sample_uses": 1,
                    "oracle_samples": 1,
                    "cache_hits": 0,
                }
            ],
        },
        "holdout_certification": {
            "status": "not_requested",
            "planned_runs": 0,
            "completed_runs": 0,
            "passes": 0,
            "samples": [],
            "artifact_fingerprint": None,
        },
        "events": [],
    }


class ReportValidationTest(unittest.TestCase):
    def test_generated_report_records_replay_contract_and_output_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reduced"
            output.mkdir()
            (output / "case.txt").write_text("failure\n", encoding="utf-8")
            result = ReductionResult(
                output=output,
                stats=ReductionStats(
                    source_files=1,
                    source_bytes=8,
                    output_files=1,
                    output_bytes=8,
                ),
                baseline=RunResult(7, "", "failure", 0.0),
                final_run=RunResult(7, "", "failure", 0.0),
            )

            report = _build_report(
                result,
                "python3 reproduce.py",
                "failure",
                failure_spec=FailureSpec("failure", exit_code=7),
                timeout_seconds=30.0,
            )

            self.assertEqual(30.0, report["execution"]["timeout_seconds"])
            self.assertEqual(7, report["failure_spec"]["exit_code"])
            self.assertEqual(
                _tree_digest(output, set()),
                report["output"]["tree_sha256"],
            )
            self.assertIs(validate_report_document(report), report)

    def test_transport_content_fingerprint_survives_mtime_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "reduced"
            metadata = root / "reduced.repomin"
            output.mkdir()
            metadata.mkdir()
            entry = output / "case.txt"
            # Keep the fixture byte-stable on Windows, where text-mode writes
            # translate ``\n`` to ``\r\n``.
            entry.write_bytes(b"failure\n")
            result = ReductionResult(
                output=output,
                stats=ReductionStats(
                    source_files=1,
                    source_bytes=8,
                    output_files=1,
                    output_bytes=8,
                ),
                baseline=RunResult(7, "", "failure", 0.0),
                final_run=RunResult(7, "", "failure", 0.0),
            )
            report = _build_report(result, "python3 reproduce.py", "failure")
            report_path = metadata / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            original_full = report["output"]["tree_sha256"]
            content_digest = report["output"]["tree_content_sha256"]
            status = entry.stat()
            os.utime(
                entry,
                ns=(status.st_atime_ns, status.st_mtime_ns + 1000000),
            )
            self.assertNotEqual(original_full, _tree_digest(output, set()))
            self.assertEqual(content_digest, _tree_content_digest(output, set()))
            validate_report_file(report_path, output)

    def test_rejects_inconsistent_replay_contract(self) -> None:
        report = _report()
        report["failure_spec"] = {
            "schema_version": 1,
            "match": "DIFFERENT",
            "exit_code": None,
            "java_exception": False,
            "python_exception": False,
            "process_failure": False,
        }
        with self.assertRaisesRegex(ReportValidationError, "failure_match"):
            validate_report_document(report)

        report = _report()
        report["failure_spec"] = None
        with self.assertRaisesRegex(ReportValidationError, "failure_spec"):
            validate_report_document(report)

    def test_rejects_orphaned_output_fingerprint_policy(self) -> None:
        report = _report()
        report["output"]["tree_fingerprint_policy"] = "tree-sha256-v2"
        with self.assertRaisesRegex(ReportValidationError, "requires tree_sha256"):
            validate_report_document(report)

    def test_rejects_unrepresentably_large_rate_counts(self) -> None:
        report = _report()
        report["events"] = [
            {
                "phase": "files",
                "description": "large-count fixture",
                "duration_seconds": 0.0,
                "oracle_runs": 10**1000,
                "oracle_passes": 10**1000,
                "oracle_rate": 1.0,
            }
        ]
        with self.assertRaisesRegex(ReportValidationError, "oracle_rate"):
            validate_report_document(report)

        report = _report()
        holdout = report["holdout_certification"]
        huge = 10**1000
        holdout.update(
            {
                "planned_runs": huge,
                "completed_runs": huge,
                "passes": huge,
                "minimum_rate": 0.1,
                "confidence": 0.9,
                "required_passes": huge,
                "observed_rate": 1.0,
                "exact_lower_bound": 0.1,
                "exact_p_value": 0.1,
                "exact_rate_gate_passed": True,
            }
        )
        with self.assertRaisesRegex(ReportValidationError, "observed_rate"):
            validate_report_document(report)

    def test_generated_report_records_repomin_version(self) -> None:
        result = ReductionResult(
            output=Path("reduced"),
            stats=ReductionStats(source_files=1, source_bytes=1),
            baseline=RunResult(1, "", "", 0.0),
            final_run=RunResult(1, "", "", 0.0),
        )
        report = _build_report(result, "python3 reproduce.py", "FAIL")
        self.assertEqual("0.1.0.dev6", report["repomin_version"])
        self.assertIs(validate_report_document(report), report)

    def test_legacy_report_without_version_remains_valid(self) -> None:
        report = _report()
        self.assertNotIn("repomin_version", report)
        self.assertIs(validate_report_document(report), report)

    def test_rejects_malformed_version_provenance(self) -> None:
        report = _report()
        report["repomin_version"] = ""
        with self.assertRaisesRegex(ReportValidationError, "repomin_version"):
            validate_report_document(report)

    def test_schema_versions_must_be_integer_one(self) -> None:
        for path, value in (
            (("schema_version",), True),
            (("schema_version",), 1.0),
            (("phase_statistics", "schema_version"), True),
            (("holdout_certification", "schema_version"), None),
        ):
            report = _report()
            section = report
            for key in path[:-1]:
                section = section[key]
            section[path[-1]] = value
            with self.subTest(path=path, value=value):
                with self.assertRaisesRegex(ReportValidationError, "schema_version"):
                    validate_report_document(report)

    def test_execution_environment_metadata_is_structurally_validated(self) -> None:
        report = _report()
        report["execution"]["environment_names"] = ["GOOD", "GOOD"]
        with self.assertRaisesRegex(ReportValidationError, "environment_names"):
            validate_report_document(report)

        report = _report()
        report["execution"]["environment_names"] = ["BAD-NAME"]
        with self.assertRaisesRegex(ReportValidationError, "environment_names"):
            validate_report_document(report)

        report = _report()
        report["execution"]["environment_sha256"] = "not-a-digest"
        with self.assertRaisesRegex(ReportValidationError, "environment_sha256"):
            validate_report_document(report)

        report = _report()
        report["execution"]["environment_names"] = ["Path", "PATH"]
        with mock.patch("repomin.report.os", SimpleNamespace(name="nt")):
            with self.assertRaisesRegex(ReportValidationError, "ambiguous"):
                validate_report_document(report)

    def test_rejects_malformed_events(self) -> None:
        report = _report()
        report.pop("events")
        with self.assertRaisesRegex(ReportValidationError, "events must be an array"):
            validate_report_document(report)

    def test_rejects_unhashable_phase_coverage_without_traceback(self) -> None:
        for coverage in ([], {}):
            report = _report()
            report["phase_statistics"]["coverage"] = coverage
            with self.subTest(coverage=coverage):
                with self.assertRaisesRegex(ReportValidationError, "coverage"):
                    validate_report_document(report)
        report = _report()
        report["events"] = [{"phase": "files"}]
        with self.assertRaisesRegex(ReportValidationError, "description"):
            validate_report_document(report)
        report = _report()
        report["events"] = [{
            'phase': 'files',
            'description': 'remove file',
            'duration_seconds': 0.1,
            'oracle_runs': 1,
            'oracle_passes': 2,
        }]
        with self.assertRaisesRegex(ReportValidationError, "exceed runs"):
            validate_report_document(report)

    def test_rejects_inconsistent_event_evidence(self) -> None:
        report = _report()
        report["events"] = [{
            "phase": "files",
            "description": "remove file",
            "duration_seconds": 0.1,
            "oracle_runs": 2,
            "oracle_passes": 1,
            "oracle_rate": 1.0,
            "oracle_lower_bound": 0.5,
            "oracle_anytime_lower_bound": 0.5,
            "oracle_early_acceptance": False,
        }]
        with self.assertRaisesRegex(ReportValidationError, "oracle_rate"):
            validate_report_document(report)

        report["events"][0]["oracle_rate"] = 0.5
        report["events"][0]["oracle_early_acceptance"] = "false"
        with self.assertRaisesRegex(ReportValidationError, "early_acceptance"):
            validate_report_document(report)

        report["events"][0]["oracle_early_acceptance"] = False
        report["events"][0]["candidate_confidence"] = 0.9
        with self.assertRaisesRegex(ReportValidationError, "incomplete"):
            validate_report_document(report)

        report["events"][0].pop("candidate_confidence")
        report["events"][0]["oracle_lower_bound"] = float("nan")
        with self.assertRaisesRegex(ReportValidationError, "finite"):
            validate_report_document(report)

        report["events"][0]["oracle_lower_bound"] = 10**1000
        with self.assertRaisesRegex(ReportValidationError, "finite"):
            validate_report_document(report)

    def test_reproduction_markdown_uses_longer_fence_for_backticks(self) -> None:
        result = ReductionResult(
            output=Path("reduced"),
            stats=ReductionStats(source_files=1, source_bytes=1),
            baseline=RunResult(1, "", "", 0.0),
            final_run=RunResult(1, "", "", 0.0),
        )
        markdown = _reproduction_markdown(result, "python3 -c 'print(\"```\")'", None)
        self.assertIn("````sh\npython3 -c 'print(\"```\")'\n````\n", markdown)

    def test_accepts_complete_report_accounting(self) -> None:
        report = _report()
        self.assertIs(validate_report_document(report), report)

    def test_rejects_phase_accounting_drift(self) -> None:
        report = _report()
        report["phase_statistics"]["phases"][0]["accepted"] = 0
        with self.assertRaisesRegex(ReportValidationError, "attempts accounting"):
            validate_report_document(report)

    def test_rejects_unsupported_schema(self) -> None:
        report = _report()
        report["schema_version"] = 99
        with self.assertRaisesRegex(ReportValidationError, "unsupported"):
            validate_report_document(report)

    def test_rejects_malformed_holdout_samples(self) -> None:
        report = _report()
        holdout = report["holdout_certification"]
        holdout.update(
            {
                'status': 'certified',
                'planned_runs': 1,
                'completed_runs': 1,
                'passes': 1,
                'samples': [{'index': 1, 'accepted': 'yes'}],
                'artifact_fingerprint': 'a' * 64,
            }
        )
        with self.assertRaisesRegex(ReportValidationError, "accepted must be boolean"):
            validate_report_document(report)
        holdout["samples"] = [{"index": 2, "accepted": True}]
        with self.assertRaisesRegex(ReportValidationError, "contiguous"):
            validate_report_document(report)
        holdout["samples"] = [{"index": 1, "accepted": True}]
        holdout["passes"] = 0
        with self.assertRaisesRegex(ReportValidationError, "do not match"):
            validate_report_document(report)

    def test_rejects_inconsistent_holdout_evidence(self) -> None:
        report = _report()
        holdout = report["holdout_certification"]
        holdout.update(
            {
                "status": "not_certified",
                "planned_runs": 1,
                "completed_runs": 1,
                "samples": [
                    {
                        "index": 1,
                        "outcome": "passed",
                        "accepted": False,
                        "returncode": 1,
                        "duration_seconds": 0.1,
                        "output_sha256": "a" * 64,
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ReportValidationError, "does not match"):
            validate_report_document(report)

        sample = holdout["samples"][0]
        sample["outcome"] = "timed_out"
        sample["timed_out"] = True
        sample["accepted"] = True
        with self.assertRaisesRegex(ReportValidationError, "cannot be accepted"):
            validate_report_document(report)

        sample["accepted"] = False
        sample["timed_out"] = True
        sample["resource_exhausted"] = True
        with self.assertRaisesRegex(ReportValidationError, "timed out and resource"):
            validate_report_document(report)

        sample["timed_out"] = False
        sample["resource_exhausted"] = False
        sample["outcome"] = "interrupted"
        with self.assertRaisesRegex(ReportValidationError, "interrupted evidence"):
            validate_report_document(report)

    def test_rejects_holdout_aggregate_drift(self) -> None:
        report = _report()
        holdout = report["holdout_certification"]
        holdout.update(
            {
                "status": "not_certified",
                "planned_runs": 1,
                "completed_runs": 1,
                "passes": 0,
                "timed_out_runs": 0,
                "resource_exhausted_runs": 0,
                "interrupted_runs": 0,
                "samples": [
                    {
                        "index": 1,
                        "outcome": "timed_out",
                        "accepted": False,
                        "timed_out": True,
                        "resource_exhausted": False,
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ReportValidationError, "timed out count"):
            validate_report_document(report)

        holdout["timed_out_runs"] = 1
        holdout["interrupted_runs"] = 1
        with self.assertRaisesRegex(ReportValidationError, "interrupted count"):
            validate_report_document(report)

        holdout["interrupted_runs"] = 0
        holdout["resource_exhausted_runs"] = 2
        with self.assertRaisesRegex(ReportValidationError, "exceeds completed"):
            validate_report_document(report)

    def test_rejects_inconsistent_holdout_statistics(self) -> None:
        report = _report()
        holdout = report["holdout_certification"]
        holdout.update(
            {
                "status": "certified",
                "planned_runs": 1,
                "completed_runs": 1,
                "passes": 1,
                "minimum_rate": 0.9,
                "confidence": 0.95,
                "required_passes": 1,
                "observed_rate": 0.5,
                "exact_lower_bound": 0.1,
                "exact_p_value": 0.01,
                "exact_rate_gate_passed": True,
                "samples": [{"index": 1, "accepted": True}],
                "artifact_fingerprint": "a" * 64,
            }
        )
        with self.assertRaisesRegex(ReportValidationError, "observed_rate"):
            validate_report_document(report)

        holdout["observed_rate"] = 1.0
        holdout["confidence"] = 1.0
        with self.assertRaisesRegex(ReportValidationError, "confidence"):
            validate_report_document(report)

        holdout["confidence"] = 0.95
        holdout["exact_rate_gate_passed"] = "yes"
        with self.assertRaisesRegex(ReportValidationError, "must be boolean"):
            validate_report_document(report)

        holdout["exact_rate_gate_passed"] = True
        holdout.pop("exact_p_value")
        with self.assertRaisesRegex(ReportValidationError, "terminal statistics"):
            validate_report_document(report)

    def test_modern_certified_holdout_requires_terminal_statistics(self) -> None:
        report = _report()
        holdout = report["holdout_certification"]
        holdout.update(
            {
                "schema_version": 1,
                "status": "certified",
                "planned_runs": 1,
                "completed_runs": 1,
                "passes": 1,
                "samples": [{"index": 1, "accepted": True}],
                "artifact_fingerprint": "a" * 64,
            }
        )
        with self.assertRaisesRegex(ReportValidationError, "terminal statistics"):
            validate_report_document(report)

        holdout.update(
            {
                "minimum_rate": 0.9,
                "confidence": 0.95,
                "required_passes": 1,
                "observed_rate": 1.0,
                "exact_lower_bound": 0.05,
                "exact_p_value": 0.9,
                "exact_rate_gate_passed": True,
            }
        )
        self.assertIs(validate_report_document(report), report)

    def test_rejects_ordinary_failure_aggregate_drift(self) -> None:
        report = _report()
        holdout = report["holdout_certification"]
        holdout.update(
            {
                "status": "not_certified",
                "planned_runs": 1,
                "completed_runs": 1,
                "passes": 0,
                "ordinary_failures": 0,
                "samples": [
                    {
                        "index": 1,
                        "outcome": "failed",
                        "accepted": False,
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ReportValidationError, "ordinary failures"):
            validate_report_document(report)

        holdout["ordinary_failures"] = 1
        self.assertIs(validate_report_document(report), report)

    def test_validates_certified_payload_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "reduced"
            metadata = root / "reduced.repomin"
            payload.mkdir()
            metadata.mkdir()
            # Use bytes so the reported size is identical on Windows, where
            # text-mode writes translate ``\n`` to ``\r\n``.
            (payload / "required.txt").write_bytes(b"keep\n")
            report = _report()
            report["holdout_certification"] = {
                "status": "certified",
                "planned_runs": 1,
                "completed_runs": 1,
                "passes": 1,
                "samples": [{"index": 1, "accepted": True}],
                "artifact_fingerprint": _tree_digest(payload, set()),
            }
            report_path = metadata / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            validate_report_file(report_path, payload)
            (payload / "required.txt").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ReportValidationError, "fingerprint"):
                validate_report_file(report_path, payload)

    def test_cli_validate_reports_machine_readable_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            report_path.write_text(json.dumps(_report()), encoding="utf-8")
            from contextlib import redirect_stdout
            from io import StringIO

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["report", "validate", str(report_path), "--json"])
            self.assertEqual(0, exit_code)
            result = json.loads(output.getvalue())
            self.assertTrue(result["valid"])
            self.assertIsNone(result["repomin_version"])
            self.assertEqual("host", result["backend"])
            self.assertEqual(2, result["source_files"])
            self.assertEqual(10, result["source_bytes"])
            self.assertEqual(1, result["output_files"])
            self.assertEqual(5, result["output_bytes"])
            self.assertEqual(1, result["attempts"])
            self.assertEqual(1, result["accepted_mutations"])
            self.assertEqual(0, result["cache_hits"])


if __name__ == "__main__":
    unittest.main()
