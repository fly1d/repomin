"""Static safety contract for the tag-bound release-candidate workflow."""

from pathlib import Path
import re
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-candidate.yml"


class ReleaseCandidateWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def _named_step(self, name: str) -> str:
        lines = self.workflow.splitlines(keepends=True)
        marker = f"      - name: {name}\n"
        start = lines.index(marker)
        end = len(lines)
        for index, line in enumerate(lines[start + 1 :], start + 1):
            if line.startswith("      - ") or (
                line.startswith("  ") and not line.startswith("    ")
            ):
                end = index
                break
        return "".join(lines[start:end])

    def _named_step_run(self, name: str) -> str:
        step = self._named_step(name)
        marker = "        run: |\n"
        self.assertIn(marker, step)
        return textwrap.dedent(step[step.index(marker) + len(marker) :])

    def _named_job(self, name: str) -> str:
        lines = self.workflow.splitlines(keepends=True)
        marker = f"  {name}:\n"
        start = lines.index(marker)
        end = len(lines)
        for index, line in enumerate(lines[start + 1 :], start + 1):
            if line.startswith("  ") and not line.startswith("    "):
                end = index
                break
        return "".join(lines[start:end])

    def _job_condition(self, name: str) -> str:
        lines = self._named_job(name).splitlines()
        marker = "    if: >-"
        start = lines.index(marker) + 1
        condition = []
        for line in lines[start:]:
            if not line.startswith("      "):
                break
            condition.append(line.strip())
        return " ".join(condition)

    def test_trigger_is_limited_to_tag_pushes(self) -> None:
        self.assertIn('on:\n  push:\n    tags:\n      - "v*"\n', self.workflow)
        for trigger in ("pull_request:", "workflow_dispatch:", "release:", "schedule:"):
            self.assertNotIn(trigger, self.workflow)
        self.assertIn('if [[ "$GITHUB_REF_TYPE" != "tag" ]]', self.workflow)

    def test_tag_is_bound_to_source_and_built_artifact_versions(self) -> None:
        source_check = self.workflow.index("Verify tag matches source version")
        build = self.workflow.index("python -m build --outdir release-dist")
        artifact_check = self.workflow.index("scripts/check_release_artifacts.py")
        installed_test = self.workflow.index("Test exact release candidate")
        storage = self.workflow.index("actions/upload-artifact@")

        self.assertLess(source_check, build)
        self.assertLess(build, artifact_check)
        self.assertLess(artifact_check, installed_test)
        self.assertLess(installed_test, storage)
        self.assertIn("RELEASE_TAG: ${{ github.ref_name }}", self.workflow)
        self.assertIn('"$RELEASE_TAG" != "v$source_version"', self.workflow)
        self.assertIn('--tag "$RELEASE_TAG"', self.workflow)
        self.assertIn("| tee release-record.json", self.workflow)

    def test_checker_receives_the_only_built_wheel_and_sdist(self) -> None:
        self.assertIn("shopt -s nullglob", self.workflow)
        self.assertIn("wheels=(release-dist/*.whl)", self.workflow)
        self.assertIn("sdists=(release-dist/*.tar.gz)", self.workflow)
        self.assertIn("${#wheels[@]} != 1", self.workflow)
        self.assertIn("${#sdists[@]} != 1", self.workflow)
        self.assertIn('"${wheels[0]}"', self.workflow)
        self.assertIn('"${sdists[0]}"', self.workflow)

    def test_exact_built_archives_are_tested_before_storage(self) -> None:
        installed_test = self.workflow.index("Test exact release candidate")
        storage = self.workflow.index("actions/upload-artifact@")
        installed_step = self._named_step("Test exact release candidate")
        storage_step = self._named_step("Store validated release candidate")
        installed_run = self._named_step_run("Test exact release candidate")

        self.assertLess(installed_test, storage)
        for required_step in (installed_step, storage_step):
            self.assertNotIn("\n        if:", required_step)
            self.assertNotIn("continue-on-error", required_step)
        self.assertNotIn("|| true", installed_run)
        self.assertNotIn("|| :", installed_run)
        self.assertNotIn("set +e", installed_run)
        self.assertIn("set -euo pipefail", installed_run)
        self.assertIn("shopt -s nullglob", installed_run)
        self.assertIn("${#wheels[@]} != 1", installed_run)
        self.assertIn("${#sdists[@]} != 1", installed_run)
        self.assertIn('wheel_venv="$test_root/wheel-venv"', installed_run)
        self.assertIn('sdist_venv="$test_root/sdist-venv"', installed_run)
        expected_version = 'expected_version="repomin ${RELEASE_TAG#v}"'
        self.assertEqual(1, installed_run.splitlines().count(expected_version))
        wheel_install_command = (
            '"$wheel_venv/bin/python" -m pip install \\\n'
            '  --no-deps --no-cache-dir "${wheels[0]}"'
        )
        sdist_install_command = (
            '"$sdist_venv/bin/python" -m pip install \\\n'
            '  --no-deps --no-cache-dir "${sdists[0]}"'
        )
        self.assertIn('"$wheel_venv/bin/repomin" --version', installed_run)
        self.assertIn(
            '"$wheel_venv/bin/python" -I -m repomin --version', installed_run
        )
        self.assertIn('"$sdist_venv/bin/repomin" --version', installed_run)
        self.assertIn(
            '"$sdist_venv/bin/python" -I -m repomin --version', installed_run
        )
        self.assertIn('cd "$wheel_run"', installed_run)
        self.assertIn('cd "$sdist_run"', installed_run)
        self.assertIn(
            'assert_installed_package "$wheel_venv/bin/python"', installed_run
        )
        self.assertIn(
            'assert_installed_package "$sdist_venv/bin/python"', installed_run
        )
        self.assertIn("package_path.relative_to(environment_path)", installed_run)
        wheel_check = '"$wheel_venv/bin/python" -m pip check'
        sdist_check = '"$sdist_venv/bin/python" -m pip check'
        wheel_tests = (
            '"$wheel_venv/bin/python" -I -m unittest discover \\\n'
            '  -s "$GITHUB_WORKSPACE/tests" -v'
        )
        sdist_tests = (
            '"$sdist_venv/bin/python" -I -m unittest discover \\\n'
            '  -s "$GITHUB_WORKSPACE/tests" -v'
        )
        run_lines = installed_run.splitlines()
        for command in (
            wheel_install_command,
            sdist_install_command,
            wheel_check,
            sdist_check,
            wheel_tests,
            sdist_tests,
        ):
            self.assertEqual(1, installed_run.count(command))
            self.assertEqual(1, run_lines.count(command.splitlines()[0]))
        for import_check in (
            'assert_installed_package "$wheel_venv/bin/python"',
            'assert_installed_package "$sdist_venv/bin/python"',
        ):
            self.assertEqual(1, run_lines.count(import_check))
        wheel_version_guard = (
            'if [[ "$wheel_console_version" != "$expected_version" || \\\n'
            '      "$wheel_module_version" != "$expected_version" ]]; then'
        )
        sdist_version_guard = (
            'if [[ "$sdist_console_version" != "$expected_version" || \\\n'
            '      "$sdist_module_version" != "$expected_version" ]]; then'
        )
        for guard in (wheel_version_guard, sdist_version_guard):
            guard_start = installed_run.index(guard)
            guard_end = installed_run.index("\nfi", guard_start)
            self.assertIn("\n  exit 1", installed_run[guard_start:guard_end])
        self.assertIn("release-record-after-install.json", installed_run)
        record_guard = 'if ! cmp -s release-record.json "$post_install_record"; then'
        self.assertEqual(1, run_lines.count(record_guard))
        record_guard_start = installed_run.index(record_guard)
        record_guard_end = installed_run.index("\nfi", record_guard_start)
        self.assertIn(
            "\n  exit 1", installed_run[record_guard_start:record_guard_end]
        )
        wheel_install = installed_run.index('"${wheels[0]}"')
        wheel_run = installed_run.index('cd "$wheel_run"')
        wheel_import = installed_run.index(
            'assert_installed_package "$wheel_venv/bin/python"'
        )
        wheel_version = installed_run.index("wheel_console_version=")
        wheel_check_index = installed_run.index(wheel_check)
        wheel_tests_index = installed_run.index(wheel_tests)
        sdist_install = installed_run.index('"${sdists[0]}"')
        sdist_run = installed_run.index('cd "$sdist_run"')
        sdist_import = installed_run.index(
            'assert_installed_package "$sdist_venv/bin/python"'
        )
        sdist_version = installed_run.index("sdist_console_version=")
        sdist_check_index = installed_run.index(sdist_check)
        sdist_tests_index = installed_run.index(sdist_tests)
        final_artifact_check = installed_run.rindex(
            "scripts/check_release_artifacts.py"
        )
        record_comparison = installed_run.index(record_guard)
        wheel_steps = (
            wheel_install,
            wheel_run,
            wheel_import,
            wheel_version,
            wheel_check_index,
            wheel_tests_index,
        )
        sdist_steps = (
            sdist_install,
            sdist_run,
            sdist_import,
            sdist_version,
            sdist_check_index,
            sdist_tests_index,
        )
        self.assertEqual(tuple(sorted(wheel_steps)), wheel_steps)
        self.assertEqual(tuple(sorted(sdist_steps)), sdist_steps)
        self.assertLess(wheel_tests_index, sdist_install)
        self.assertLess(sdist_tests_index, final_artifact_check)
        self.assertLess(final_artifact_check, record_comparison)

    def test_installed_suite_uses_java_11(self) -> None:
        java_setup = (
            "      - uses: actions/setup-java@"
            "b6effb05e454b25005698d916606bdc6ffcbf961 # v5\n"
            "        with:\n"
            "          distribution: temurin\n"
            '          java-version: "11"\n'
        )
        self.assertEqual(1, self.workflow.count(java_setup))
        self.assertLess(
            self.workflow.index(java_setup),
            self.workflow.index("Test exact release candidate"),
        )

    def test_candidate_build_has_no_publication_authority(self) -> None:
        build_job = self._named_job("build")
        self.assertIn("permissions:\n  contents: read\n", self.workflow)
        self.assertIn("persist-credentials: false", build_job)
        self.assertIn("actions/upload-artifact@", build_job)
        self.assertEqual(1, self.workflow.count("actions/upload-artifact@"))
        for forbidden in (
            "contents: write",
            "id-token: write",
            "packages: write",
            "write-all",
            "secrets.",
            "twine upload",
            "gh release",
            "pypa/gh-action-pypi-publish",
            "softprops/action-gh-release",
            "overwrite: true",
        ):
            self.assertNotIn(forbidden, build_job)

    def test_release_actions_are_commit_pinned(self) -> None:
        actions = re.findall(
            r"^\s*(?:-\s+)?uses\s*:\s+([^\s#]+)", self.workflow, re.MULTILINE
        )
        self.assertEqual(9, len(actions))
        for action in actions:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_pypi_candidate_is_revalidated_without_oidc(self) -> None:
        build_job = self._named_job("build")
        verify_job = self._named_job("verify-pypi-candidate")
        expected_condition = (
            "github.repository == 'fly1d/repomin' && "
            "vars.PYPI_PUBLISH_ENABLED == 'true'"
        )
        self.assertEqual(
            expected_condition, self._job_condition("verify-pypi-candidate")
        )
        for required in (
            "    needs: build\n",
            "    permissions:\n"
            "      actions: read\n"
            "      contents: read\n",
            "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
            "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            "actions/download-artifact@"
            "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            "artifact-ids: ${{ needs.build.outputs.candidate-artifact-id }}",
            "path: candidate",
            "scripts/check_release_artifacts.py",
            'cmp -s "$stored_record" "$verified_record"',
        ):
            self.assertIn(required, verify_job)
        self.assertIn("id: store-candidate", build_job)
        self.assertIn(
            "candidate-artifact-id: "
            "${{ steps.store-candidate.outputs.artifact-id }}",
            build_job,
        )
        self.assertNotIn("id-token: write", verify_job)
        self.assertNotIn("environment:", verify_job)

        download = verify_job.index("Download validated release candidate")
        validation = verify_job.index("Revalidate stored release candidate")
        download_step = self._named_step("Download validated release candidate")
        validation_step = self._named_step("Revalidate stored release candidate")
        for step in (download_step, validation_step):
            self.assertNotRegex(step, r"(?m)^\s*if\s*:")
            self.assertNotRegex(step, r"(?m)^\s*continue-on-error\s*:")
        validation_run = self._named_step_run("Revalidate stored release candidate")
        stored_assignment = 'stored_record="candidate/release-record.json"'
        verified_assignment = (
            'verified_record="$RUNNER_TEMP/release-record-before-publish.json"'
        )
        for variable, expected in (
            ("stored_record", stored_assignment),
            ("verified_record", verified_assignment),
        ):
            assignments = [
                line.strip()
                for line in validation_run.splitlines()
                if re.match(rf"^\s*{variable}\s*=", line)
            ]
            self.assertEqual([expected], assignments)
        regular_file_guard = (
            'if [[ ! -f "$stored_record" || -L "$stored_record" ]]; then'
        )
        self.assertEqual(
            1, validation_run.splitlines().count(regular_file_guard)
        )
        record_guard = 'if ! cmp -s "$stored_record" "$verified_record"; then'
        self.assertEqual(1, validation_run.splitlines().count(record_guard))
        guard_start = validation_run.index(record_guard)
        guard_end = validation_run.index("\nfi", guard_start)
        self.assertIn("\n  exit 1", validation_run[guard_start:guard_end])
        validation_order = (
            validation_run.index(stored_assignment),
            validation_run.index(regular_file_guard),
            validation_run.index(verified_assignment),
            validation_run.index("scripts/check_release_artifacts.py"),
            guard_start,
        )
        self.assertEqual(tuple(sorted(validation_order)), validation_order)
        self.assertLess(download, validation)

    def test_pypi_publish_is_dormant_approved_and_oidc_only(self) -> None:
        publish_job = self._named_job("publish-pypi")
        expected_condition = (
            "github.repository == 'fly1d/repomin' && "
            "vars.PYPI_PUBLISH_ENABLED == 'true'"
        )
        self.assertEqual(expected_condition, self._job_condition("publish-pypi"))
        for required in (
            "    needs:\n"
            "      - build\n"
            "      - verify-pypi-candidate\n",
            "    environment:\n      name: pypi\n",
            "    permissions:\n"
            "      actions: read\n"
            "      id-token: write\n",
            "actions/download-artifact@"
            "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            "artifact-ids: ${{ needs.build.outputs.candidate-artifact-id }}",
            "path: candidate",
            "pypa/gh-action-pypi-publish@"
            "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
            "packages-dir: candidate/release-dist/",
            "verify-metadata: true",
            "skip-existing: false",
            "attestations: true",
        ):
            self.assertIn(required, publish_job)
        self.assertEqual(1, self.workflow.count("id-token: write"))
        self.assertEqual(1, self.workflow.count("pypa/gh-action-pypi-publish@"))
        for forbidden in (
            "contents: write",
            "packages: write",
            "secrets",
            "password:",
            "repository-url:",
            "python -m build",
            "twine upload",
            "pip install",
            "actions/checkout",
            "actions/setup-python",
            "actions/upload-artifact",
            "scripts/check_release_artifacts.py",
            "cmp -s",
            "run:",
            "continue-on-error",
            "if: always()",
        ):
            self.assertNotIn(forbidden, publish_job)

        for forbidden_key in ("run", "password", "repository-url"):
            self.assertNotRegex(
                publish_job, rf"(?m)^\s*(?:-\s+)?{forbidden_key}\s*:"
            )
        self.assertNotIn("contents: read", publish_job)
        publish_actions = re.findall(
            r"^\s*(?:-\s+)?uses\s*:\s+([^\s#]+)",
            publish_job,
            re.MULTILINE,
        )
        self.assertEqual(
            [
                "actions/download-artifact@"
                "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
                "pypa/gh-action-pypi-publish@"
                "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
            ],
            publish_actions,
        )
        download = publish_job.index("Download revalidated release candidate")
        publication = publish_job.index(
            "Publish exact candidate with trusted publishing"
        )
        self.assertLess(download, publication)

    def test_run_scoped_artifact_contains_only_candidate_evidence(self) -> None:
        for expected in (
            "release-dist/*.whl",
            "release-dist/*.tar.gz",
            "release-record.json",
            "if-no-files-found: error",
            "retention-days: 14",
        ):
            self.assertIn(expected, self.workflow)


if __name__ == "__main__":
    unittest.main()
