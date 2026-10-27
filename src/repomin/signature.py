from __future__ import annotations

import posixpath
import re
import signal
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Pattern, Sequence

from repomin.model import (
    PYTHON_FRAME_POLICY,
    JavaExceptionSignature,
    ProcessFailureSignature,
    PythonExceptionSignature,
    RunResult,
)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LOG_PREFIX = re.compile(r"^(?:\[[^\]]+\]\s*)+")
_EXCEPTION = re.compile(
    r"(?P<class>(?:[A-Za-z_$][\w$]*\.)*[A-Za-z_$][\w$]*"
    r"(?:Exception|Error|Throwable|Failure))(?::\s*(?P<message>.*))?$"
)
_FRAME = re.compile(r"^\s*at\s+(?P<method>[^\s(]+)\s*\(")
_PYTHON_TRACEBACK = re.compile(
    r"^(?:Exception Group )?Traceback \(most recent call last\):$"
)
_PYTHON_FRAME = re.compile(
    r'^File ["\'](?P<path>.+?)["\'], line \d+(?:, in (?P<function>.+))?$'
)
_PYTHON_EXCEPTION = re.compile(
    r"^(?P<class>(?:[A-Za-z_]\w*\.)*[A-Z][A-Za-z0-9_]*)"
    r"(?::\s*(?P<message>.*))?$"
)
_PYTEST_EXCEPTION = re.compile(
    r"^E\s+(?P<class>(?:[A-Za-z_]\w*\.)*[A-Z][A-Za-z0-9_]*)"
    r"(?::\s*(?P<message>.*))?$"
)
_PYTEST_LOCATION = re.compile(
    r"^(?P<path>(?:[A-Za-z]:)?[^:\n]+\.py):\d+"
    r"(?:: in (?P<function>[^:]+))?(?:: [A-Z][A-Za-z0-9_.]*)?$"
)
_MAX_REPORT_FILES = 200
_MAX_REPORT_BYTES = 4 * 1024 * 1024
_MAX_FRAMES = 3
_WINDOWS_STATUS_NAMES = {
    0xC0000005: "EXCEPTION_ACCESS_VIOLATION",
    0xC000001D: "EXCEPTION_ILLEGAL_INSTRUCTION",
    0xC0000094: "EXCEPTION_INT_DIVIDE_BY_ZERO",
    0xC00000FD: "EXCEPTION_STACK_OVERFLOW",
    0xC0000135: "STATUS_DLL_NOT_FOUND",
    0xC0000139: "STATUS_ENTRYPOINT_NOT_FOUND",
    0xC0000142: "STATUS_DLL_INIT_FAILED",
    0xC0000374: "STATUS_HEAP_CORRUPTION",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN",
}


@dataclass
class _ExceptionBlock:
    class_name: str
    message: str
    caused_by: bool
    suppressed: bool
    header: str
    frames: List[str] = field(default_factory=list)

    def signature(self) -> JavaExceptionSignature:
        return JavaExceptionSignature(
            class_name=self.class_name,
            message=self.message,
            frames=tuple(self.frames[:_MAX_FRAMES]),
        )


@dataclass
class _PythonExceptionBlock:
    class_name: str
    message: str
    header: str
    frames: List[str] = field(default_factory=list)

    def signature(self, rooted: bool = False) -> PythonExceptionSignature:
        innermost = list(reversed(self.frames[-_MAX_FRAMES:]))
        return PythonExceptionSignature(
            class_name=self.class_name,
            message=self.message,
            frames=tuple(innermost),
            normalization_policy=PYTHON_FRAME_POLICY if rooted else None,
        )


def extract_process_failure(
    result: RunResult,
) -> Optional[ProcessFailureSignature]:
    """Normalize a completed process termination without guessing from output."""
    if result.timed_out or result.resource_exhausted or result.returncode == 0:
        return None

    returncode = result.returncode
    if returncode < 0:
        signal_number = -returncode
        if signal_number in signal.valid_signals():
            return ProcessFailureSignature("posix_signal", signal_number)
        if -(2**31) <= returncode:
            unsigned = returncode & 0xFFFFFFFF
            if unsigned >= 0x80000000:
                return ProcessFailureSignature("windows_status", unsigned)
    if 0x80000000 <= returncode <= 0xFFFFFFFF:
        return ProcessFailureSignature("windows_status", returncode)
    return ProcessFailureSignature("exit_code", returncode)


def process_failure_name(signature: ProcessFailureSignature) -> Optional[str]:
    if signature.kind == "posix_signal":
        try:
            return signal.Signals(signature.code).name
        except ValueError:
            return None
    if signature.kind == "windows_status":
        return _WINDOWS_STATUS_NAMES.get(signature.code)
    return None


def format_process_failure_signature(signature: ProcessFailureSignature) -> str:
    name = process_failure_name(signature)
    if signature.kind == "posix_signal":
        return "POSIX signal %s (%d)" % (name or "<unknown>", signature.code)
    if signature.kind == "windows_status":
        label = "Windows status 0x%08X" % signature.code
        return "%s (%s)" % (label, name) if name else label
    return "exit code %d" % signature.code


def valid_process_failure_signature(signature: ProcessFailureSignature) -> bool:
    if signature.kind == "posix_signal":
        return signature.code in signal.valid_signals()
    if signature.kind == "windows_status":
        return 0x80000000 <= signature.code <= 0xFFFFFFFF
    if signature.kind != "exit_code" or signature.code == 0:
        return False
    normalized = extract_process_failure(
        RunResult(signature.code, "", "", 0.0)
    )
    return normalized == signature


def valid_recorded_process_failure_signature(
    signature: ProcessFailureSignature,
) -> bool:
    """Validate a portable report signature without consulting the host OS."""
    if signature.kind == "posix_signal":
        return 1 <= signature.code <= 255
    if signature.kind == "windows_status":
        return 0x80000000 <= signature.code <= 0xFFFFFFFF
    if signature.kind == "exit_code":
        return 1 <= signature.code < 0x80000000
    return False


def extract_run_java_exception(
    result: RunResult,
    pattern: Optional[Pattern[str]] = None,
) -> Optional[JavaExceptionSignature]:
    if result.diagnostics:
        blocks = _java_exception_blocks(result.diagnostics)
        if any(block.frames and not block.suppressed for block in blocks):
            return _select_java_exception(blocks, pattern)
    return extract_java_exception(result.output, pattern)


def extract_java_exception(
    text: str,
    pattern: Optional[Pattern[str]] = None,
) -> Optional[JavaExceptionSignature]:
    return _select_java_exception(_java_exception_blocks(text), pattern)


def _java_exception_blocks(text: str) -> List[_ExceptionBlock]:
    blocks: List[_ExceptionBlock] = []
    current: Optional[_ExceptionBlock] = None
    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        frame_match = _FRAME.match(line)
        if frame_match is not None and current is not None:
            current.frames.append(_normalize_frame(frame_match.group("method")))
            continue

        exception_match = _EXCEPTION.search(line)
        if exception_match is None:
            continue
        prefix = line[: exception_match.start()]
        current = _ExceptionBlock(
            class_name=exception_match.group("class"),
            message=_normalize_message(exception_match.group("message") or ""),
            caused_by="Caused by:" in prefix,
            suppressed="Suppressed:" in prefix,
            header=line,
        )
        blocks.append(current)

    return blocks


def _select_java_exception(
    blocks: Sequence[_ExceptionBlock],
    pattern: Optional[Pattern[str]],
) -> Optional[JavaExceptionSignature]:
    candidates = [block for block in blocks if block.frames and not block.suppressed]
    if not candidates:
        return None

    if pattern is None:
        caused = [block for block in candidates if block.caused_by]
        return (caused[-1] if caused else candidates[0]).signature()

    groups: List[List[_ExceptionBlock]] = []
    for block in blocks:
        if block.suppressed:
            continue
        if not block.caused_by or not groups:
            groups.append([])
        if block.frames:
            groups[-1].append(block)

    matching_groups = [
        group
        for group in groups
        if any(pattern.search(_java_block_text(block)) is not None for block in group)
    ]
    selected_groups = matching_groups or groups
    signatures = []
    for group in selected_groups:
        if not group:
            continue
        caused = [block for block in group if block.caused_by]
        signature = (caused[-1] if caused else group[0]).signature()
        if signature not in signatures:
            signatures.append(signature)
    return signatures[0] if len(signatures) == 1 else None


def _java_block_text(block: _ExceptionBlock) -> str:
    return "%s\n%s: %s\n%s" % (
        block.header,
        block.class_name,
        block.message,
        "\n".join(block.frames),
    )


def extract_run_python_exception(
    result: RunResult,
    pattern: Optional[Pattern[str]] = None,
    use_execution_root: bool = True,
) -> Optional[PythonExceptionSignature]:
    return extract_python_exception(
        result.output, pattern,
        execution_root=result.python_execution_root if use_execution_root else None,
    )


def extract_python_exception(
    text: str,
    pattern: Optional[Pattern[str]] = None,
    *,
    execution_root: Optional[str] = None,
) -> Optional[PythonExceptionSignature]:
    blocks: List[_PythonExceptionBlock] = []
    active_frames: Optional[List[str]] = None
    pending_pytest: Optional[_PythonExceptionBlock] = None

    for raw_line in text.splitlines():
        line = _clean_python_line(raw_line)
        if _PYTHON_TRACEBACK.match(line):
            active_frames = []
            pending_pytest = None
            continue

        if active_frames is not None:
            frame_match = _PYTHON_FRAME.match(line)
            if frame_match is not None:
                active_frames.append(
                    _python_frame(
                        frame_match.group("path"),
                        frame_match.group("function") or "<module>",
                        execution_root,
                    )
                )
                continue
            exception_match = _PYTHON_EXCEPTION.match(line)
            if exception_match is not None and active_frames:
                blocks.append(
                    _PythonExceptionBlock(
                        class_name=exception_match.group("class"),
                        message=_normalize_message(
                            exception_match.group("message") or ""
                        ),
                        header=line,
                        frames=list(active_frames),
                    )
                )
                active_frames = None
                continue

        pytest_match = _PYTEST_EXCEPTION.match(line)
        if pytest_match is not None:
            pending_pytest = _PythonExceptionBlock(
                class_name=pytest_match.group("class"),
                message=_normalize_message(pytest_match.group("message") or ""),
                header=line,
            )
            blocks.append(pending_pytest)
            continue

        location_match = _PYTEST_LOCATION.match(line)
        if location_match is not None and pending_pytest is not None:
            pending_pytest.frames.append(
                _python_frame(
                    location_match.group("path"),
                    location_match.group("function") or "<module>",
                    execution_root,
                )
            )
            pending_pytest = None

    candidates = [block for block in blocks if block.frames]
    leaves = [
        block
        for block in candidates
        if block.class_name not in {"ExceptionGroup", "BaseExceptionGroup"}
    ]
    ordered = leaves + [block for block in candidates if block not in leaves]
    if pattern is not None:
        matching = []
        for block in ordered:
            if pattern.search(_python_block_text(block)) is None:
                continue
            signature = block.signature(execution_root is not None)
            if signature not in matching:
                matching.append(signature)
        if matching:
            return matching[0] if len(matching) == 1 else None
        signatures = []
        for block in ordered:
            signature = block.signature(execution_root is not None)
            if signature not in signatures:
                signatures.append(signature)
        return signatures[0] if len(signatures) == 1 else None
    return ordered[0].signature(execution_root is not None) if ordered else None


def _python_block_text(block: _PythonExceptionBlock) -> str:
    return "%s\n%s: %s\n%s" % (
        block.header,
        block.class_name,
        block.message,
        "\n".join(block.frames),
    )


def collect_surefire_diagnostics(root: Path) -> str:
    reports = sorted(
        path
        for path in root.rglob("TEST-*.xml")
        if path.is_file() and path.parent.name == "surefire-reports"
    )
    diagnostics: List[str] = []
    consumed = 0
    for report in reports[:_MAX_REPORT_FILES]:
        try:
            size = report.stat().st_size
        except OSError:
            continue
        if size > _MAX_REPORT_BYTES or consumed + size > _MAX_REPORT_BYTES:
            continue
        consumed += size
        diagnostics.extend(_read_surefire_failures(report))
    return "\n".join(diagnostics)


def _read_surefire_failures(path: Path) -> Iterable[str]:
    try:
        root = ET.parse(str(path)).getroot()
    except (ET.ParseError, OSError):
        return []
    failures: List[str] = []
    for element in root.iter():
        if _local_name(element.tag) not in {"error", "failure"}:
            continue
        body = (element.text or "").strip()
        if body:
            failures.append(body)
            continue
        class_name = element.attrib.get("type", "").strip()
        message = element.attrib.get("message", "").strip()
        if class_name:
            failures.append(class_name + (": " + message if message else ""))
    return failures


def _clean_line(line: str) -> str:
    without_ansi = _ANSI_ESCAPE.sub("", line)
    return _LOG_PREFIX.sub("", without_ansi).strip()


def _clean_python_line(line: str) -> str:
    cleaned = _clean_line(line)
    while cleaned.startswith(("|", "+")):
        cleaned = cleaned[1:].lstrip()
    return cleaned


def _normalize_message(message: str) -> str:
    return " ".join(message.split())


def _normalize_frame(method: str) -> str:
    return method.rsplit("/", 1)[-1]


def _python_frame(
    path: str, function: str, execution_root: Optional[str] = None,
) -> str:
    normalized = path.replace("\\", "/")
    if execution_root is not None:
        root = posixpath.normpath(execution_root.replace("\\", "/")).rstrip("/") + "/"
        candidate = posixpath.normpath(normalized)
        windows = re.match(r"^[A-Za-z]:/", root) is not None
        compared = candidate.casefold() if windows else candidate
        prefix = root.casefold() if windows else root
        if compared.startswith(prefix):
            normalized = candidate[len(root):]
            return "%s:%s" % (normalized, function.strip() or "<module>")
        # A host path containing /workspace is not a container-relative path.
        # Keep the legacy site-packages and external-frame fallback below.
        if "/site-packages/" in normalized:
            normalized = normalized.rsplit("/site-packages/", 1)[-1]
        elif normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
            normalized = normalized.rsplit("/", 1)[-1]
        return "%s:%s" % (normalized or "<unknown>", function.strip() or "<module>")
    for marker in ("/workspace/", "/site-packages/"):
        if marker in normalized:
            normalized = normalized.rsplit(marker, 1)[-1]
            break
    else:
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
            normalized = normalized.rsplit("/", 1)[-1]
    location = normalized or "<unknown>"
    return "%s:%s" % (location, function.strip() or "<module>")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
