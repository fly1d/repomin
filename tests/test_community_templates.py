import re
from pathlib import Path
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_ISSUE_TEMPLATE_DIR = _ROOT / ".github" / "ISSUE_TEMPLATE"


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if match is None:
        raise AssertionError("missing YAML front matter: " + str(path))
    fields = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip().strip('"')
    return fields


class CommunityTemplateTest(unittest.TestCase):
    def test_issue_templates_have_complete_front_matter(self) -> None:
        required = {"name", "about", "title", "labels", "assignees"}
        paths = sorted(_ISSUE_TEMPLATE_DIR.glob("*.md"))
        self.assertGreaterEqual(len(paths), 1)
        for path in paths:
            fields = _frontmatter(path)
            self.assertTrue(
                required.issubset(fields),
                msg="incomplete issue template metadata: " + str(path),
            )
            for key in ("name", "about"):
                self.assertTrue(fields[key], msg=key + " is empty in " + str(path))

    def test_real_failure_template_collects_evidence_and_privacy_attestations(
        self,
    ) -> None:
        path = _ISSUE_TEMPLATE_DIR / "real_failure.md"
        text = path.read_text(encoding="utf-8").lower()
        for required in (
            "## workflow",
            "## failure contract",
            "## repromin run",
            "## artifact evidence",
            "## authorship and automation",
            "## privacy and redistribution",
            "report validate",
            "material llm or agent involvement",
            "ai-assistance policies",
            "credentials",
            "proprietary source",
        ):
            self.assertIn(required, text)

    def test_adoption_feedback_template_collects_trial_evidence_and_boundaries(
        self,
    ) -> None:
        path = _ISSUE_TEMPLATE_DIR / "adoption_feedback.md"
        text = path.read_text(encoding="utf-8").lower()
        for required in (
            "## workflow",
            "## run",
            "## value and friction",
            "## privacy and redistribution",
            "repomin doctor",
            "report validate",
            "report replay",
            "credentials",
            "proprietary source",
            "current-environment",
            "--format markdown",
            "local report",
            "review and redact",
        ):
            self.assertIn(required, text)
        self.assertNotIn("compact, privacy-safe result", text)

    def test_issue_chooser_has_one_route_per_request_type(self) -> None:
        config = (_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("name: Usage questions", config)
        self.assertIn("github.com/fly1d/repomin/discussions", config)
        self.assertNotIn("issues/new?template=", config)
        self.assertFalse((_ISSUE_TEMPLATE_DIR / "question.md").exists())

    def test_benchmark_proposals_are_not_automatically_starter_tasks(self) -> None:
        fields = _frontmatter(_ISSUE_TEMPLATE_DIR / "benchmark_proposal.md")
        labels = {
            label.strip() for label in fields["labels"].split(",") if label.strip()
        }
        self.assertIn("enhancement", labels)
        self.assertNotIn(
            "good first issue",
            labels,
        )


if __name__ == "__main__":
    unittest.main()
