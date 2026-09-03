"""Static safety contract for the tag-bound release-candidate workflow."""

from pathlib import Path
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-candidate.yml"


class ReleaseCandidateWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def _named_step(self, name: str) -> str:
        marker = f"      - name: {name}\n"
        start = self.workflow.index(marker)
        end = self.workflow.find("\n      - name:", start + len(marker))
        if end == -1:
            end = len(self.workflow)
        return self.workflow[start:end]

    def _named_step_run(self, name: str) -> str:
        step = self._named_step(name)
        marker = "        run: |\n"
        self.assertIn(marker, step)
        return textwrap.dedent(step[step.index(marker) + len(marker) :])

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
        storage = self.workflow.index("actions/upload-artifact@v6")

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
        storage = self.workflow.index("actions/upload-artifact@v6")
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
            "      - uses: actions/setup-java@v5\n"
            "        with:\n"
            "          distribution: temurin\n"
            '          java-version: "11"\n'
        )
        self.assertEqual(1, self.workflow.count(java_setup))
        self.assertLess(
            self.workflow.index(java_setup),
            self.workflow.index("Test exact release candidate"),
        )

    def test_workflow_has_no_package_or_release_publication_authority(self) -> None:
        self.assertIn("permissions:\n  contents: read\n", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn("actions/upload-artifact@v6", self.workflow)
        self.assertEqual(1, self.workflow.count("actions/upload-artifact@v6"))
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
            self.assertNotIn(forbidden, self.workflow)

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
