import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from repomin.dotnet_manifest import (
    DotnetManifestReducer,
    _discover_targets,
    _remove_target,
)
from repomin.model import FailureSpec, ReductionStats
from repomin.oracle import CommandRunner, FailureOracle
from repomin.session import ReductionSession


CSPROJ = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Required.Core" Version="1.0.0" />
    <PackageReference Include="Unused.Core" Version="2.0.0" />
    <ProjectReference Include="../required/Required.csproj" />
    <ProjectReference Include="../unused/Unused.csproj" />
    <FrameworkReference Include="Microsoft.AspNetCore.App" />
    <Compile Include="Required.cs" />
    <Compile Include="Unused.cs" />
    <None Include="README.md" />
  </ItemGroup>
  <Import Project="$(CustomTargets)" Condition="Exists('$(CustomTargets)')" />
</Project>
"""


DIRECTORY_BUILD_PROPS = """\
<Project>
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Shared.Required" Version="1.0.0" />
    <PackageReference Include="Unused.Shared" Version="2.0.0" />
    <ProjectReference Include="../required/Required.csproj" />
    <ProjectReference Include="../unused/Unused.csproj" />
  </ItemGroup>
  <Import Project="$(CustomTargets)" Condition="Exists('$(CustomTargets)')" />
</Project>
"""


class DotnetManifestReducerTest(unittest.TestCase):
    def test_discovers_items_and_ignores_properties_and_imports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "fixture.csproj"
            path.write_text(CSPROJ, encoding="utf-8")
            targets = _discover_targets(root)
            categories = {target.label: target.category for target in targets}
            self.assertEqual("package-reference", categories["Required.Core"])
            self.assertEqual("project-reference", categories["../unused/Unused.csproj"])
            self.assertEqual("framework-reference", categories["Microsoft.AspNetCore.App"])
            self.assertEqual("compile", categories["Required.cs"])
            labels = {target.label for target in targets}
            self.assertNotIn("net8.0", labels)
            self.assertNotIn("$(CustomTargets)", labels)

    def test_removals_keep_msbuild_xml_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "fixture.csproj"
            for label in ("Required.Core", "Unused.cs", "README.md"):
                path.write_text(CSPROJ, encoding="utf-8")
                target = next(item for item in _discover_targets(root) if item.label == label)
                self.assertTrue(_remove_target(root, target), label)
                document = ET.parse(path)
                self.assertEqual("Project", document.getroot().tag)

    def test_stale_element_hash_rejects_without_modifying_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "fixture.csproj"
            path.write_text(CSPROJ, encoding="utf-8")
            target = next(item for item in _discover_targets(root) if item.label == "Unused.Core")
            shifted = CSPROJ.replace(
                '<PackageReference Include="Unused.Core" Version="2.0.0" />',
                '<PackageReference Include="Unused.Core" Version="2.0.0"><PrivateAssets>all</PrivateAssets></PackageReference>',
            )
            path.write_text(shifted, encoding="utf-8")
            self.assertFalse(_remove_target(root, target))
            self.assertEqual(shifted, path.read_text(encoding="utf-8"))

    def test_malformed_project_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.csproj").write_text(
                "<Project><ItemGroup><PackageReference Include=\"x\"></Project>",
                encoding="utf-8",
            )
            self.assertEqual([], _discover_targets(root))

    def test_entity_declaration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entity.csproj").write_text(
                '<!DOCTYPE Project [<!ENTITY x "expanded">]>'
                '<Project><ItemGroup><PackageReference Include="x" />'
                "</ItemGroup></Project>",
                encoding="utf-8",
            )
            self.assertEqual([], _discover_targets(root))

    def test_namespaced_project_items_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "namespaced.csproj"
            path.write_text(
                '<Project xmlns="urn:msbuild"><ItemGroup>'
                '<PackageReference Include="Required" Version="1" />'
                '</ItemGroup></Project>',
                encoding="utf-8",
            )
            target = next(item for item in _discover_targets(root) if item.label == "Required")
            self.assertTrue(_remove_target(root, target))
            document = ET.parse(path)
            self.assertEqual("Project", document.getroot().tag.rsplit("}", 1)[-1])

    def test_dotnet_only_project_makes_adapter_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fixture.csproj").write_text(CSPROJ, encoding="utf-8")
            session = ReductionSession(
                root,
                FailureOracle(
                    CommandRunner("python3 -c 'raise SystemExit(1)'", timeout_seconds=5),
                    FailureSpec(None, exit_code=1),
                ),
                ReductionStats(source_files=1, source_bytes=0),
            )
            try:
                self.assertTrue(DotnetManifestReducer(session).is_applicable())
            finally:
                session.close()

    def test_reducer_preserves_required_project_and_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "fixture.csproj").write_text(CSPROJ, encoding="utf-8")
            (source / "reproduce.py").write_text(
                "from pathlib import Path\n"
                "text = Path('fixture.csproj').read_text(encoding='utf-8')\n"
                "if 'Required.Core' not in text or '../required/Required.csproj' not in text:\n"
                "    raise SystemExit(2)\n"
                "if '<TargetFramework>net8.0</TargetFramework>' not in text:\n"
                "    raise SystemExit(3)\n"
                "print('ORIGINAL_FAILURE')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            session = ReductionSession(
                source,
                FailureOracle(
                    CommandRunner("python3 reproduce.py", timeout_seconds=5),
                    FailureSpec("ORIGINAL_FAILURE"),
                ),
                ReductionStats(source_files=2, source_bytes=0),
            )
            try:
                session.verify_baseline(1)
                reducer = DotnetManifestReducer(session)
                self.assertTrue(reducer.is_applicable())
                self.assertTrue(reducer.reduce())
                reduced = (session.current / "fixture.csproj").read_text(encoding="utf-8")
                self.assertIn("Required.Core", reduced)
                self.assertNotIn("Unused.Core", reduced)
                self.assertNotIn("../unused/Unused.csproj", reduced)
                self.assertTrue(session.oracle.accepts(session.run_current()))
            finally:
                session.close()


class DirectoryBuildPropsTest(unittest.TestCase):
    """Directory.Build.props reuses the project-file safe path."""

    def test_discovers_shared_items_and_ignores_properties_and_imports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Directory.Build.props").write_text(DIRECTORY_BUILD_PROPS, encoding="utf-8")
            targets = _discover_targets(root)
            categories = {target.label: target.category for target in targets}
            self.assertEqual("package-reference", categories["Shared.Required"])
            self.assertEqual("project-reference", categories["../unused/Unused.csproj"])
            labels = {target.label for target in targets}
            self.assertNotIn("net8.0", labels)
            self.assertNotIn("$(CustomTargets)", labels)

    def test_directory_build_props_alone_makes_adapter_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Directory.Build.props").write_text(DIRECTORY_BUILD_PROPS, encoding="utf-8")
            session = ReductionSession(
                root,
                FailureOracle(
                    CommandRunner("python3 -c 'raise SystemExit(1)'", timeout_seconds=5),
                    FailureSpec(None, exit_code=1),
                ),
                ReductionStats(source_files=1, source_bytes=0),
            )
            try:
                self.assertTrue(DotnetManifestReducer(session).is_applicable())
            finally:
                session.close()

    def test_stale_identity_rejects_directory_build_props_without_modifying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "Directory.Build.props"
            path.write_text(DIRECTORY_BUILD_PROPS, encoding="utf-8")
            target = next(item for item in _discover_targets(root) if item.label == "Unused.Shared")
            shifted = DIRECTORY_BUILD_PROPS.replace(
                '<PackageReference Include="Unused.Shared" Version="2.0.0" />',
                '<PackageReference Include="Unused.Shared" Version="2.0.0"><PrivateAssets>all</PrivateAssets></PackageReference>',
            )
            path.write_text(shifted, encoding="utf-8")
            self.assertFalse(_remove_target(root, target))
            self.assertEqual(shifted, path.read_text(encoding="utf-8"))

    def test_malformed_directory_build_props_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Directory.Build.props").write_text(
                "<Project><ItemGroup><PackageReference Include=\"x\"></Project>",
                encoding="utf-8",
            )
            self.assertEqual([], _discover_targets(root))

    def test_entity_declaration_directory_build_props_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Directory.Build.props").write_text(
                '<!DOCTYPE Project [<!ENTITY x "expanded">]>'
                '<Project><ItemGroup><PackageReference Include="x" />'
                "</ItemGroup></Project>",
                encoding="utf-8",
            )
            self.assertEqual([], _discover_targets(root))

    def test_namespaced_directory_build_props_items_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "Directory.Build.props"
            path.write_text(
                '<Project xmlns="urn:msbuild"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>'
                '<ItemGroup><PackageReference Include="Shared.Required" Version="1" /></ItemGroup></Project>',
                encoding="utf-8",
            )
            target = next(item for item in _discover_targets(root) if item.label == "Shared.Required")
            self.assertTrue(_remove_target(root, target))
            document = ET.parse(path)
            self.assertEqual("Project", document.getroot().tag.rsplit("}", 1)[-1])

    def test_reducer_preserves_required_shared_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "Directory.Build.props").write_text(DIRECTORY_BUILD_PROPS, encoding="utf-8")
            (source / "reproduce.py").write_text(
                "from pathlib import Path\n"
                "text = Path('Directory.Build.props').read_text(encoding='utf-8')\n"
                "if 'Shared.Required' not in text or '../required/Required.csproj' not in text:\n"
                "    raise SystemExit(2)\n"
                "if '<TargetFramework>net8.0</TargetFramework>' not in text:\n"
                "    raise SystemExit(3)\n"
                "print('ORIGINAL_FAILURE')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            session = ReductionSession(
                source,
                FailureOracle(
                    CommandRunner("python3 reproduce.py", timeout_seconds=5),
                    FailureSpec("ORIGINAL_FAILURE"),
                ),
                ReductionStats(source_files=2, source_bytes=0),
            )
            try:
                session.verify_baseline(1)
                reducer = DotnetManifestReducer(session)
                self.assertTrue(reducer.is_applicable())
                self.assertTrue(reducer.reduce())
                reduced = (session.current / "Directory.Build.props").read_text(encoding="utf-8")
                self.assertIn("Shared.Required", reduced)
                self.assertIn("../required/Required.csproj", reduced)
                self.assertNotIn("Unused.Shared", reduced)
                self.assertNotIn("../unused/Unused.csproj", reduced)
                self.assertTrue(session.oracle.accepts(session.run_current()))
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
