import re
from pathlib import Path
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_ISSUE_TEMPLATE_DIR = _ROOT / ".github" / "ISSUE_TEMPLATE"
_PR_TEMPLATE = _ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
_DISCUSSION_TEMPLATE_DIR = _ROOT / ".github" / "DISCUSSION_TEMPLATE"
_Q_AND_A_TEMPLATE = _DISCUSSION_TEMPLATE_DIR / "q-a.yml"
_SHOW_AND_TELL_TEMPLATE = _DISCUSSION_TEMPLATE_DIR / "show-and-tell.yml"


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


def _level_two_headings(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return re.findall(r"^## ([^\n]+?)\s*$", text, flags=re.MULTILINE)


def _markdown_section(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing section {heading!r} in {path}")
    return match.group(1)


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

    def test_issue_intake_has_three_distinct_routes(self) -> None:
        paths = sorted(_ISSUE_TEMPLATE_DIR.glob("*.md"))
        self.assertEqual(
            {path.name for path in paths},
            {"bug_report.md", "feature_request.md", "real_failure.md"},
        )

        expected_sections = {
            "bug_report.md": {
                "Requested outcome",
                "Context and impact",
                "Evidence and validation",
                "Done when",
                "Safety",
            },
            "feature_request.md": {
                "Requested outcome",
                "Context and impact",
                "Evidence and validation",
                "Done when",
                "Safety",
            },
            "real_failure.md": {
                "Requested outcome",
                "Context and impact",
                "Evidence and validation",
                "Done when",
                "Authorship and automation",
                "Safety and redistribution",
            },
        }
        for path in paths:
            self.assertTrue(
                expected_sections[path.name].issubset(_level_two_headings(path)),
                msg="issue template is missing decision context: " + str(path),
            )

    def test_pull_request_template_is_short_and_review_ready(self) -> None:
        self.assertEqual(
            _level_two_headings(_PR_TEMPLATE),
            ["Why", "What changed", "Verification", "Disclosure"],
        )
        text = _PR_TEMPLATE.read_text(encoding="utf-8").lower()
        for required in (
            "issue or discussion",
            "user problem",
            "exact command",
            "observed result",
            "not run",
            "secrets",
            "documentation and changelog",
            "llm, agent, or automation involvement",
        ):
            self.assertIn(required, text)

    def test_public_submission_templates_state_privacy_boundary(self) -> None:
        paths = sorted(_ISSUE_TEMPLATE_DIR.glob("*.md")) + [
            _PR_TEMPLATE,
            *sorted(_DISCUSSION_TEMPLATE_DIR.glob("*.yml")),
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8").lower()
            self.assertRegex(text, r"secret|credential|token", msg=str(path))
            self.assertRegex(
                text,
                r"private|proprietary|confidential",
                msg=str(path),
            )

    def test_q_and_a_discussion_form_asks_two_questions(self) -> None:
        text = _Q_AND_A_TEMPLATE.read_text(encoding="utf-8")
        labels = re.findall(
            r"^[ \t]+label: ([^\n]+)$", text, flags=re.MULTILINE
        )
        self.assertEqual(labels, ["Question", "Relevant context", "Safety"])
        ids = re.findall(
            r"^[ \t]+- type: (?:textarea|checkboxes)\n[ \t]+id: ([^\n]+)$",
            text,
            flags=re.MULTILINE,
        )
        self.assertEqual(
            len(ids),
            len(set(ids)),
            msg="discussion form IDs must be unique",
        )
        self.assertEqual(text.count("- type: textarea"), 2)
        self.assertEqual(text.count("required: true"), 3)

    def test_show_and_tell_has_two_required_questions_and_one_optional(self) -> None:
        text = _SHOW_AND_TELL_TEMPLATE.read_text(encoding="utf-8")
        labels = re.findall(
            r"^[ \t]+label: ([^\n]+)$", text, flags=re.MULTILINE
        )
        self.assertEqual(
            labels,
            [
                "Result and value",
                "Workflow and evidence",
                "Limits or requested next step",
                "Safety",
            ],
        )
        ids = re.findall(
            r"^[ \t]+- type: (?:textarea|checkboxes)\n[ \t]+id: ([^\n]+)$",
            text,
            flags=re.MULTILINE,
        )
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(text.count("- type: textarea"), 3)
        self.assertEqual(text.count("required: true"), 3)

    def test_community_contract_defines_both_sides_of_a_reply(self) -> None:
        section = _markdown_section(_ROOT / "CONTRIBUTING.md", "Community communication")
        for required in (
            "Requested outcome",
            "Context and impact",
            "Evidence and validation",
            "Done when",
            "Decision",
            "Reason",
            "Next step",
            "Owner / done when",
            "N/A",
            "not headings",
        ):
            self.assertIn(required, section)

    def test_templates_do_not_restore_ambiguous_catch_all_sections(self) -> None:
        paths = sorted(_ISSUE_TEMPLATE_DIR.glob("*.md")) + [_PR_TEMPLATE]
        for path in paths:
            headings = _level_two_headings(path)
            self.assertEqual(len(headings), len(set(headings)), msg=str(path))
            self.assertNotIn("Additional context", headings, msg=str(path))
            self.assertNotIn("Other decision-relevant context", headings, msg=str(path))

    def test_real_failure_template_collects_runnable_or_reviewed_evidence(self) -> None:
        text = (_ISSUE_TEMPLATE_DIR / "real_failure.md").read_text(
            encoding="utf-8"
        ).lower()
        for required in (
            "public, licensed repository/fixture",
            "enough reviewed evidence",
            "target exit code or failure-signature shape",
            "different failure that must be rejected",
            "report validate",
            "material llm or agent involvement",
            "ai-assistance policies",
            "credentials",
            "proprietary source",
        ):
            self.assertIn(required, text)

    def test_improvement_template_keeps_benchmark_decision_context(self) -> None:
        text = (_ISSUE_TEMPLATE_DIR / "feature_request.md").read_text(
            encoding="utf-8"
        ).lower()
        normalized = " ".join(text.split())
        for required in (
            "offline benchmark",
            "target failure",
            "near-match that must be rejected",
            "required tools",
            "expected minimal payload",
            "without network access",
        ):
            self.assertIn(required, normalized)

    def test_issue_chooser_routes_questions_to_structured_discussions(self) -> None:
        config = (_ISSUE_TEMPLATE_DIR / "config.yml").read_text(encoding="utf-8")
        self.assertIn("name: Usage questions", config)
        self.assertIn("discussions/new?category=q-a", config)
        self.assertIn("name: Share a result", config)
        self.assertIn("discussions/new?category=show-and-tell", config)
        self.assertNotIn("issues/new?template=", config)
        self.assertFalse((_ISSUE_TEMPLATE_DIR / "question.md").exists())

    def test_support_routes_each_public_request_once(self) -> None:
        support = (_ROOT / "SUPPORT.md").read_text(encoding="utf-8")
        links = re.findall(r"\| \[[^]]+\]\(([^)]+)\) \|", support)
        self.assertEqual(len(links), 6)
        self.assertEqual(len(links), len(set(links)))
        self.assertEqual(support.count("discussions/new?category=q-a"), 1)
        self.assertEqual(
            support.count("discussions/new?category=show-and-tell"), 1
        )
        for path in sorted(_ISSUE_TEMPLATE_DIR.glob("*.md")):
            self.assertEqual(
                support.count("template=" + path.name),
                1,
                msg="support route is missing or duplicated: " + path.name,
            )
        self.assertNotIn("adoption_feedback.md", support)
        self.assertNotIn("benchmark_proposal.md", support)


if __name__ == "__main__":
    unittest.main()
