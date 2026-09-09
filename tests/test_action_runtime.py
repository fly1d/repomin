from __future__ import annotations

import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from repomin import _action_runtime as action_runtime


def _environment(workspace: Path, temporary: Path) -> dict[str, str]:
    return {
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_OUTPUT": str(temporary / "github-output.txt"),
        "RUNNER_TEMP": str(temporary),
        "GITHUB_RUN_ID": "17",
        "GITHUB_RUN_ATTEMPT": "2",
        "REPOMIN_CONFIG": "",
        "REPOMIN_SOURCE": ".",
        "REPOMIN_OUTPUT": "",
        "REPOMIN_COMMAND": "reproduce",
        "REPOMIN_MATCH": "failure",
        "REPOMIN_EXIT_CODE": "",
        "REPOMIN_JAVA_EXCEPTION": "false",
        "REPOMIN_PYTHON_EXCEPTION": "false",
        "REPOMIN_PROCESS_FAILURE": "false",
        "REPOMIN_HOLDOUT_RUNS": "",
        "REPOMIN_MIN_HOLDOUT_RATE": "",
        "REPOMIN_HOLDOUT_CONFIDENCE": "",
        "REPOMIN_IGNORE": "",
        "REPOMIN_IGNORE_PATH": "",
        "REPOMIN_KEEP": "",
        "REPOMIN_TEXT_FILE": "",
        "REPOMIN_GITIGNORE": "false",
        "REPOMIN_GITIGNORE_RECURSIVE": "false",
        "REPOMIN_ADAPTER": "auto",
        "REPOMIN_SOURCE_REDUCER": "auto",
        "REPOMIN_BACKEND": "host",
        "REPOMIN_DOCKER_IMAGE": "",
        "REPOMIN_DOCKER_NETWORK": "none",
        "REPOMIN_TIMEOUT": "120",
        "REPOMIN_MAX_ATTEMPTS": "",
        "REPOMIN_MAX_DURATION": "",
        "REPOMIN_JOBS": "1",
        "REPOMIN_STEP_SUMMARY": "false",
    }


def _summary() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_files": 5,
        "source_bytes": 100,
        "output_files": 2,
        "output_bytes": 25,
        "attempts": 9,
        "accepted_mutations": 3,
        "holdout_status": "not_requested",
        "oracle_mode": "exit_code",
        "file_retention_ratio": 0.4,
        "byte_retention_ratio": 0.25,
        "payload_fingerprint_mode": "exact",
        "payload_fingerprint_verified": True,
    }


class ActionPreparationTests(unittest.TestCase):
    def test_requires_every_wired_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = _environment(root, root)
            for name in (*action_runtime._ACTION_INPUTS, "GITHUB_OUTPUT"):
                with self.subTest(name=name):
                    incomplete = dict(environment)
                    del incomplete[name]
                    with self.assertRaisesRegex(
                        action_runtime.ActionInputError, name
                    ):
                        action_runtime._prepare_action(incomplete)

    def test_rejects_escaping_nonportable_and_symlinked_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            outside = root / "outside"
            temporary = root / "temporary"
            workspace.mkdir()
            outside.mkdir()
            temporary.mkdir()
            cases = [
                ("REPOMIN_SOURCE", "..\\outside", "portable"),
                ("REPOMIN_SOURCE", "NUL", "portable"),
                ("REPOMIN_OUTPUT", "result[old]", "portable"),
                ("REPOMIN_OUTPUT", "../outside", "portable"),
            ]
            try:
                (workspace / "escape").symlink_to(
                    outside, target_is_directory=True
                )
            except (NotImplementedError, OSError):
                if sys.platform != "win32":
                    raise
            else:
                cases.extend(
                    (
                        ("REPOMIN_SOURCE", "escape", "symbolic links"),
                        ("REPOMIN_OUTPUT", "escape/result", "symbolic links"),
                    )
                )
            for variable, value, expected in cases:
                with self.subTest(variable=variable, value=value):
                    environment = _environment(workspace, temporary)
                    environment[variable] = value
                    with self.assertRaisesRegex(
                        action_runtime.ActionInputError, expected
                    ):
                        action_runtime._prepare_action(environment)

    def test_rejects_nonportable_or_unsafe_config_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            temporary = root / "temporary"
            workspace.mkdir()
            temporary.mkdir()
            (workspace / "directory.json").mkdir()
            (workspace / "real.json").write_text("{}", encoding="utf-8")
            cases = [
                ("C:/spec.json", "portable"),
                ("./real.json", "portable"),
                ("a//spec.json", "portable"),
                ("bad\x01.json", "portable"),
                ("config/spec:old.json", "portable"),
                ("NUL.json", "portable"),
                ("spec.json.", "portable"),
                ("spec*.json", "portable"),
                ("spec?.json", "portable"),
                ("spec[old].json", "portable"),
                ("missing.json", "must resolve inside"),
                ("directory.json", "readable regular file"),
            ]
            try:
                (workspace / "linked.json").symlink_to(workspace / "real.json")
            except (NotImplementedError, OSError):
                if sys.platform != "win32":
                    raise
            else:
                cases.append(("linked.json", "symbolic links"))
            for config, expected in cases:
                with self.subTest(config=config):
                    environment = _environment(workspace, temporary)
                    environment.update(
                        {
                            "REPOMIN_CONFIG": config,
                            "REPOMIN_COMMAND": "",
                            "REPOMIN_MATCH": "",
                        }
                    )
                    with self.assertRaisesRegex(
                        action_runtime.ActionInputError, expected
                    ):
                        action_runtime._prepare_action(environment)

    def test_config_accepts_only_default_semantic_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "spec.json"
            config.write_text("{}", encoding="utf-8")
            environment = _environment(root, root)
            environment.update(
                {
                    "REPOMIN_CONFIG": "spec.json",
                    "REPOMIN_COMMAND": "",
                    "REPOMIN_MATCH": "",
                }
            )
            plan = action_runtime._prepare_action(environment, pid=41)
            self.assertEqual(
                (
                    str(root.resolve()),
                    "--output",
                    str(root / "repomin-result-17-2-41"),
                    "--config",
                    str(config.resolve()),
                ),
                plan.arguments,
            )

            for name, variable, default in action_runtime._CONFIG_SEMANTIC_DEFAULTS:
                with self.subTest(name=name):
                    conflicting = dict(environment)
                    conflicting[variable] = "changed" if default != "changed" else "other"
                    with self.assertRaisesRegex(
                        action_runtime.ActionInputError, name
                    ):
                        action_runtime._prepare_action(conflicting)

    def test_builds_direct_arguments_without_shell_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            temporary = root / "temporary"
            source = workspace / "source dir"
            workspace.mkdir()
            temporary.mkdir()
            source.mkdir()
            environment = _environment(workspace, temporary)
            environment.update(
                {
                    "REPOMIN_SOURCE": "source dir",
                    "REPOMIN_OUTPUT": "result dir",
                    "REPOMIN_COMMAND": "python reproduce.py --value 'two words'",
                    "REPOMIN_MATCH": "failure text",
                    "REPOMIN_PYTHON_EXCEPTION": "true",
                    "REPOMIN_HOLDOUT_RUNS": "7",
                    "REPOMIN_MIN_HOLDOUT_RATE": "0.8",
                    "REPOMIN_HOLDOUT_CONFIDENCE": "0.95",
                    "REPOMIN_IGNORE": "one\r\ntwo words\n\nthree\r",
                    "REPOMIN_IGNORE_PATH": "nested/noise.txt",
                    "REPOMIN_KEEP": "keep me.txt",
                    "REPOMIN_TEXT_FILE": "data.txt",
                    "REPOMIN_GITIGNORE": "true",
                    "REPOMIN_GITIGNORE_RECURSIVE": "true",
                    "REPOMIN_ADAPTER": "none",
                    "REPOMIN_SOURCE_REDUCER": "python",
                    "REPOMIN_BACKEND": "docker",
                    "REPOMIN_DOCKER_IMAGE": "fixture:latest",
                    "REPOMIN_DOCKER_NETWORK": "bridge",
                    "REPOMIN_TIMEOUT": "30",
                    "REPOMIN_MAX_ATTEMPTS": "50",
                    "REPOMIN_MAX_DURATION": "60",
                    "REPOMIN_JOBS": "2",
                }
            )
            plan = action_runtime._prepare_action(environment)
            self.assertEqual(str(source.resolve()), plan.arguments[0])
            self.assertEqual(
                str((workspace / "result dir").resolve()), plan.arguments[2]
            )
            expected_pairs = {
                "--command": "python reproduce.py --value 'two words'",
                "--match": "failure text",
                "--adapter": "none",
                "--source-reducer": "python",
                "--backend": "docker",
                "--docker-image": "fixture:latest",
                "--docker-network": "bridge",
                "--timeout": "30",
                "--jobs": "2",
                "--holdout-runs": "7",
                "--min-holdout-rate": "0.8",
                "--holdout-confidence": "0.95",
                "--max-attempts": "50",
                "--max-duration": "60",
            }
            for option, value in expected_pairs.items():
                index = plan.arguments.index(option)
                self.assertEqual(value, plan.arguments[index + 1])
            self.assertIn("--python-exception", plan.arguments)
            self.assertIn("--gitignore", plan.arguments)
            self.assertIn("--gitignore-recursive", plan.arguments)
            self.assertEqual(
                ["one", "two words", "three"],
                [
                    plan.arguments[index + 1]
                    for index, item in enumerate(plan.arguments)
                    if item == "--ignore"
                ],
            )

    def test_rejects_invalid_input_combinations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                ({"REPOMIN_COMMAND": "  "}, "command is required"),
                (
                    {"REPOMIN_BACKEND": "docker", "REPOMIN_DOCKER_IMAGE": ""},
                    "docker-image is required",
                ),
                ({"REPOMIN_JAVA_EXCEPTION": "yes"}, "must be true or false"),
                ({"REPOMIN_GITIGNORE": "yes"}, "must be true or false"),
                ({"REPOMIN_STEP_SUMMARY": "yes"}, "must be true or false"),
                (
                    {
                        "REPOMIN_JAVA_EXCEPTION": "true",
                        "REPOMIN_PYTHON_EXCEPTION": "true",
                    },
                    "only one of",
                ),
                (
                    {"REPOMIN_MATCH": "", "REPOMIN_EXIT_CODE": ""},
                    "failure oracle is explicit",
                ),
                (
                    {
                        "REPOMIN_MATCH": "",
                        "REPOMIN_EXIT_CODE": "7",
                        "REPOMIN_PROCESS_FAILURE": "true",
                    },
                    "cannot be combined",
                ),
            )
            for updates, expected in cases:
                with self.subTest(updates=updates):
                    environment = _environment(root, root)
                    environment.update(updates)
                    with self.assertRaisesRegex(
                        action_runtime.ActionInputError, expected
                    ):
                        action_runtime._prepare_action(environment)


class ActionOutputTests(unittest.TestCase):
    def test_summary_outputs_require_fields_and_render_scalars(self) -> None:
        outputs = dict(action_runtime._render_summary_outputs(_summary()))
        self.assertEqual("true", outputs["payload-fingerprint-verified"])
        self.assertEqual("0.4", outputs["file-retention-ratio"])

        missing = _summary()
        del missing["attempts"]
        with self.assertRaisesRegex(
            action_runtime.ActionRuntimeError, "missing an action output field"
        ):
            action_runtime._render_summary_outputs(missing)

        complex_value = _summary()
        complex_value["oracle_mode"] = ["unsafe"]
        with self.assertRaisesRegex(
            action_runtime.ActionRuntimeError, "not scalar: oracle-mode"
        ):
            action_runtime._render_summary_outputs(complex_value)

    def test_step_summary_uses_only_validated_workflow_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_path = root / "summary.md"
            environment = {
                "GITHUB_STEP_SUMMARY": str(summary_path),
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_REPOSITORY": "fly1d/repomin",
                "GITHUB_RUN_ID": "123",
            }
            self.assertEqual(
                str(summary_path),
                action_runtime._write_step_summary(environment, _summary()),
            )
            rendered = summary_path.read_text(encoding="utf-8")
            self.assertIn("# ReproMin validation summary", rendered)
            self.assertIn("https://github.com/fly1d/repomin/actions/runs/123", rendered)

            unsafe_path = root / "unsafe.md"
            unsafe = dict(environment)
            unsafe.update(
                {
                    "GITHUB_STEP_SUMMARY": str(unsafe_path),
                    "GITHUB_REPOSITORY": "fly1d/repomin)\nPRIVATE",
                }
            )
            action_runtime._write_step_summary(unsafe, _summary())
            unsafe_rendered = unsafe_path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE", unsafe_rendered)
            self.assertIn("prepared for upload", unsafe_rendered)

    def test_action_outputs_keep_the_public_order_and_lf_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_file = root / "github-output.txt"
            payload = root / "payload"
            report = root / "payload.repomin" / "report.json"
            plan = action_runtime.ActionPlan((), payload, report, False)
            action_runtime._write_action_outputs(
                {"GITHUB_OUTPUT": str(output_file)},
                plan,
                "",
                _summary(),
            )
            content = output_file.read_bytes()
            self.assertNotIn(b"\r", content)
            lines = content.decode("utf-8").splitlines()
            self.assertEqual(
                [
                    "payload-path=%s" % payload,
                    "report-path=%s" % report,
                    "metadata-path=%s" % report.parent,
                    "step-summary-path=",
                ],
                lines[:4],
            )
            self.assertIn("payload-fingerprint-verified=true", lines)

    def test_run_action_uses_python_argv_and_propagates_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = _environment(root, root)
            with mock.patch.object(
                action_runtime.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 7),
            ) as run:
                self.assertEqual(7, action_runtime.run_action(environment))
            command = run.call_args.args[0]
            self.assertEqual(
                [sys.executable, "-I", "-m", "repomin"], command[:4]
            )
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertEqual(
                str(Path(action_runtime.__file__).resolve().parent),
                run.call_args.kwargs["cwd"],
            )

            with mock.patch.object(
                action_runtime.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], -15),
            ):
                self.assertEqual(143, action_runtime.run_action(environment))

    def test_report_validation_io_errors_keep_cli_exit_code_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = _environment(root, root)
            payload = root / "result"
            report = root / "result.repomin" / "report.json"
            environment["REPOMIN_OUTPUT"] = "result"

            def complete(
                _command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess:
                payload.mkdir()
                report.parent.mkdir()
                report.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess([], 0)

            errors = io.StringIO()
            with (
                contextlib.redirect_stderr(errors),
                mock.patch.object(action_runtime.subprocess, "run", side_effect=complete),
                mock.patch.object(
                    action_runtime,
                    "validate_report_file",
                    side_effect=OSError("unreadable payload"),
                ),
            ):
                self.assertEqual(2, action_runtime.run_action(environment))
            self.assertIn("repomin report: unreadable payload", errors.getvalue())

    def test_run_action_validates_and_emits_one_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = _environment(root, root)
            environment["REPOMIN_OUTPUT"] = "result"
            environment["REPOMIN_STEP_SUMMARY"] = "true"
            environment["GITHUB_STEP_SUMMARY"] = str(root / "summary.md")
            payload = root / "result"
            report = root / "result.repomin" / "report.json"

            def complete(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
                payload.mkdir()
                report.parent.mkdir()
                report.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess([], 0)

            with (
                mock.patch.object(action_runtime.subprocess, "run", side_effect=complete),
                mock.patch.object(action_runtime, "validate_report_file", return_value={}),
                mock.patch.object(action_runtime, "_validation_summary", return_value=_summary()),
            ):
                self.assertEqual(0, action_runtime.run_action(environment))
            output = Path(environment["GITHUB_OUTPUT"]).read_text(encoding="utf-8")
            self.assertIn("payload-path=%s\n" % payload.resolve(), output)
            self.assertIn(
                "step-summary-path=%s\n" % environment["GITHUB_STEP_SUMMARY"],
                output,
            )

    def test_requested_summary_warns_when_runner_path_is_unavailable(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            path = action_runtime._write_step_summary({}, _summary())
        self.assertEqual("", path)
        self.assertIn("GITHUB_STEP_SUMMARY is unavailable", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
