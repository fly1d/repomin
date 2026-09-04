from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

from repomin.batching import try_interval_batches
from repomin.session import ReductionSession
from repomin.text_edits import _resolve_text_target, remove_text_targets


@dataclass(frozen=True)
class TextLineTarget:
    path: Path
    start: int
    end: int
    label: str
    content_hash: str


class TextReducer:
    """Reduce explicitly selected UTF-8 text files one line range at a time."""

    def __init__(self, session: ReductionSession, paths: Sequence[str]) -> None:
        self.session = session
        self.paths = tuple(sorted(set(paths)))

    def is_applicable(self) -> bool:
        return any(
            _resolve_text_target(self.session.current, Path(path)) is not None
            for path in self.paths
        )

    def reduce(self) -> None:
        if not self.paths:
            return
        with self.session.measure_phase("text"):
            self._reduce()

    def _reduce(self) -> None:
        while True:
            targets = _discover_targets(self.session.current, self.paths)
            if not try_interval_batches(
                self.session,
                "text",
                targets,
                _target_location,
                _describe_targets,
                remove_text_targets,
            ):
                return


def _discover_targets(root: Path, paths: Sequence[str]) -> List[TextLineTarget]:
    targets: List[TextLineTarget] = []
    for relative_text in paths:
        relative = Path(relative_text)
        path = _resolve_text_target(root, relative)
        if path is None:
            continue
        try:
            with path.open("r", encoding="utf-8", newline="") as stream:
                text = stream.read()
        except (OSError, UnicodeDecodeError):
            continue
        offset = 0
        line_number = 0
        for line in text.splitlines(keepends=True):
            line_number += 1
            start = offset
            end = offset + len(line)
            offset = end
            if not line:
                continue
            targets.append(
                TextLineTarget(
                    path=relative,
                    start=start,
                    end=end,
                    label="remove line %d of %s" % (line_number, relative.as_posix()),
                    content_hash=hashlib.sha256(line.encode("utf-8")).hexdigest(),
                )
            )
    return targets


def _target_location(target: TextLineTarget) -> Tuple[Path, int, int]:
    return target.path, target.start, target.end


def _describe_targets(targets: Sequence[TextLineTarget]) -> str:
    paths = sorted({target.path.as_posix() for target in targets})
    rendered = ", ".join(paths[:3]) + ("..." if len(paths) > 3 else "")
    return "remove %d line(s) from %s" % (len(targets), rendered)
