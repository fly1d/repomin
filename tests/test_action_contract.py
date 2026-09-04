"""Static checks for the public GitHub Action metadata contract."""

from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _uses_references(metadata: str) -> list[str]:
    return [
        value.split("#", 1)[0].strip()
        for value in re.findall(r"(?m)^\s*(?:-\s*)?uses\s*:\s*(\S.*)$", metadata)
    ]


def _reduce_script(metadata: str) -> str:
    step = metadata.split("    - name: Reduce failing repository\n", 1)[1]
    block = step.split("      run: |\n", 1)[1].split("\n    - name:", 1)[0]
    return "\n".join(
        line[8:] if line.startswith("        ") else line for line in block.splitlines()
    )


def _action_environment(workspace: Path, temporary: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "GITHUB_WORKSPACE": str(workspace),
            "RUNNER_TEMP": str(temporary),
            "GITHUB_RUN_ID": "1",
            "GITHUB_RUN_ATTEMPT": "1",
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
    )
    return environment


class ActionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.action = (ROOT / "action.yml").read_text(encoding="utf-8")
        cls.workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        cls.docs = (ROOT / "docs" / "GITHUB_ACTION.md").read_text(encoding="utf-8")

    def test_external_actions_are_reviewed_and_commit_pinned(self) -> None:
        expected = [
            "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f",
        ]
        references = _uses_references(self.action)
        external = [reference for reference in references if not reference.startswith("./")]

        self.assertCountEqual(expected, external)
        for reference in external:
            action, marker, revision = reference.rpartition("@")
            self.assertEqual("@", marker)
            self.assertIn("/", action)
            self.assertRegex(revision, r"^[0-9a-f]{40}$")
        self.assertEqual(
            ["owner/action@v1"],
            _uses_references("steps:\n  - uses : owner/action@v1\n"),
        )

    def test_failure_oracle_inputs_are_optional_and_forwarded(self) -> None:
        self.assertRegex(
            self.action,
            r'(?m)^  command:\n'
            r'    description: .+\n'
            r'    required: false\n'
            r'    default: ""$',
        )
        self.assertIn("  match:\n", self.action)
        self.assertIn("    required: false\n    default: \"\"", self.action)
        for name in (
            "exit-code",
            "java-exception",
            "python-exception",
            "process-failure",
        ):
            self.assertIn("  %s:\n" % name, self.action)
            self.assertIn("REPOMIN_%s" % name.upper().replace("-", "_"), self.action)
        self.assertIn("args+=(--match \"$REPOMIN_MATCH\")", self.action)
        self.assertIn("args+=(--exit-code \"$REPOMIN_EXIT_CODE\")", self.action)
        self.assertIn("args+=(--java-exception)", self.action)
        self.assertIn("args+=(--python-exception)", self.action)
        self.assertIn("args+=(--process-failure)", self.action)
        self.assertIn("command is required when config is omitted", self.action)

    @unittest.skipIf(os.name == "nt", "Action composite script requires Bash")
    def test_action_fails_closed_for_empty_command_and_escaping_paths(self) -> None:
        script = _reduce_script(self.action)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            outside = root / "outside"
            temporary = root / "runner-temp"
            workspace.mkdir()
            outside.mkdir()
            temporary.mkdir()

            cases = []
            empty_command = _action_environment(workspace, temporary)
            empty_command.update(
                {"REPOMIN_COMMAND": "  ", "REPOMIN_MATCH": "", "REPOMIN_EXIT_CODE": "0"}
            )
            cases.append(("empty-command", empty_command, "command is required"))

            backslash_source = _action_environment(workspace, temporary)
            backslash_source["REPOMIN_SOURCE"] = "..\\outside"
            cases.append(("backslash-source", backslash_source, "portable"))

            reserved_source = _action_environment(workspace, temporary)
            reserved_source["REPOMIN_SOURCE"] = "NUL"
            cases.append(("reserved-source", reserved_source, "portable"))

            glob_output = _action_environment(workspace, temporary)
            glob_output["REPOMIN_OUTPUT"] = "result[old]"
            cases.append(("glob-output", glob_output, "portable"))

            escape = workspace / "escape"
            escape.symlink_to(outside, target_is_directory=True)
            linked_source = _action_environment(workspace, temporary)
            linked_source["REPOMIN_SOURCE"] = "escape"
            cases.append(("linked-source", linked_source, "symbolic links"))

            linked_output = _action_environment(workspace, temporary)
            linked_output["REPOMIN_OUTPUT"] = "escape/result"
            cases.append(("linked-output", linked_output, "symbolic links"))

            for name, environment, expected in cases:
                with self.subTest(name=name):
                    completed = subprocess.run(
                        ["bash", "-c", script],
                        text=True,
                        capture_output=True,
                        env=environment,
                        check=False,
                    )
                    self.assertEqual(2, completed.returncode, completed.stderr)
                    self.assertIn(expected, completed.stderr)

    @unittest.skipIf(os.name == "nt", "Action composite script requires Bash")
    def test_action_rejects_nonportable_or_unsafe_config_paths(self) -> None:
        script = _reduce_script(self.action)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            temporary = root / "runner-temp"
            workspace.mkdir()
            temporary.mkdir()
            (workspace / "directory.json").mkdir()
            (workspace / "real.json").write_text("{}", encoding="utf-8")
            (workspace / "linked.json").symlink_to(workspace / "real.json")

            cases = (
                ("drive", "C:/spec.json", "portable"),
                ("dot-component", "./real.json", "portable"),
                ("empty-component", "a//spec.json", "portable"),
                ("control-character", "bad\x01.json", "portable"),
                ("nested-colon", "config/spec:old.json", "portable"),
                ("reserved-name", "NUL.json", "portable"),
                ("trailing-period", "spec.json.", "portable"),
                ("glob-star", "spec*.json", "portable"),
                ("glob-question", "spec?.json", "portable"),
                ("glob-bracket", "spec[old].json", "portable"),
                ("symlink", "linked.json", "symbolic links"),
                ("missing", "missing.json", "must resolve inside"),
                ("directory", "directory.json", "readable regular file"),
            )
            for name, config, expected in cases:
                with self.subTest(name=name):
                    environment = _action_environment(workspace, temporary)
                    environment.update(
                        {
                            "REPOMIN_CONFIG": config,
                            "REPOMIN_COMMAND": "",
                            "REPOMIN_MATCH": "",
                        }
                    )
                    completed = subprocess.run(
                        ["bash", "-c", script],
                        text=True,
                        capture_output=True,
                        env=environment,
                        check=False,
                    )
                    self.assertEqual(2, completed.returncode, completed.stderr)
                    self.assertIn(expected, completed.stderr)

    def test_versioned_config_is_isolated_from_semantic_action_inputs(self) -> None:
        self.assertRegex(
            self.action,
            r'(?m)^  config:\n'
            r'    description: .+\n'
            r'    required: false\n'
            r'    default: ""$',
        )
        self.assertIn("REPOMIN_CONFIG: ${{ inputs.config }}", self.action)
        self.assertIn('if [[ -n "$REPOMIN_CONFIG" ]]; then', self.action)
        self.assertIn(
            'f"{label} must be a portable repository-relative path',
            self.action,
        )
        self.assertIn('resolve_workspace_path "$REPOMIN_CONFIG" config', self.action)
        self.assertIn("config must resolve to a readable regular file", self.action)
        self.assertIn(
            "config cannot be combined with non-default semantic inputs",
            self.action,
        )
        for name in (
            "command",
            "match",
            "exit-code",
            "python-exception",
            "adapter",
            "backend",
            "timeout",
            "jobs",
        ):
            self.assertIn("record_config_conflict %s " % name, self.action)
        self.assertIn('args+=(--config "$config_path")', self.action)

    def test_action_confines_source_and_explicit_output_to_the_workspace(self) -> None:
        self.assertIn("resolve_workspace_path()", self.action)
        self.assertIn('source_path="$(resolve_workspace_path', self.action)
        self.assertIn('output_path="$(resolve_workspace_path', self.action)
        self.assertIn("path must not contain symbolic links", self.action)
        self.assertIn("must resolve inside GITHUB_WORKSPACE", self.action)

    def test_exception_mode_inputs_are_strict_booleans_and_mutually_exclusive(
        self,
    ) -> None:
        for name in ("java-exception", "python-exception", "process-failure"):
            self.assertRegex(
                self.action,
                r"(?m)^  %s:\n"
                r"    description: .+\n"
                r"    required: false\n"
                r'    default: "false"$' % re.escape(name),
            )
            self.assertIn(
                '"%s:$REPOMIN_%s"'
                % (name, name.upper().replace("-", "_")),
                self.action,
            )
        self.assertIn('case "$signature_value" in', self.action)
        self.assertIn('true|false) ;;', self.action)
        self.assertIn('$signature_name must be true or false', self.action)
        self.assertIn(
            'signature_mode_count=$((signature_mode_count + 1))', self.action
        )
        self.assertIn(
            "only one of java-exception, python-exception, and process-failure may be true",
            self.action,
        )
        self.assertIn(
            '[[ -z "$REPOMIN_MATCH" && -z "$REPOMIN_EXIT_CODE" '
            '&& "$REPOMIN_PROCESS_FAILURE" != true ]]',
            self.action,
        )
        self.assertIn(
            '[[ -n "$REPOMIN_EXIT_CODE" && "$REPOMIN_PROCESS_FAILURE" == true ]]',
            self.action,
        )

    def test_report_outputs_are_declared_and_emitted(self) -> None:
        for name in (
            "metadata-path",
            "report-schema-version",
            "source-files",
            "source-bytes",
            "output-files",
            "output-bytes",
            "attempts",
            "accepted-mutations",
            "holdout-status",
            "oracle-mode",
            "file-retention-ratio",
            "byte-retention-ratio",
            "payload-fingerprint-mode",
            "payload-fingerprint-verified",
            "step-summary-path",
        ):
            self.assertIn("  %s:\n" % name, self.action)
        self.assertIn('print(f"{name}={value}")', self.action)
        self.assertIn(
            'repomin report validate "$report_path" --payload "$payload_path" --json',
            self.action,
        )
        self.assertIn("generated report is missing an action output field", self.action)
        self.assertIn("from repomin.cli import _validation_summary", self.action)
        self.assertIn('python - "$report_path" "$payload_path" <<\'PY\'', self.action)
        self.assertIn('default: ""', self.action)
        self.assertIn('output_path="$RUNNER_TEMP/repomin-result-', self.action)
        self.assertIn('if [[ -z "${RUNNER_TEMP:-}" ]]; then', self.action)
        self.assertIn("printf 'metadata-path=%s\\n'", self.action)
        self.assertIn("steps.reduce.outputs.metadata-path", self.action)

    def test_step_summary_is_opt_in_and_privacy_safe(self) -> None:
        self.assertIn("  step-summary:\n", self.action)
        self.assertIn(
            "description: Append a privacy-safe validation summary to GITHUB_STEP_SUMMARY",
            self.action,
        )
        self.assertIn('default: "false"', self.action)
        self.assertIn("REPOMIN_STEP_SUMMARY: ${{ inputs.step-summary }}", self.action)
        self.assertIn('case "$REPOMIN_STEP_SUMMARY" in', self.action)
        self.assertIn("step-summary must be true or false", self.action)
        self.assertIn(
            'repomin report validate "$report_path" --payload "$payload_path" --format markdown',
            self.action,
        )
        self.assertIn('>> "$summary_path"', self.action)
        self.assertIn(
            'if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then', self.action
        )
        self.assertIn(
            "step-summary requested but GITHUB_STEP_SUMMARY is unavailable; skipping",
            self.action,
        )
        self.assertIn("GITHUB_SERVER_URL", self.action)
        self.assertIn("GITHUB_REPOSITORY", self.action)
        self.assertIn("GITHUB_RUN_ID", self.action)
        self.assertIn("step-summary-path", self.action)

    def test_holdout_inputs_are_forwarded(self) -> None:
        for name in ("holdout-runs", "min-holdout-rate", "holdout-confidence"):
            self.assertIn("  %s:\n" % name, self.action)
            variable = "REPOMIN_%s" % name.upper().replace("-", "_")
            self.assertIn(variable, self.action)
        self.assertIn('args+=(--holdout-runs "$REPOMIN_HOLDOUT_RUNS")', self.action)
        self.assertIn(
            'args+=(--min-holdout-rate "$REPOMIN_MIN_HOLDOUT_RATE")', self.action
        )
        self.assertIn(
            'args+=(--holdout-confidence "$REPOMIN_HOLDOUT_CONFIDENCE")', self.action
        )

    def test_privacy_exclusion_inputs_are_forwarded_as_repeated_options(self) -> None:
        for name in ("ignore", "ignore-path", "gitignore", "gitignore-recursive"):
            self.assertIn("  %s:\n" % name, self.action)
            variable = "REPOMIN_%s" % name.upper().replace("-", "_")
            self.assertIn(variable, self.action)
        self.assertIn("append_repeated_option --ignore \"$REPOMIN_IGNORE\"", self.action)
        self.assertIn(
            "append_repeated_option --ignore-path \"$REPOMIN_IGNORE_PATH\"",
            self.action,
        )
        self.assertIn("args+=(--gitignore)", self.action)
        self.assertIn("args+=(--gitignore-recursive)", self.action)
        self.assertIn("value=\"${value%$'\\r'}\"", self.action)

    def test_protected_text_and_budget_inputs_are_forwarded(self) -> None:
        for name in ("keep", "text-file", "max-attempts", "max-duration"):
            self.assertIn("  %s:\n" % name, self.action)
            variable = "REPOMIN_%s" % name.upper().replace("-", "_")
            self.assertIn(variable, self.action)
        self.assertIn("append_repeated_option --keep \"$REPOMIN_KEEP\"", self.action)
        self.assertIn(
            "append_repeated_option --text-file \"$REPOMIN_TEXT_FILE\"",
            self.action,
        )
        self.assertIn(
            'args+=(--max-attempts "$REPOMIN_MAX_ATTEMPTS")',
            self.action,
        )
        self.assertIn(
            'args+=(--max-duration "$REPOMIN_MAX_DURATION")',
            self.action,
        )

    def test_smoke_workflow_preserves_exit_code_and_output_coverage(self) -> None:
        self.assertIn("  action-smoke:\n", self.workflow)
        self.assertIn("    name: GitHub Action smoke test\n", self.workflow)
        self.assertIn('          exit-code: "7"', self.workflow)
        self.assertNotIn("          match: INPUT_CONTROLS_FAILURE", self.workflow)
        self.assertIn("          ignore: |", self.workflow)
        self.assertIn("          ignore-path: nested/deep-noise.txt", self.workflow)
        self.assertIn("          gitignore: true", self.workflow)
        self.assertIn("          gitignore-recursive: true", self.workflow)
        self.assertIn("          keep: keep-me.txt", self.workflow)
        self.assertIn("          text-file: exit-sentinel.txt", self.workflow)
        self.assertIn('          max-attempts: "100"', self.workflow)
        self.assertIn('          max-duration: "120"', self.workflow)
        self.assertIn('execution["keep_paths"] == ["keep-me.txt"]', self.workflow)
        self.assertIn(
            'execution["text_files"] == ["exit-sentinel.txt"]', self.workflow
        )
        self.assertIn('execution["max_attempts"] == 100', self.workflow)
        self.assertIn('execution["max_duration_seconds"] == 120.0', self.workflow)
        for name in (
            "ACTUAL_SCHEMA",
            "ACTUAL_SOURCE_FILES",
            "ACTUAL_OUTPUT_FILES",
            "ACTUAL_HOLDOUT",
            "ACTUAL_ORACLE",
            "ACTUAL_FILE_RETENTION",
            "ACTUAL_BYTE_RETENTION",
            "ACTUAL_FINGERPRINT_MODE",
            "ACTUAL_FINGERPRINT_VERIFIED",
        ):
            self.assertIn(name, self.workflow)
        self.assertIn('ACTUAL_HOLDOUT" == "not_requested"', self.workflow)
        self.assertIn('ACTUAL_ORACLE" == "exit_code"', self.workflow)
        self.assertIn("step-summary: true", self.workflow)
        self.assertIn("# ReproMin validation summary", self.workflow)
        self.assertIn("summary_schema_version", self.workflow)
        self.assertIn("| `summary_schema_version` | `2` |", self.workflow)
        self.assertIn("| `oracle_mode` | `exit_code` |", self.workflow)
        self.assertIn("PRIVATE_MATCH_SENTINEL", self.workflow)
        self.assertIn("step-summary-path", self.workflow)
        self.assertIn("SUMMARY_PATH", self.workflow)

    def test_exception_smoke_rejects_broad_regex_false_positive(self) -> None:
        action_job = self.workflow.split("  action-smoke:\n", 1)[1].split(
            "\n  quality:", 1
        )[0]
        self.assertNotIn("  action-exception-smoke:\n", self.workflow)
        self.assertIn("Prepare Python exception oracle fixture", action_job)
        self.assertEqual(
            2,
            action_job.count('raise ValueError("payment failed")'),
        )
        self.assertEqual(3, action_job.count("        uses: ./\n"))
        self.assertIn(
            "          config: action-python-exception-fixture/.repomin.json",
            action_job,
        )
        self.assertIn('"signature": "python_exception"', action_job)
        self.assertIn('"keep_paths": ["required.txt"]', action_job)
        python_action = action_job.split(
            "      - name: Run local ReproMin action with a Python exception oracle\n",
            1,
        )[1].split("      - name: Check exception signature output and payload\n", 1)[0]
        self.assertNotIn("          command:", python_action)
        self.assertNotIn("          match:", python_action)
        self.assertNotIn("          python-exception:", python_action)
        self.assertIn("          artifact-name: action-exception-smoke", action_job)
        self.assertIn('ACTUAL_ORACLE" == "python_exception"', action_job)
        self.assertIn("| `oracle_mode` | `python_exception` |", action_job)
        self.assertIn("! grep -F 'ValueError'", action_job)
        self.assertIn('[[ -f "$PAYLOAD_PATH/required.txt" ]]', action_job)
        self.assertIn('failure_spec["python_exception"] is True', action_job)
        self.assertIn('signature["class"] == "ValueError"', action_job)
        self.assertIn('signature["message"] == "payment failed"', action_job)
        self.assertIn('endswith(":target_failure")', action_job)
        self.assertIn('"fallback_failure" not in frame', action_job)

    def test_action_smoke_exercises_java_exception_input(self) -> None:
        action_job = self.workflow.split("  action-smoke:\n", 1)[1].split(
            "\n  quality:", 1
        )[0]
        self.assertIn("Prepare Java exception oracle fixture", action_job)
        self.assertIn("          match: NoSuchMethodError", action_job)
        self.assertIn('          java-exception: "true"', action_job)
        self.assertIn("          artifact-name: action-java-exception-smoke", action_job)
        self.assertIn('ACTUAL_ORACLE" == "java_exception"', action_job)
        self.assertIn('[[ -f "$PAYLOAD_PATH/required.txt" ]]', action_job)
        self.assertIn('failure_spec["java_exception"] is True', action_job)
        self.assertIn(
            'signature["class"] == "java.lang.NoSuchMethodError"', action_job
        )
        self.assertIn('signature["message"] == "demo.Target.missing()"', action_job)
        self.assertIn('signature["frames"] == ["demo.Target.run"]', action_job)

    def test_docs_describe_unstable_output_mode(self) -> None:
        self.assertIn("exit-code", self.docs)
        self.assertIn("java-exception", self.docs)
        self.assertIn("python-exception", self.docs)
        self.assertIn("process-failure", self.docs)
        self.assertIn("unstable output", self.docs)
        self.assertIn("holdout-status", self.docs)
        self.assertIn("min-holdout-rate", self.docs)
        self.assertIn("ignore-path", self.docs)
        self.assertIn("gitignore-recursive", self.docs)
        self.assertIn("max-attempts", self.docs)
        self.assertIn("max-duration", self.docs)
        self.assertIn("text-file", self.docs)
        self.assertIn("keep", self.docs)
        self.assertIn("step-summary", self.docs)
        self.assertIn("GITHUB_STEP_SUMMARY", self.docs)

    def test_docs_describe_python_runtime_path_behavior(self) -> None:
        self.assertIn("python-version", self.action)
        self.assertIn("prepends that interpreter to `PATH`", self.docs)
        self.assertIn("does not reconstruct the PATH", self.docs)
        self.assertIn("version used", self.docs)
        self.assertIn("failing job", self.docs)

    def test_ci_runs_branch_changes_once_and_keeps_release_tag_coverage(self) -> None:
        trigger = (
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "    tags:\n"
            '      - "v*"\n'
            "  pull_request:\n"
        )
        self.assertIn(trigger, self.workflow)

        lines = self.workflow.splitlines()
        pull_request = lines.index("  pull_request:")
        nested_configuration = []
        for line in lines[pull_request + 1 :]:
            if line and not line.startswith(" "):
                break
            if line.strip():
                nested_configuration.append(line)
        self.assertEqual([], nested_configuration)

    def test_embedded_output_reader_is_indented_inside_yaml_block(self) -> None:
        lines = self.action.splitlines()
        start = lines.index("          python - \"$report_path\" \"$payload_path\" <<'PY'")
        end = lines.index("        } >> \"$GITHUB_OUTPUT\"", start)
        heredoc = lines[start + 1 : end]
        self.assertIn("        import json", heredoc)
        self.assertIn("        PY", heredoc)
        self.assertNotIn("import json", heredoc)
        self.assertNotIn("PY", heredoc)


if __name__ == "__main__":
    unittest.main()
