from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Optional, Protocol, Sequence


class TextRemoval(Protocol):
    path: Path
    start: int
    end: int
    content_hash: str


def _resolve_text_target(root: Path, relative: Path) -> Optional[Path]:
    """Validate and return an existing regular file contained beneath ``root``."""
    try:
        resolved_root = root.resolve(strict=True)
        if relative.is_absolute() or relative.drive:
            return None
        candidate = root / relative
        canonical_candidate = resolved_root / relative
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return None
    if (
        resolved != canonical_candidate
        or not resolved.is_file()
        or resolved.is_symlink()
    ):
        return None
    return candidate


def remove_text_targets(root: Path, targets: Sequence[TextRemoval]) -> bool:
    """Validate and apply non-overlapping UTF-8 text removals as one mutation."""
    by_path: Dict[Path, list] = {}
    for target in targets:
        by_path.setdefault(target.path, []).append(target)
    if not by_path:
        return False

    originals: Dict[Path, str] = {}
    transformed: Dict[Path, str] = {}
    for relative, edits in by_path.items():
        path = _resolve_text_target(root, relative)
        if path is None:
            return False
        try:
            with path.open("r", encoding="utf-8", newline="") as stream:
                text = stream.read()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return False
        ordered = sorted(edits, key=lambda item: (item.start, item.end))
        previous_end = -1
        for edit in ordered:
            if edit.start < 0 or edit.end <= edit.start or edit.end > len(text):
                return False
            if edit.start < previous_end:
                return False
            selected = text[edit.start : edit.end].encode("utf-8")
            if hashlib.sha256(selected).hexdigest() != edit.content_hash:
                return False
            previous_end = edit.end
        updated = text
        for edit in reversed(ordered):
            updated = updated[: edit.start] + updated[edit.end :]
        originals[path] = text
        transformed[path] = updated

    attempted = []
    try:
        for path, text in transformed.items():
            relative = path.relative_to(root)
            if _resolve_text_target(root, relative) != path:
                raise OSError("text target changed before mutation: %s" % relative)
            attempted.append(path)
            with path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(text)
    except OSError as write_error:
        rollback_failures = []
        for path in reversed(attempted):
            try:
                relative = path.relative_to(root)
                if _resolve_text_target(root, relative) != path:
                    rollback_failures.append(path)
                    continue
                with path.open("w", encoding="utf-8", newline="") as stream:
                    stream.write(originals[path])
            except (OSError, ValueError):
                rollback_failures.append(path)
        if rollback_failures:
            raise OSError(
                "failed to roll back a partial structured text batch: %s"
                % ", ".join(str(path) for path in rollback_failures)
            ) from write_error
        return False
    return True
