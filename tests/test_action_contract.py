"""Static checks for the public GitHub Action metadata contract."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ActionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.action = (ROOT / "action.yml").read_text(encoding="utf-8")
        cls.workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        cls.docs = (ROOT / "docs" / "GITHUB_ACTION.md").read_text(encoding="utf-8")

    def test_failure_oracle_inputs_are_optional_and_forwarded(self) -> None:
        self.assertIn("  match:\n", self.action)
        self.assertIn("    required: false\n    default: \"\"", self.action)
        for name in ("exit-code", "process-failure"):
            self.assertIn("  %s:\n" % name, self.action)
            self.assertIn("REPOMIN_%s" % name.upper().replace("-", "_"), self.action)
        self.assertIn("args+=(--match \"$REPOMIN_MATCH\")", self.action)
        self.assertIn("args+=(--exit-code \"$REPOMIN_EXIT_CODE\")", self.action)
        self.assertIn("args+=(--process-failure)", self.action)

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

    def test_smoke_workflow_exercises_exit_code_and_outputs(self) -> None:
        self.assertIn('exit-code: "7"', self.workflow)
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
        self.assertIn("step-summary: true", self.workflow)
        self.assertIn("# ReproMin validation summary", self.workflow)
        self.assertIn("summary_schema_version", self.workflow)
        self.assertIn("| `summary_schema_version` | `2` |", self.workflow)
        self.assertIn("PRIVATE_MATCH_SENTINEL", self.workflow)
        self.assertIn("step-summary-path", self.workflow)
        self.assertIn("SUMMARY_PATH", self.workflow)

    def test_docs_describe_unstable_output_mode(self) -> None:
        self.assertIn("exit-code", self.docs)
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
