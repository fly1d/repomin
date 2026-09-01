import json
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_RUNNER_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "run_offline.py"
_RUNNER_SPEC = importlib.util.spec_from_file_location("repomin_offline_runner", _RUNNER_PATH)
if _RUNNER_SPEC is None or _RUNNER_SPEC.loader is None:
    raise ImportError("could not load the offline benchmark runner")
_RUNNER = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(_RUNNER)
_write_summary = _RUNNER._write_summary
_offline_main = _RUNNER.main
_select_checks = _RUNNER._select_checks
_validate_report = _RUNNER._validate_report


class OfflineBenchmarkSummaryTest(unittest.TestCase):
    def test_validates_fixture_report_against_exported_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            metadata = output.with_name(output.name + ".repomin")
            output.mkdir()
            metadata.mkdir()
            with patch.object(_RUNNER, "validate_report_file") as validate:
                _validate_report(output)
            validate.assert_called_once_with(metadata / "report.json", output)

    def test_list_mode_is_side_effect_free_and_includes_all_fixtures(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, _offline_main(["--list"]))

        names = output.getvalue().splitlines()
        self.assertIn("python-pyproject", names)
        self.assertIn("python-requirements", names)
        self.assertIn("report-replay", names)
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 10)

    @unittest.skipUnless(os.name == "posix", "requires the POSIX python3 fixture command")
    def test_python_requirements_fixture_runs_end_to_end(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                _offline_main(["--only", "python-requirements"]),
            )

        self.assertIn("PASS python-requirements", output.getvalue())

    @unittest.skipUnless(os.name == "posix", "requires the POSIX python3 fixture command")
    def test_report_replay_fixture_runs_end_to_end(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                _offline_main(["--only", "report-replay"]),
            )

        self.assertIn("PASS report-replay", output.getvalue())

    def test_writes_versioned_counts_and_check_details(self) -> None:
        checks = [
            {
                "name": "required",
                "status": "passed",
                "duration_seconds": 0.125,
            },
            {
                "name": "optional-tool",
                "status": "skipped",
                "duration_seconds": 0.001,
                "detail": "tool is not installed",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "results.json"
            _write_summary(path, checks)
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(1, data["schema_version"])
        self.assertEqual(1, data["passed"])
        self.assertEqual(1, data["skipped"])
        self.assertEqual(0, data["failed"])
        self.assertEqual(checks, data["checks"])
        self.assertIn("python", data)
        self.assertIn("platform", data)
        self.assertEqual(_RUNNER.__version__, data["repomin_version"])
        self.assertEqual(
            {"only": [], "exclude": [], "selected": ["required", "optional-tool"]},
            data["selection"],
        )

        selected_path = path.with_name("selected.json")
        _write_summary(selected_path, checks[:1], only=("required",), exclude=("optional-tool",))
        selected = json.loads(selected_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {"only": ["required"], "exclude": ["optional-tool"], "selected": ["required"]},
            selected["selection"],
        )

    def test_select_checks_supports_only_and_exclude(self) -> None:
        checks = [("first", lambda: None), ("second", lambda: None), ("third", lambda: None)]

        selected = _select_checks(checks, ["third", "first"], [])
        self.assertEqual(["first", "third"], [name for name, _ in selected])

        selected = _select_checks(checks, [], ["second"])
        self.assertEqual(["first", "third"], [name for name, _ in selected])

    def test_select_checks_rejects_unknown_and_conflicting_names(self) -> None:
        checks = [("first", lambda: None)]
        with self.assertRaises(SystemExit):
            _select_checks(checks, ["missing"], [])
        with self.assertRaises(SystemExit):
            _select_checks(checks, ["first"], ["first"])
        with self.assertRaises(SystemExit):
            _select_checks(checks, [], ["first"])


if __name__ == "__main__":
    unittest.main()
