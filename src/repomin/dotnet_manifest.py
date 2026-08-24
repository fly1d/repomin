from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from repomin.batching import try_hierarchical_batches
from repomin.session import ReductionSession


@dataclass(frozen=True)
class DotnetManifestTarget:
    project: Path
    category: str
    include: str
    ordinal: int
    label: str
    content_hash: str


_ITEM_CATEGORIES = {
    "PackageReference": "package-reference",
    "ProjectReference": "project-reference",
    "FrameworkReference": "framework-reference",
    "Compile": "compile",
    "EmbeddedResource": "embedded-resource",
    "Content": "content",
    "None": "none",
}


class DotnetManifestReducer:
    """Remove selected MSBuild item entries while the failure is preserved."""

    def __init__(self, session: ReductionSession) -> None:
        self.session = session

    def is_applicable(self) -> bool:
        return bool(_reducible_files(self.session.current))

    def reduce(self) -> bool:
        with self.session.measure_phase("dotnet-manifest"):
            accepted_before = self.session.stats.accepted
            while True:
                targets = _discover_targets(self.session.current)
                if not try_hierarchical_batches(
                    self.session,
                    "dotnet-manifest",
                    targets,
                    _describe_targets,
                    _remove_targets,
                ):
                    break
            return self.session.stats.accepted > accepted_before


def _reducible_files(root: Path) -> List[Path]:
    """Discover MSBuild project files plus shared `Directory.Build.props`.

    Only regular, non-symlink files are eligible so a symlinked manifest
    cannot redirect mutation outside the private reduction tree. The
    `Directory.Build.props` basename is matched exactly so unrelated *.props
    style files do not enter the mutation set.
    """
    paths = []
    for pattern in ("*.csproj", "*.fsproj", "*.vbproj", "Directory.Build.props"):
        paths.extend(
            path
            for path in root.rglob(pattern)
            if path.is_file() and not path.is_symlink()
        )
    return sorted(set(paths))


def _discover_targets(root: Path) -> List[DotnetManifestTarget]:
    root = root.resolve()
    targets: List[DotnetManifestTarget] = []
    for path in _reducible_files(root):
        try:
            document = _parse_project(path)
        except (ET.ParseError, OSError):
            continue
        seen: Dict[Tuple[str, str], int] = {}
        for parent in document.getroot().iter():
            for child in list(parent):
                category = _ITEM_CATEGORIES.get(_local_name(child.tag))
                include = child.attrib.get("Include")
                if category is None or not include:
                    continue
                key = (category, include)
                ordinal = seen.get(key, 0)
                seen[key] = ordinal + 1
                targets.append(
                    DotnetManifestTarget(
                        project=path.relative_to(root),
                        category=category,
                        include=include,
                        ordinal=ordinal,
                        label=include,
                        content_hash=_element_hash(child),
                    )
                )
    priorities = {
        "package-reference": 0,
        "project-reference": 1,
        "framework-reference": 2,
        "compile": 3,
        "embedded-resource": 4,
        "content": 5,
        "none": 6,
    }
    return sorted(
        targets,
        key=lambda item: (
            priorities.get(item.category, 99),
            item.project.as_posix(),
            item.ordinal,
            item.include,
        ),
    )


def _remove_target(root: Path, target: DotnetManifestTarget) -> bool:
    return _remove_targets(root, (target,))


def _remove_targets(root: Path, targets: Sequence[DotnetManifestTarget]) -> bool:
    by_project: Dict[Path, List[DotnetManifestTarget]] = {}
    for target in targets:
        by_project.setdefault(target.project, []).append(target)
    if not by_project:
        return False

    originals: Dict[Path, bytes] = {}
    transformed: Dict[Path, bytes] = {}
    for relative, edits in by_project.items():
        project_path = root / relative
        if project_path.is_symlink():
            return False
        try:
            original = project_path.read_bytes()
            document = _parse_project(project_path)
        except (ET.ParseError, FileNotFoundError, OSError):
            return False
        selected: List[Tuple[ET.Element, ET.Element]] = []
        selected_ids = set()
        for target in edits:
            located = _locate_target(document, target)
            if located is None or id(located[1]) in selected_ids:
                return False
            selected.append(located)
            selected_ids.add(id(located[1]))
        for parent, child in selected:
            parent.remove(child)
        root_element = document.getroot()
        namespace = _namespace(root_element.tag)
        if namespace:
            ET.register_namespace("", namespace)
        if hasattr(ET, "indent"):
            ET.indent(document, space="  ")
        originals[project_path] = original
        transformed[project_path] = ET.tostring(
            root_element,
            encoding="utf-8",
            xml_declaration=True,
        )

    attempted: List[Path] = []
    try:
        for project_path, data in transformed.items():
            attempted.append(project_path)
            project_path.write_bytes(data)
    except OSError as write_error:
        rollback_failures = []
        for project_path in reversed(attempted):
            try:
                project_path.write_bytes(originals[project_path])
            except OSError:
                rollback_failures.append(project_path)
        if rollback_failures:
            raise OSError(
                "failed to roll back a partial MSBuild batch: %s"
                % ", ".join(str(path) for path in rollback_failures)
            ) from write_error
        return False
    return True


def _locate_target(
    document: ET.ElementTree,
    target: DotnetManifestTarget,
) -> Optional[Tuple[ET.Element, ET.Element]]:
    occurrence = 0
    for parent in document.getroot().iter():
        for child in list(parent):
            category = _ITEM_CATEGORIES.get(_local_name(child.tag))
            if category != target.category or child.attrib.get("Include") != target.include:
                continue
            if occurrence == target.ordinal:
                if _element_hash(child) != target.content_hash:
                    return None
                return parent, child
            occurrence += 1
    return None


def _describe_targets(targets: Sequence[DotnetManifestTarget]) -> str:
    if len(targets) == 1:
        target = targets[0]
        return "remove MSBuild %s %s from %s" % (
            target.category,
            target.label,
            target.project,
        )
    labels = ", ".join(target.label for target in targets[:3])
    if len(targets) > 3:
        labels += ", ..."
    return "remove %d MSBuild item entries: %s" % (len(targets), labels)


def _element_hash(element: ET.Element) -> str:
    return hashlib.sha256(ET.tostring(element, encoding="utf-8")).hexdigest()


def _parse_project(path: Path) -> ET.ElementTree:
    data = path.read_bytes()
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ET.ParseError("MSBuild project contains a forbidden XML declaration")
    return ET.ElementTree(ET.fromstring(data))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return ""
