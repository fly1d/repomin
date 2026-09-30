from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Sequence, Tuple


class GitignoreError(ValueError):
    """A gitignore-style rule cannot be represented by the supported subset."""


_GLOB_STAR = re.compile(r"\*+")
_GLOB_QUESTION = re.compile(r"\?")


@dataclass(frozen=True)
class GitignoreRule:
    """One compiled gitignore-style rule and its original text.

    The supported subset is deliberately narrower than git's full grammar:

    * blank lines and ``#`` comments are ignored;
    * a leading ``!`` negates an earlier rule;
    * a trailing ``/`` makes a rule directory-only (the directory and every
      descendant match);
    * a leading ``/`` anchors a rule at the rule file's directory;
    * ``*`` does not cross a directory separator while ``**`` does;
    * ``?`` matches one non-separator character;
    * ``[...]`` character classes are supported without escaping.

    Character escaping and rules that contain ``!``, ``#``, or ``[`` only after
    special position are accepted only when the resulting text is unambiguous;
    otherwise the caller should use the explicit ``--ignore-path`` form.
    """

    pattern: str
    regex: "re.Pattern[str]"
    exact_regex: "re.Pattern[str]"
    directory_only: bool
    anchored: bool
    negated: bool
    source: str
    line: int
    base: Tuple[str, ...] = ()


def _translate_glob(pattern: str) -> str:
    """Translate a gitignore glob into a non-capturing regex fragment.

    A standalone ``**`` path segment has Git's special zero-or-more-directory
    meaning.  Handling complete segments here keeps ``foo/**/bar`` compatible
    with both ``foo/bar`` and deeper paths; ordinary ``**`` embedded in a
    segment remains the broad wildcard supported by the documented subset.
    """
    segments = pattern.split("/")
    if any(segment == "**" for segment in segments):
        return _translate_glob_segments(segments)
    return _translate_glob_segment(pattern)


def _translate_glob_segments(segments: Sequence[str]) -> str:
    """Translate path segments while collapsing standalone ``**`` groups."""
    result: List[str] = []
    after_double_star = False
    last_index = len(segments) - 1
    for index, segment in enumerate(segments):
        if segment == "**" and index < last_index:
            if index > 0 and not after_double_star:
                result.append("/")
            result.append("(?:[^/]+/)*")
            after_double_star = True
            continue
        if segment == "**":
            if index > 0 and not after_double_star:
                result.append("/")
            result.append(".*")
            after_double_star = False
            continue
        if index > 0 and not after_double_star:
            result.append("/")
        result.append(_translate_glob_segment(segment))
        after_double_star = False
    return "".join(result)


def _translate_glob_segment(pattern: str) -> str:
    """Translate one path segment into a regex fragment."""
    result: List[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            end = index
            while end < length and pattern[end] == "*":
                end += 1
            # Cross-directory ``**`` is handled only when it is a complete
            # path segment by ``_translate_glob_segments``.  Inside a segment
            # (for example ``a**b``), consecutive stars have the same
            # one-segment behavior as ``*`` and must not consume ``/``.
            result.append("[^/]*")
            index = end
            continue
        if char == "?":
            result.append("[^/]")
            index += 1
            continue
        if char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                raise GitignoreError("unterminated character class: %s" % pattern)
            body = pattern[index + 1 : end]
            if not body:
                raise GitignoreError("empty character class: %s" % pattern)
            if any(special in body for special in ("/", "\x00")):
                raise GitignoreError(
                    "character classes cannot contain a directory separator: %s"
                    % pattern
                )
            # Git accepts both "!" and "^" as the leading negation marker in
            # a character class. Translate either to regex "^" negation.
            # A leading marker is positional, so a following literal "!" or
            # "^" remains an ordinary class member.
            if body[0] in ("!", "^"):
                body = "^" + body[1:]
            elif body.startswith(r"\!") or body.startswith(r"\^"):
                body = body[1:]
            result.append("[" + body + "]")
            index = end + 1
            continue
        if char == "\\":
            if index + 1 >= length:
                raise GitignoreError("trailing escape in pattern: %s" % pattern)
            result.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        if char in "/.":
            result.append(re.escape(char))
            index += 1
            continue
        result.append(re.escape(char))
        index += 1
    return "".join(result)


def _parse_pattern(
    pattern: str,
    source: str,
    line: int,
    base: Tuple[str, ...] = (),
) -> GitignoreRule:
    negated = pattern.startswith("!")
    if negated:
        pattern = pattern[1:]
    directory_only = pattern.endswith("/")
    if directory_only:
        pattern = pattern.rstrip("/")
    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern.lstrip("/")
    if not pattern:
        raise GitignoreError(
            "%s:%d: empty pattern" % (source, line)
        )
    if "\x00" in pattern:
        raise GitignoreError(
            "%s:%d: pattern contains NUL" % (source, line)
        )

    contains_separator = "/" in pattern
    # Git anchors any pattern that contains a directory separator at the
    # rule file's directory, even when the leading "/" is omitted. A
    # separator-free pattern instead matches the basename at every depth.
    if contains_separator:
        anchored = True
    # Keep a capture for the optional descendant portion.  Directory-only
    # rules must not match a regular file at the exact rule path, while the
    # same rule still applies to entries below an ignored directory.
    descendant_suffix = r"(?P<descendant>/.*)?"
    fragment = _translate_glob(pattern)
    if anchored:
        exact_regex = re.compile(r"^%s$" % fragment)
        regex = re.compile(r"^%s%s$" % (fragment, descendant_suffix))
    else:
        exact_regex = re.compile(r"^(?:.*/)?%s$" % fragment)
        regex = re.compile(r"^(?:.*/)?%s%s$" % (fragment, descendant_suffix))
    return GitignoreRule(
        pattern=pattern,
        regex=regex,
        exact_regex=exact_regex,
        directory_only=directory_only,
        anchored=anchored,
        negated=negated,
        source=source,
        line=line,
        base=base,
    )


class GitignoreMatcher:
    """Apply an ordered, file-local gitignore-style rule list."""

    def __init__(self, rules: Sequence[GitignoreRule]) -> None:
        self.rules = tuple(rules)

    @classmethod
    def from_text(
        cls,
        text: str,
        source: str = "<gitignore>",
        base: Tuple[str, ...] = (),
    ) -> "GitignoreMatcher":
        rules: List[GitignoreRule] = []
        for line_number, raw in enumerate(text.splitlines(), start=1):
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            rules.append(_parse_pattern(stripped, source, line_number, base))
        return cls(rules)

    @classmethod
    def from_files(
        cls,
        files: Sequence[Tuple[str, str]],
    ) -> "GitignoreMatcher":
        rules: List[GitignoreRule] = []
        for source, text in files:
            rules.extend(cls.from_text(text, source).rules)
        return cls(rules)

    @classmethod
    def from_scoped_files(
        cls,
        files: Sequence[Tuple[str, Tuple[str, ...], str]],
    ) -> "GitignoreMatcher":
        """Parse rules from a sequence of ``(source, base, text)`` entries.

        Entries are processed in call order, which is the repository's
        top-down directory order for nested ``.gitignore`` files.
        """
        rules: List[GitignoreRule] = []
        for source, base, text in files:
            rules.extend(cls.from_text(text, source, base).rules)
        return cls(rules)

    def matches(
        self,
        relative: PurePosixPath,
        is_directory: Optional[bool] = None,
    ) -> bool:
        """Return whether *relative* is excluded by the ordered rules.

        ``is_directory`` is optional for callers that only have a path.  When
        it is explicitly ``False``, a trailing-slash rule does not match the
        regular file at the exact rule path; descendants still match because
        their parent directory is the ignored target.
        """
        path = relative.as_posix()
        matched = False
        for rule in self.rules:
            if rule.base:
                if relative.parts[: len(rule.base)] != rule.base:
                    continue
                scoped_path = "/".join(relative.parts[len(rule.base) :])
            else:
                scoped_path = path
            rule_match = rule.regex.match(scoped_path)
            if rule_match is None:
                continue
            if rule.directory_only and is_directory is False:
                # A trailing-slash rule applies to a file only when one of
                # its proper ancestors is the ignored directory.  Checking
                # the exact target regex separately is important for patterns
                # whose wildcard can itself consume descendant segments
                # (for example ``foo/**/``).
                if not self._has_matching_directory_ancestor(relative, rule):
                    continue
            if rule.negated:
                matched = False
            else:
                matched = True
        return matched

    @staticmethod
    def _has_matching_directory_ancestor(
        relative: PurePosixPath,
        rule: GitignoreRule,
    ) -> bool:
        parts = relative.parts
        for length in range(1, len(parts)):
            ancestor = parts[:length]
            if rule.base:
                if ancestor[: len(rule.base)] != rule.base:
                    continue
                scoped = "/".join(ancestor[len(rule.base) :])
            else:
                scoped = "/".join(ancestor)
            if rule.exact_regex.match(scoped) is not None:
                return True
        return False

    def may_reinclude_descendant(self, relative: PurePosixPath) -> bool:
        """Return whether a negated rule could affect a child of *relative*.

        Directory walkers may prune an ignored directory, but doing so would
        hide a later negated rule such as ``!generated/keep.txt``.  This check
        is deliberately conservative: a possible glob overlap keeps the
        directory walkable, while the ordinary :meth:`matches` result still
        decides which individual entries are ignored.
        """
        path_parts = relative.parts
        for rule in self.rules:
            if not rule.negated:
                continue
            if rule.base:
                if path_parts[: len(rule.base)] != rule.base:
                    continue
                scoped_parts = path_parts[len(rule.base) :]
            else:
                scoped_parts = path_parts

            pattern_parts = tuple(part for part in rule.pattern.split("/") if part)
            if not scoped_parts:
                return True
            if not pattern_parts:
                continue

            # ``**`` can consume an arbitrary number of path segments.  A
            # simple length comparison would incorrectly prune a directory
            # that is already deeper than the number of literal pattern
            # segments (for example ``!foo/**/keep.txt``).  Check only the
            # prefix before the first double-star segment and conservatively
            # keep walking whenever that prefix can still overlap.
            double_star_index = next(
                (
                    index
                    for index, segment in enumerate(pattern_parts)
                    if "**" in segment
                ),
                None,
            )
            if double_star_index is not None:
                if not rule.anchored:
                    return True
                prefix_parts = pattern_parts[:double_star_index]
                if len(scoped_parts) < len(prefix_parts):
                    actual_prefix = scoped_parts
                else:
                    actual_prefix = scoped_parts[: len(prefix_parts)]
                if all(
                    _glob_segments_overlap(pattern, actual)
                    for pattern, actual in zip(prefix_parts, actual_prefix)
                ):
                    return True
                continue

            # A separator-free, unanchored rule can match a basename at any
            # depth below this directory.  An anchored one only applies when
            # the current path begins with the same (possibly globbed) name.
            if len(pattern_parts) == 1:
                if not rule.anchored:
                    return True
                if _glob_segments_overlap(pattern_parts[0], scoped_parts[0]):
                    return True
                continue

            # Patterns containing a separator are anchored at the rule file's
            # directory.  They can affect a descendant while the current path
            # is a compatible prefix of the pattern.
            if len(scoped_parts) >= len(pattern_parts):
                continue
            if all(
                _glob_segments_overlap(pattern, actual)
                for pattern, actual in zip(pattern_parts, scoped_parts)
            ):
                return True
        return False

    def serialized_rules(self) -> List[dict]:
        return [
            {
                "pattern": rule.pattern,
                "negated": rule.negated,
                "directory_only": rule.directory_only,
                "anchored": rule.anchored,
                "source": rule.source,
                "line": rule.line,
                "base": list(rule.base),
            }
            for rule in self.rules
        ]


def _glob_segments_overlap(pattern: str, actual: str) -> bool:
    """Return whether two one-path-segment patterns may overlap."""
    if any(character in pattern for character in "*?["):
        return True
    return pattern == actual


def _read_gitignore_file(path: Path, source: Path) -> Tuple[Path, str]:
    """Read rules before tree validation without following unsafe source links."""
    path = path.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        relative = path.relative_to(source)
    except ValueError:
        # Explicit external rule files are supported; they are not copied.
        relative = None
    if relative is not None:
        component = source
        pending = list(relative.parts)
        links = 0
        while pending:
            part = pending.pop(0)
            if part == "..":
                # An explicitly configured ../external.ignore is supported.
                component = component.parent
                continue
            candidate = component / part
            status = candidate.lstat()
            if stat.S_ISLNK(status.st_mode):
                links += 1
                if links > 40:
                    raise ValueError("gitignore file has a symbolic-link loop: %s" % path)
                target = Path(os.readlink(candidate))
                if source == component or source in component.parents:
                    try:
                        (component / target).resolve().relative_to(source)
                        contained = True
                    except (OSError, RuntimeError, ValueError):
                        contained = False
                    if target.is_absolute() or not contained:
                        raise ValueError(
                            "gitignore file traverses an unsafe symbolic link: %s" % path
                        )
                if target.is_absolute():
                    component = Path(target.anchor)
                    pending = list(target.parts[1:]) + pending
                else:
                    pending = list(target.parts) + pending
            else:
                if (
                    source == component or source in component.parents
                ) and getattr(status, "st_file_attributes", 0) & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
                ):
                    raise ValueError("gitignore file traverses a reparse point: %s" % path)
                component = candidate
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("gitignore file could not be resolved: %s" % path) from exc
    status = resolved.stat()
    if not stat.S_ISREG(status.st_mode):
        raise ValueError("gitignore file is not a regular file: %s" % resolved)
    if source in resolved.parents and status.st_nlink > 1:
        raise ValueError("gitignore file is hard-linked: %s" % path)
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ValueError("gitignore file could not be read: %s" % resolved) from exc
    try:
        return resolved, data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("gitignore file is not UTF-8: %s" % resolved) from exc


def load_gitignore(
    source: Path,
    enabled: bool,
    files: Sequence[str],
    recursive: bool = False,
    ignore_names: Sequence[str] = (),
    ignore_paths: Sequence[str] = (),
    default_ignores: Iterable[str] = (),
) -> Tuple[Optional[GitignoreMatcher], Sequence[str], Optional[str], bool]:
    """Load the configured gitignore files for a repository.

    The loader is shared by the reduction command and ``repomin doctor`` so
    both commands use identical rule ordering, nested-file discovery, and
    fingerprint metadata.  ``default_ignores`` supplies the reducer's
    built-in exclusions while walking for nested rule files; it is intentionally
    an argument here to avoid a dependency cycle with :mod:`repomin.session`.
    """
    if not enabled and not files and not recursive:
        return None, (), None, False

    source_root = Path(source).expanduser().resolve()
    entries: List[Tuple[str, Tuple[str, ...], str]] = []
    base_entries: List[Tuple[str, Tuple[str, ...], str]] = []
    seen: set[str] = set()

    def add_path(path: Path) -> None:
        resolved, text = _read_gitignore_file(path, source_root)
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        try:
            relative = resolved.relative_to(source_root)
        except ValueError:
            relative = None
        label = relative.as_posix() if relative is not None else str(resolved)
        base = relative.parent.parts if relative is not None else ()
        entry = (label, base, text)
        entries.append(entry)
        base_entries.append(entry)

    if enabled or recursive:
        add_path(source_root / ".gitignore")
    for value in files:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = source_root / path
        add_path(path)

    base_matcher = None
    if base_entries:
        try:
            base_matcher = GitignoreMatcher.from_scoped_files(base_entries)
        except GitignoreError as exc:
            raise ValueError(str(exc)) from exc

    if recursive:
        for label, text in _collect_nested_gitignore(
            source_root,
            ignore_names,
            ignore_paths,
            default_ignores,
            base_matcher,
            loaded_labels=(entry[0] for entry in base_entries),
        ):
            add_path(source_root / label)

    if not entries:
        return None, (), None, recursive

    try:
        matcher = GitignoreMatcher.from_scoped_files(entries)
    except GitignoreError as exc:
        raise ValueError(str(exc)) from exc

    digest = hashlib.sha256()
    for label, _base, text in entries:
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.encode("utf-8"))
        digest.update(b"\0")
    labels = tuple(label for label, _base, _text in entries)
    return matcher, labels, digest.hexdigest(), recursive


def _collect_nested_gitignore(
    source: Path,
    ignore_names: Sequence[str],
    ignore_paths: Sequence[str],
    default_ignores: Iterable[str],
    gitignore: Optional[GitignoreMatcher] = None,
    loaded_labels: Iterable[str] = (),
) -> Sequence[Tuple[str, str]]:
    """Collect nested ``.gitignore`` files in deterministic top-down order."""
    names = set(default_ignores)
    names.update(ignore_names)
    path_parts = tuple(PurePosixPath(path).parts for path in ignore_paths)
    loaded = set(loaded_labels)
    active_rules = list(gitignore.rules) if gitignore is not None else []
    active_matcher = GitignoreMatcher(active_rules)

    def excluded(relative: Path) -> bool:
        parts = relative.parts
        if any(part in names for part in parts):
            return True
        if any(parts[: len(prefix)] == prefix for prefix in path_parts):
            return True
        if not active_rules:
            return False
        posix = PurePosixPath(relative.as_posix())
        return active_matcher.matches(posix, is_directory=True) and not (
            os.path.isdir(source / relative)
            and active_matcher.may_reinclude_descendant(posix)
        )

    result: List[Tuple[str, str]] = []
    for directory, dirnames, filenames in os.walk(
        source,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        if ".gitignore" in filenames:
            path = directory_path / ".gitignore"
            relative = path.relative_to(source)
            label = relative.as_posix()
            if label not in loaded:
                try:
                    _, text = _read_gitignore_file(path, source)
                except (OSError, UnicodeDecodeError) as exc:
                    raise ValueError(
                        "nested gitignore file could not be read: %s" % path
                    ) from exc
                try:
                    parsed = GitignoreMatcher.from_text(
                        text,
                        label,
                        relative.parent.parts,
                    )
                except GitignoreError as exc:
                    raise ValueError(str(exc)) from exc
                active_rules.extend(parsed.rules)
                active_matcher = GitignoreMatcher(active_rules)
                loaded.add(label)
                result.append((label, text))
        kept = []
        for name in sorted(dirnames):
            child = directory_path / name
            relative = child.relative_to(source)
            if excluded(relative):
                continue
            status = child.lstat()
            if getattr(status, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ) and not stat.S_ISLNK(status.st_mode):
                raise ValueError(
                    "nested gitignore discovery cannot traverse a reparse point: %s"
                    % child
                )
            kept.append(name)
        dirnames[:] = kept
    return result
