"""Static safety contract for the tag-bound release-candidate workflow."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-candidate.yml"


class ReleaseCandidateWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_trigger_is_limited_to_tag_pushes(self) -> None:
        self.assertIn('on:\n  push:\n    tags:\n      - "v*"\n', self.workflow)
        for trigger in ("pull_request:", "workflow_dispatch:", "release:", "schedule:"):
            self.assertNotIn(trigger, self.workflow)
        self.assertIn('if [[ "$GITHUB_REF_TYPE" != "tag" ]]', self.workflow)

    def test_tag_is_bound_to_source_and_built_artifact_versions(self) -> None:
        source_check = self.workflow.index("Verify tag matches source version")
        build = self.workflow.index("python -m build --outdir release-dist")
        artifact_check = self.workflow.index("scripts/check_release_artifacts.py")
        storage = self.workflow.index("actions/upload-artifact@v6")

        self.assertLess(source_check, build)
        self.assertLess(build, artifact_check)
        self.assertLess(artifact_check, storage)
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
