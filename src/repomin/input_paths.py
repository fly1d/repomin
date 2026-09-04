"""Shared validation for explicit repository input paths."""

from __future__ import annotations

import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional, Sequence

from repomin.gitignore import GitignoreMatcher
from repomin.session import DEFAULT_IGNORES, IgnoreSet, _is_reparse_point


def normalize_ignore_path(value: object) -> str:
    """Return one exact repository-relative path exclusion or protected path."""
    if not isinstance(value, str):
        raise ValueError("ignore path must be text")
    path = value.strip()
    if (
        not path
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or bool(PureWindowsPath(path).drive)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(char in path for char in "*?[")
    ):
        raise ValueError(
            "ignore path must be an exact relative path without glob syntax"
        )
    normalized = PurePosixPath(path).as_posix()
    if normalized in {"", "."}:
        raise ValueError("ignore path must not be the repository root")
    return normalized


def normalize_text_file_path(value: object) -> str:
    """Return one exact repository-relative text-reduction target."""
    if not isinstance(value, str):
        raise ValueError("text file path must be text")
    path = value.strip()
    if (
        not path
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or bool(PureWindowsPath(path).drive)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(char in path for char in "*?[")
    ):
        raise ValueError(
            "text file path must be an exact relative path without glob syntax"
        )
    return PurePosixPath(path).as_posix()


def _lstat_safe_relative_path(
    source: Path,
    value: str,
    *,
    description: str,
    option: str,
):
    """Inspect a relative path without following unsafe parent components."""
    candidate = source
    parts = PurePosixPath(value).parts
    for index, part in enumerate(parts):
        candidate /= part
        try:
            status = candidate.lstat()
        except FileNotFoundError as exc:
            raise ValueError(
                "%s does not exist in the source repository: %s "
                "(check %s %s)" % (description, value, option, value)
            ) from exc
        except OSError as exc:
            raise ValueError(
                "%s could not be inspected: %s (check %s %s)"
                % (description, value, option, value)
            ) from exc

        component = PurePosixPath(*parts[: index + 1]).as_posix()
        if stat.S_ISLNK(status.st_mode):
            raise ValueError(
                "%s must not be a symbolic link or traverse one: %s "
                "(check %s %s)" % (description, component, option, value)
            )
        if _is_reparse_point(status):
            raise ValueError(
                "%s must not be a reparse point or traverse one: %s "
                "(check %s %s)" % (description, component, option, value)
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(status.st_mode):
            raise ValueError(
                "%s has a non-directory parent component: %s "
                "(check %s %s)" % (description, component, option, value)
            )
    return candidate, status


def validate_keep_paths(source: Path, keep_paths: Sequence[str]) -> None:
    """Reject explicit keep paths that cannot protect anything."""
    normalized_paths = sorted({normalize_ignore_path(value) for value in keep_paths})
    for value in normalized_paths:
        _, status = _lstat_safe_relative_path(
            source,
            value,
            description="keep path",
            option="--keep",
        )
        if not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)):
            raise ValueError(
                "keep path must be a regular file or directory: %s "
                "(check --keep %s)" % (value, value)
            )


def validate_text_file_paths(
    source: Path,
    text_files: Sequence[str],
    *,
    ignore_names: Sequence[str] = (),
    ignore_paths: Sequence[str] = (),
    gitignore_matcher: Optional[GitignoreMatcher] = None,
    keep_paths: Sequence[str] = (),
) -> None:
    """Reject explicit text targets that are absent from the effective tree."""
    normalized_keep_paths = sorted(
        {normalize_ignore_path(value) for value in keep_paths}
    )
    ignores = IgnoreSet(
        DEFAULT_IGNORES,
        sorted(set(ignore_paths)),
        gitignore_matcher,
        normalized_keep_paths,
    )
    ignores.update(ignore_names)
    normalized_text_files = sorted(
        {normalize_text_file_path(value) for value in text_files}
    )
    for value in normalized_text_files:
        relative = Path(*PurePosixPath(value).parts)
        candidate, status = _lstat_safe_relative_path(
            source,
            value,
            description="text file",
            option="--text-file",
        )

        if ignores.matches(
            relative,
            is_directory=stat.S_ISDIR(status.st_mode),
        ):
            raise ValueError(
                "text file is excluded by the effective ignore rules: %s "
                "(check --text-file %s or the configured ignore rules)"
                % (value, value)
            )
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(
                "text file must be a regular file: %s (check --text-file %s)"
                % (value, value)
            )
        try:
            with candidate.open("r", encoding="utf-8", newline="") as stream:
                stream.read()
        except UnicodeDecodeError as exc:
            raise ValueError(
                "text file is not UTF-8: %s (check --text-file %s)"
                % (value, value)
            ) from exc
        except OSError as exc:
            raise ValueError(
                "text file could not be read: %s (check --text-file %s)"
                % (value, value)
            ) from exc
