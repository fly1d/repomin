"""Static checks for the public GitHub Action metadata contract."""

from pathlib import Path
import re
import unittest

from repomin import _action_runtime as action_runtime


ROOT = Path(__file__).resolve().parents[1]


def _uses_references(metadata: str) -> list[str]:
    return [
        value.split("#", 1)[0].strip()
        for value in re.findall(r"(?m)^\s*(?:-\s*)?uses\s*:\s*(\S.*)$", metadata)
    ]


def _named_step(metadata: str, name: str) -> str:
    step = metadata.split("    - name: %s\n" % name, 1)[1]
    return step.split("\n    - name:", 1)[0]


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

    def test_every_runtime_input_is_declared_and_forwarded(self) -> None:
        self.assertEqual(28, len(action_runtime._ACTION_INPUTS))
        for variable in action_runtime._ACTION_INPUTS:
            name = variable.removeprefix("REPOMIN_").lower().replace("_", "-")
            with self.subTest(input=name):
                self.assertIn("  %s:\n" % name, self.action)
                self.assertIn(
                    "        %s: ${{ inputs.%s }}" % (variable, name),
                    self.action,
                )
        self.assertRegex(
            self.action,
            r'(?m)^  command:\n'
            r'    description: .+\n'
            r'    required: false\n'
            r'    default: ""$',
        )
        self.assertRegex(
            self.action,
            r'(?m)^  config:\n'
            r'    description: .+\n'
            r'    required: false\n'
            r'    default: ""$',
        )

    def test_reduce_step_has_one_small_runtime_entrypoint(self) -> None:
        step = _named_step(self.action, "Reduce failing repository")
        self.assertIn("      id: reduce\n", step)
        self.assertIn("      shell: bash\n", step)
        self.assertIn("      working-directory: ${{ github.action_path }}\n", step)
        self.assertIn(
            "      run: '\"$REPOMIN_ACTION_PYTHON\" -I -m "
            "repomin._action_runtime'",
            step,
        )
        self.assertEqual(1, step.count("      run:"))
        self.assertNotIn("run: |", step)
        self.assertNotIn("<<", step)
        self.assertNotIn("resolve_workspace_path", step)

    def test_install_and_runtime_use_a_private_isolated_environment(self) -> None:
        install = _named_step(self.action, "Install ReproMin")
        reduce = _named_step(self.action, "Reduce failing repository")
        for step in (install, reduce):
            self.assertIn("      working-directory: ${{ github.action_path }}\n", step)
        self.assertIn("      id: install\n", install)
        self.assertIn('python -I -m venv "$runtime"', install)
        self.assertIn('"$action_python" -I -m pip install', install)
        self.assertIn(" >> \"$GITHUB_OUTPUT\"", install)
        self.assertIn(
            "REPOMIN_ACTION_PYTHON: ${{ steps.install.outputs.python }}", reduce
        )
        self.assertIn('"$REPOMIN_ACTION_PYTHON" -I -m', reduce)

    def test_public_outputs_are_declared_and_wired(self) -> None:
        expected = {
            "payload-path",
            "report-path",
            "metadata-path",
            "artifact-name",
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
        }
        output_section = self.action.split("outputs:\n", 1)[1].split("inputs:\n", 1)[0]
        actual = set(
            re.findall(r"(?m)^  ([a-z][a-z0-9-]*):\n    description:", output_section)
        )
        self.assertEqual(expected, actual)
        self.assertIn("value: ${{ inputs.artifact-name }}", output_section)
        for name in expected - {"artifact-name"}:
            self.assertIn("value: ${{ steps.reduce.outputs.%s }}" % name, output_section)

        runtime_outputs = {name for name, _key, _required in action_runtime._SUMMARY_OUTPUTS}
        self.assertEqual(
            expected
            - {
                "payload-path",
                "report-path",
                "metadata-path",
                "artifact-name",
                "step-summary-path",
            },
            runtime_outputs,
        )

    def test_step_summary_is_opt_in_and_documented(self) -> None:
        self.assertRegex(
            self.action,
            r'(?m)^  step-summary:\n'
            r'    description: Append a privacy-safe validation summary .+\n'
            r'    required: false\n'
            r'    default: "false"$',
        )
        self.assertIn("REPOMIN_STEP_SUMMARY: ${{ inputs.step-summary }}", self.action)
        self.assertIn("step-summary", self.docs)
        self.assertIn("GITHUB_STEP_SUMMARY", self.docs)

    def test_smoke_workflow_preserves_input_and_output_coverage(self) -> None:
        self.assertIn("  action-smoke:\n", self.workflow)
        self.assertIn("    name: GitHub Action smoke test\n", self.workflow)
        self.assertIn('          exit-code: "7"', self.workflow)
        self.assertNotIn("          match: INPUT_CONTROLS_FAILURE", self.workflow)
        for fragment in (
            "          ignore: |",
            "          ignore-path: nested/deep-noise.txt",
            "          gitignore: true",
            "          gitignore-recursive: true",
            "          keep: keep-me.txt",
            "          text-file: exit-sentinel.txt",
            '          max-attempts: "100"',
            '          max-duration: "120"',
            'execution["keep_paths"] == ["keep-me.txt"]',
            'execution["text_files"] == ["exit-sentinel.txt"]',
            'execution["max_attempts"] == 100',
            'execution["max_duration_seconds"] == 120.0',
        ):
            self.assertIn(fragment, self.workflow)
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
        self.assertIn("| `summary_schema_version` | `2` |", self.workflow)
        self.assertIn("| `oracle_mode` | `exit_code` |", self.workflow)
        self.assertIn("PRIVATE_MATCH_SENTINEL", self.workflow)
        self.assertIn("step-summary-path", self.workflow)

    def test_exception_smoke_rejects_broad_regex_false_positive(self) -> None:
        action_job = self.workflow.split("  action-smoke:\n", 1)[1].split(
            "\n  quality:", 1
        )[0]
        self.assertNotIn("  action-exception-smoke:\n", self.workflow)
        self.assertIn("Prepare Python exception oracle fixture", action_job)
        self.assertEqual(2, action_job.count('raise ValueError("payment failed")'))
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

    def test_docs_describe_action_inputs_and_unstable_output_mode(self) -> None:
        for term in (
            "exit-code",
            "java-exception",
            "python-exception",
            "process-failure",
            "unstable output",
            "holdout-status",
            "min-holdout-rate",
            "ignore-path",
            "gitignore-recursive",
            "max-attempts",
            "max-duration",
            "text-file",
            "keep",
        ):
            self.assertIn(term, self.docs)

    def test_docs_describe_python_runtime_path_behavior(self) -> None:
        self.assertRegex(
            self.action,
            r'(?m)^  python-version:\n'
            r'    description: .+\n'
            r'    required: false\n'
            r'    default: ""$',
        )
        self.assertIn("if: ${{ inputs.python-version != '' }}", self.action)
        self.assertIn("already first on the job's `PATH`", self.docs)
        self.assertIn("not automatically preserved", self.docs)
        self.assertIn("prepended to `PATH`", self.docs)
        self.assertIn("failing job", self.docs)
        self.assertIn(
            'run: \'"$REPOMIN_ACTION_PYTHON" -I -m repomin._action_runtime\'',
            self.action,
        )
        self.assertNotRegex(
            _named_step(self.action, "Reduce failing repository"),
            r"(?m)^\s*repomin(?:\s|$)",
        )
        self.assertIn('python-version: "3.11"', self.workflow)
        self.assertIn('python-version: "3.13"', self.workflow)
        self.assertIn(
            "assert sys.version_info[:2] == (3, 11), sys.version", self.workflow
        )
        self.assertIn(
            "assert sys.version_info[:2] == (3, 13), sys.version", self.workflow
        )

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


if __name__ == "__main__":
    unittest.main()
