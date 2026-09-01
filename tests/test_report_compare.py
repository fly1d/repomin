import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from repomin.cli import main
from repomin.report_compare import (
    ReportComparisonError,
    compare_reports,
    render_comparison_markdown,
    render_comparison_text,
)


def _report() -> dict:
    return {
        "schema_version": 1,
        "repomin_version": "0.1.0.dev8",
        "command": "python3 reproduce.py",
        "failure_match": "ORIGINAL_FAILURE",
        "baseline_exit_code": 1,
        "final_exit_code": 1,
        "source": {"files": 10, "bytes": 1000},
        "output": {"files": 4, "bytes": 400},
        "attempts": 8,
        "accepted_mutations": 3,
        "cache_hits": 1,
        "execution": {
            "backend": "host",
            "jobs": 1,
            "budget_exhausted": False,
            "candidate_sampling_policy": "fixed-size-v1",
            "candidate_runs": 1,
            "min_candidate_rate": None,
            "confidence": 0.95,
            "run_confidence": None,
            "reduction_strategy": "global-dirty-worklist-v3",
            "environment_names": ["PRIVATE_ENV_NAME"],
            "environment_sha256": "a" * 64,
        },
        "phase_statistics": {
            "schema_version": 1,
            "coverage": "complete",
            "phases": [
                {
                    "phase": "files",
                    "attempts": 8,
                    "no_op": 1,
                    "rejected": 3,
                    "accepted": 3,
                    "superseded": 1,
                    "aborted": 0,
                    "oracle_sample_uses": 8,
                    "oracle_samples": 7,
                    "cache_hits": 1,
                }
            ],
        },
        "holdout_certification": {
            "schema_version": 1,
            "status": "not_requested",
            "planned_runs": 0,
            "completed_runs": 0,
            "passes": 0,
            "samples": [],
            "artifact_fingerprint": None,
        },
        "events": [],
    }


def _write_report(root: Path, name: str, report: dict) -> Path:
    path = root / name
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


class ReportCompareTest(unittest.TestCase):
    def test_comparison_is_deterministic_and_preserves_input_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            second = _report()
            second["repomin_version"] = "0.1.0.dev9"
            second["source"] = {"files": 12, "bytes": 1200}
            second["output"] = {"files": 3, "bytes": 300}
            second["attempts"] = 10
            second["accepted_mutations"] = 4
            second["phase_statistics"]["phases"][0].update(
                {"attempts": 10, "rejected": 4, "accepted": 4}
            )
            second["execution"]["budget_exhausted"] = True
            second["holdout_certification"] = {
                "schema_version": 1,
                "status": "certified",
                "planned_runs": 2,
                "completed_runs": 2,
                "passes": 2,
                "minimum_rate": 0.5,
                "confidence": 0.95,
                "required_passes": 1,
                "observed_rate": 1.0,
                "exact_lower_bound": 0.1,
                "exact_p_value": 0.1,
                "exact_rate_gate_passed": True,
                "samples": [
                    {"index": 1, "accepted": True},
                    {"index": 2, "accepted": True},
                ],
                "artifact_fingerprint": "c" * 64,
            }
            first_path = _write_report(root, "first.json", first)
            second_path = _write_report(root, "second.json", second)

            result = compare_reports(
                [first_path, second_path], labels=["baseline", "candidate"]
            )
            repeat = compare_reports(
                [first_path, second_path], labels=["baseline", "candidate"]
            )

        self.assertEqual(result, repeat)
        self.assertTrue(result["descriptive_only"])
        self.assertEqual(2, result["run_count"])
        self.assertEqual(
            ["baseline", "candidate"],
            [run["label"] for run in result["runs"]],
        )
        self.assertEqual("0.1.0.dev8", result["runs"][0]["repomin_version"])
        self.assertEqual("0.1.0.dev9", result["runs"][1]["repomin_version"])
        delta = result["deltas"][0]
        self.assertEqual(-1, delta["numeric_deltas"]["output_files"])
        self.assertEqual(-100, delta["numeric_deltas"]["output_bytes"])
        self.assertEqual(1, delta["numeric_deltas"]["accepted_mutations"])
        self.assertEqual(-0.15, delta["numeric_deltas"]["file_retention_ratio"])
        self.assertIn("source sizes differ", result["context_warnings"][0])
        self.assertIn("holdout certification statuses differ", result["context_warnings"])

    def test_legacy_version_and_partial_phase_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            first.pop("repomin_version")
            second = _report()
            second["phase_statistics"] = {
                "coverage": "partial",
                "phases": [],
            }
            paths = [
                _write_report(root, "legacy.json", first),
                _write_report(root, "partial.json", second),
            ]
            result = compare_reports(paths)

        self.assertIsNone(result["runs"][0]["repomin_version"])
        self.assertEqual("partial", result["runs"][1]["phase_coverage"])
        self.assertIn(
            "at least one report has partial phase statistics",
            result["context_warnings"],
        )
        self.assertIn("phase statistics coverage differs", result["context_warnings"])

    def test_execution_context_differences_are_warned_without_leaking_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            second = _report()
            first["execution"].update(
                {
                    "jobs": 1,
                    "timeout_seconds": 5.0,
                    "cache_enabled": True,
                    "max_attempts": 10,
                    "max_duration_seconds": 60.0,
                    "semantic_reducer": "http",
                    "semantic_model": "private-model-a",
                    "semantic_endpoint": "https://private-a.example/v1",
                    "working_directory_policy": "host-output-basename-v1",
                    "working_directory_basename": "private-a",
                    "resumed": False,
                }
            )
            second["execution"].update(
                {
                    "jobs": 2,
                    "timeout_seconds": 15.0,
                    "cache_enabled": False,
                    "max_attempts": 20,
                    "max_duration_seconds": 120.0,
                    "semantic_reducer": "none",
                    "semantic_model": "private-model-b",
                    "semantic_endpoint": "https://private-b.example/v1",
                    "working_directory_policy": "docker-workspace-v1",
                    "working_directory_basename": "private-b",
                    "resumed": True,
                }
            )
            first_path = _write_report(root, "first.json", first)
            second_path = _write_report(root, "second.json", second)
            result = compare_reports([first_path, second_path])
            serialized = json.dumps(result, sort_keys=True)
            text = render_comparison_text(result)
            markdown = render_comparison_markdown(result)

        warnings = result["context_warnings"]
        self.assertIn("execution jobs differ", warnings)
        self.assertTrue(any("timeout_seconds" in warning for warning in warnings))
        self.assertTrue(any("cache_enabled" in warning for warning in warnings))
        self.assertTrue(any("budget limits" in warning for warning in warnings))
        self.assertIn("semantic reducer configuration differs", warnings)
        self.assertIn("working-directory execution policies differ", warnings)
        self.assertIn("report resumption states differ", warnings)
        for secret in (
            "private-model-a",
            "private-model-b",
            "private-a.example",
            "private-b.example",
            "private-a",
            "private-b",
        ):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, markdown)

    def test_input_selection_and_oracle_identity_are_compared_opaquely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            second = _report()
            first_match = "PRIVATE_REGEX_FIRST"
            second_match = "PRIVATE_REGEX_SECOND"
            first["failure_match"] = first_match
            second["failure_match"] = second_match
            for report, match, suffix in (
                (first, first_match, "first"),
                (second, second_match, "second"),
            ):
                report["failure_spec"] = {
                    "schema_version": 1,
                    "match": match,
                    "exit_code": None,
                    "java_exception": False,
                    "python_exception": False,
                    "process_failure": False,
                }
                report["execution"].update(
                    {
                        "ignored_names": ["PRIVATE_IGNORED_%s" % suffix],
                        "ignored_paths": ["PRIVATE_PATH_%s/data" % suffix],
                        "gitignore_files": ["PRIVATE_GITIGNORE_%s" % suffix],
                        "gitignore_sha256": ("a" if suffix == "first" else "b") * 64,
                        "gitignore_recursive": suffix == "second",
                        "keep_paths": ["PRIVATE_KEEP_%s/important" % suffix],
                        "text_files": ["PRIVATE_TEXT_%s.txt" % suffix],
                    }
                )
            second["phase_statistics"]["phases"][0]["phase"] = "semantic"
            first_path = _write_report(root, "first.json", first)
            second_path = _write_report(root, "second.json", second)
            result = compare_reports([first_path, second_path])
            serialized = json.dumps(result, sort_keys=True)
            text = render_comparison_text(result)
            markdown = render_comparison_markdown(result)

        self.assertIn("input selection and exclusion controls differ", result["context_warnings"])
        self.assertIn(
            "failure oracle configuration or identity differs",
            result["context_warnings"],
        )
        self.assertIn("phase definitions differ", result["context_warnings"])
        for secret in (
            first_match,
            second_match,
            "PRIVATE_IGNORED_first",
            "PRIVATE_IGNORED_second",
            "PRIVATE_PATH_first",
            "PRIVATE_PATH_second",
            "PRIVATE_GITIGNORE_first",
            "PRIVATE_GITIGNORE_second",
            "PRIVATE_KEEP_first",
            "PRIVATE_KEEP_second",
            "PRIVATE_TEXT_first",
            "PRIVATE_TEXT_second",
        ):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, markdown)

    def test_exception_signature_changes_warn_even_when_oracle_mode_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            second = _report()
            for report, message in (
                (first, "PRIVATE_EXCEPTION_FIRST"),
                (second, "PRIVATE_EXCEPTION_SECOND"),
            ):
                report["failure_match"] = "EXCEPTION_MARKER"
                report["failure_spec"] = {
                    "schema_version": 1,
                    "match": "EXCEPTION_MARKER",
                    "exit_code": None,
                    "java_exception": True,
                    "python_exception": False,
                    "process_failure": False,
                }
                report["java_exception_signature"] = {
                    "class": "ValueError",
                    "message": message,
                    "frames": ["private-frame"],
                }
            paths = [
                _write_report(root, "first.json", first),
                _write_report(root, "second.json", second),
            ]
            result = compare_reports(paths)
            serialized = json.dumps(result, sort_keys=True)

        self.assertEqual("java_exception", result["runs"][0]["oracle_mode"])
        self.assertEqual("java_exception", result["runs"][1]["oracle_mode"])
        self.assertIn(
            "failure oracle configuration or identity differs",
            result["context_warnings"],
        )
        self.assertNotIn("PRIVATE_EXCEPTION_FIRST", serialized)
        self.assertNotIn("PRIVATE_EXCEPTION_SECOND", serialized)

    def test_unavailable_version_provenance_is_warned_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            first.pop("repomin_version")
            second = _report()
            second["repomin_version"] = "release-without-semver"
            first_path = _write_report(root, "first.json", first)
            second_path = _write_report(root, "second.json", second)
            result = compare_reports([first_path, second_path])
            serialized = json.dumps(result, sort_keys=True)

        self.assertIsNone(result["runs"][0]["repomin_version"])
        self.assertIsNone(result["runs"][1]["repomin_version"])
        self.assertIn(
            "ReproMin version provenance is unavailable for at least one report",
            result["context_warnings"],
        )
        self.assertNotIn("release-without-semver", serialized)

    def test_labels_are_bounded_unique_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [
                _write_report(root, "one.json", _report()),
                _write_report(root, "two.json", _report()),
            ]
            with self.assertRaisesRegex(ReportComparisonError, "unique"):
                compare_reports(paths, labels=["same", "same"])
            with self.assertRaisesRegex(ReportComparisonError, "exactly once"):
                compare_reports(paths, labels=["one"])
            with self.assertRaisesRegex(ReportComparisonError, "ASCII"):
                compare_reports(paths, labels=["safe", "bad|label"])
            with self.assertRaisesRegex(ReportComparisonError, "ASCII"):
                compare_reports(paths, labels=["safe", "\u4e2d"])

    def test_path_like_strings_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [
                _write_report(root, "one.json", _report()),
                _write_report(root, "two.json", _report()),
            ]
            result = compare_reports([str(path) for path in paths])

        self.assertEqual(2, result["run_count"])

    def test_invalid_input_is_rejected_without_echoing_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "private-command.json"
            invalid.write_text("not json", encoding="utf-8")
            valid = _write_report(root, "valid.json", _report())
            with self.assertRaises(ReportComparisonError) as raised:
                compare_reports([invalid, valid])

        self.assertNotIn(str(invalid), str(raised.exception))
        self.assertIn("report 1", str(raised.exception))

    def test_validation_errors_redact_dynamic_values_and_malicious_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = "PRIVATE_SCHEMA_SENTINEL"
            invalid = root / (sentinel + ".json")
            malformed = _report()
            malformed["schema_version"] = sentinel
            invalid_path = _write_report(root, invalid.name, malformed)
            valid = _write_report(root, "valid.json", _report())
            with self.assertRaises(ReportComparisonError) as raised:
                compare_reports([invalid_path, valid])

        message = str(raised.exception)
        self.assertNotIn(sentinel, message)
        self.assertIn("report 1", message)
        self.assertIn("unsupported report schema_version", message)

    def test_sensitive_report_fields_never_enter_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            second = _report()
            sentinel = "PRIVATE_COMMAND_MATCH_PATH_ENV_SIGNATURE"
            first["command"] = sentinel
            first["failure_match"] = sentinel
            first["execution"]["environment_names"] = [sentinel]
            first["java_exception_signature"] = {
                "class": "ValueError",
                "message": sentinel,
                "frames": ["sentinel-frame"],
            }
            first["output"].update(
                {
                    "tree_sha256": "b" * 64,
                    "tree_fingerprint_policy": "tree-sha256-v2",
                }
            )
            first_path = _write_report(root, "first.json", first)
            second_path = _write_report(root, "second.json", second)
            result = compare_reports([first_path, second_path])
            serialized = json.dumps(result, sort_keys=True)
            markdown = render_comparison_markdown(result)
            text = render_comparison_text(result)

        self.assertNotIn(sentinel, serialized)
        self.assertNotIn(sentinel, markdown)
        self.assertNotIn(sentinel, text)
        self.assertNotIn("tree_sha256", serialized)
        self.assertNotIn("environment_names", serialized)
        self.assertNotIn("report.json", markdown)

    def test_large_counts_do_not_overflow_or_emit_non_json_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            second = _report()
            huge = 10**1000
            first["source"] = {"files": huge, "bytes": huge}
            first["output"] = {"files": huge - 1, "bytes": huge - 1}
            second["source"] = {"files": huge - 2, "bytes": huge - 2}
            second["output"] = {"files": huge - 3, "bytes": huge - 3}
            paths = [
                _write_report(root, "first.json", first),
                _write_report(root, "second.json", second),
            ]
            result = compare_reports(paths)
            encoded = json.dumps(result, allow_nan=False)

        self.assertIsNone(result["runs"][0]["file_retention_ratio"])
        self.assertEqual(-2, result["deltas"][0]["numeric_deltas"]["source_files"])
        self.assertNotIn("NaN", encoded)

    def test_oversized_integer_delta_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            second = _report()
            too_large = 10**2000
            second["source"] = {"files": too_large, "bytes": too_large}
            second["output"] = {"files": 0, "bytes": 0}
            paths = [
                _write_report(root, "first.json", first),
                _write_report(root, "second.json", second),
            ]
            result = compare_reports(paths)

        numeric = result["deltas"][0]["numeric_deltas"]
        self.assertIsNone(numeric["source_files"])
        self.assertIsNone(numeric["source_bytes"])

    def test_zero_denominators_and_nonfinite_deltas_are_null(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            second = _report()
            first["source"] = {"files": 0, "bytes": 0}
            first["output"] = {"files": 0, "bytes": 0}
            second["source"] = {"files": 0, "bytes": 0}
            second["output"] = {"files": 0, "bytes": 0}
            paths = [
                _write_report(root, "first.json", first),
                _write_report(root, "second.json", second),
            ]
            result = compare_reports(paths)
            encoded = json.dumps(result, allow_nan=False)

        self.assertIsNone(result["runs"][0]["file_retention_ratio"])
        self.assertIsNone(result["runs"][0]["byte_retention_ratio"])
        self.assertIsNone(result["deltas"][0]["numeric_deltas"]["file_retention_ratio"])
        self.assertIsNone(result["deltas"][0]["numeric_deltas"]["byte_retention_ratio"])
        self.assertNotIn("NaN", encoded)

    def test_renderers_are_deterministic_and_markdown_has_no_raw_table_injection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _report()
            second = _report()
            second["execution"]["backend"] = "docker"
            paths = [
                _write_report(root, "first.json", first),
                _write_report(root, "second.json", second),
            ]
            result = compare_reports(paths)

        first_markdown = render_comparison_markdown(result)
        second_markdown = render_comparison_markdown(result)
        self.assertEqual(first_markdown, second_markdown)
        self.assertIn("## Context Warnings", first_markdown)
        self.assertIn("execution backends differ", first_markdown)
        self.assertNotIn("PRIVATE", first_markdown)
        self.assertIn("descriptive only", render_comparison_text(result))

    def test_cli_requires_two_reports_and_supports_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _write_report(root, "first.json", _report())
            second = _write_report(root, "second.json", _report())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = main(
                    [
                        "report",
                        "compare",
                        str(first),
                        str(second),
                        "--format",
                        "json",
                    ]
                )
            result = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual(2, result["run_count"])

            one_output = io.StringIO()
            error = io.StringIO()
            with contextlib.redirect_stdout(one_output), contextlib.redirect_stderr(error):
                one_exit = main(["report", "compare", str(first)])

        self.assertEqual(2, one_exit)
        self.assertEqual("", one_output.getvalue())
        self.assertIn("at least two report paths", error.getvalue())

    def test_cli_accepts_interleaved_report_paths_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _write_report(root, "first.json", _report())
            second = _write_report(root, "second.json", _report())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = main(
                    [
                        "report",
                        "compare",
                        str(first),
                        "--label",
                        "before",
                        str(second),
                        "--label",
                        "after",
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(0, exit_code)
        result = json.loads(output.getvalue())
        self.assertEqual(["before", "after"], [run["label"] for run in result["runs"]])


if __name__ == "__main__":
    unittest.main()
