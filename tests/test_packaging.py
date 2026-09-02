"""Regression checks for the source distribution's public packaging contract."""

import ast
import configparser
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]

_RELEASE_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "docs" / "QUICKSTART.md",
    ROOT / "docs" / "QUICKSTART.zh-CN.md",
    ROOT / "docs" / "GITHUB_ACTION.md",
    ROOT / "docs" / "REPLAY.md",
    ROOT / "docs" / "REAL_FAILURE_PILOT.md",
)
_RELEASE_REFERENCE_PATTERNS = (
    re.compile(r"fly1d/repomin@v([0-9][^\s`]*)"),
    re.compile(r"releases/(?:download|tag)/v([0-9][^\s)`]*)"),
    re.compile(r"`v([0-9][^`]*)`\s+(?:pre-release|发布包)"),
    re.compile(r"\bREPOMIN_VERSION\s*=\s*['\"]?([0-9][^'\"\s]+)"),
    re.compile(
        r"\brepomin\s+([0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.-]+)?)"
    ),
)


def _runtime_version() -> str:
    module = ast.parse(
        (ROOT / "src" / "repomin" / "__init__.py").read_text(encoding="utf-8")
    )
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                return value
    raise AssertionError("src/repomin/__init__.py must define a string __version__")


def _setup_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None)
    loaded = config.read(ROOT / "setup.cfg", encoding="utf-8")
    if not loaded:
        raise AssertionError("setup.cfg could not be read")
    return config


class PackagingContractTests(unittest.TestCase):
    def test_metadata_version_is_bound_to_runtime_version(self) -> None:
        config = _setup_config()
        metadata_version = config["metadata"]["version"].strip()
        runtime_version = _runtime_version()

        if metadata_version.startswith("attr:"):
            self.assertEqual("attr: repomin.__version__", metadata_version)
        else:
            self.assertEqual(runtime_version, metadata_version)

    def test_metadata_exposes_project_links_and_platform_support(self) -> None:
        config = _setup_config()
        metadata = config["metadata"]
        self.assertEqual("https://github.com/fly1d/repomin", metadata["url"])
        project_urls = config["metadata"]["project_urls"]
        for label in (
            "Documentation",
            "Repository",
            "Issues",
            "Discussions",
            "Changelog",
        ):
            self.assertIn(label, project_urls)
        self.assertIn("Operating System :: OS Independent", metadata["classifiers"])
        self.assertIn("Programming Language :: Python :: 3.9", metadata["classifiers"])
        self.assertIn("Programming Language :: Python :: 3.13", metadata["classifiers"])
        self.assertIn("Programming Language :: Python :: 3.14", metadata["classifiers"])

    def test_development_extra_contains_release_and_test_tools(self) -> None:
        config = _setup_config()
        development_requirements = config["options.extras_require"]["dev"]
        for package in ("build", "coverage", "pytest", "ruff", "twine"):
            self.assertRegex(
                development_requirements,
                r"(?m)^\s*%s(?:[<>=!~]|\s|$)" % package,
            )

    def test_requirements_fixture_is_included_in_source_manifest(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        for relative in (
            ".gitattributes",
            "scripts/check_contribution.py",
            "scripts/check_docs.py",
            "scripts/check_release_artifacts.py",
            "benchmarks/python-requirements/README.md",
            "benchmarks/python-requirements/requirements.txt",
            "benchmarks/python-requirements/requirements/runtime.txt",
            "benchmarks/python-requirements/requirements/ci.txt",
            "benchmarks/python-requirements/constraints.txt",
            "benchmarks/python-requirements/reproduce.py",
            "benchmarks/report-replay/README.md",
            "benchmarks/report-replay/reproduce.py",
            "benchmarks/report-replay/required.txt",
            "benchmarks/report-replay/noise.txt",
        ):
            self.assertIn("include " + relative, manifest)

    def test_common_text_files_are_pinned_to_lf(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        for pattern in ("*.md", "*.py", "*.yml", "*.txt"):
            self.assertIn(pattern + " text eol=lf", attributes)

    def test_release_document_references_match_runtime_version(self) -> None:
        """Keep install and Action examples aligned when a release is cut."""
        runtime_version = _runtime_version()
        checklist = (ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
        references = []
        for path in _RELEASE_DOCUMENTS:
            relative = path.relative_to(ROOT).as_posix()
            self.assertIn(relative, checklist)
            text = path.read_text(encoding="utf-8")
            for pattern in _RELEASE_REFERENCE_PATTERNS:
                references.extend(pattern.findall(text))

        self.assertTrue(references, "no pinned release references were found")
        self.assertEqual({runtime_version}, set(references))


if __name__ == "__main__":
    unittest.main()
