from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import List, Optional, Protocol, Sequence

from repomin.model import DEFAULT_SEMANTIC_TIMEOUT_SECONDS
from repomin.session import MutationCandidate, ReductionSession


DEFAULT_MAX_FILES = 64
DEFAULT_MAX_FILE_BYTES = 20000
DEFAULT_MAX_TOTAL_BYTES = 120000
DEFAULT_TIMEOUT_SECONDS = DEFAULT_SEMANTIC_TIMEOUT_SECONDS
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class SemanticError(ValueError):
    """A semantic reducer backend could not produce a valid candidate."""


class SemanticBackend(Protocol):
    """Provider-agnostic source of semantic reduction candidates.

    A backend inspects the current reduction tree and returns one or more
    :class:`~repomin.session.MutationCandidate` objects. Every returned
    candidate is still verified by the configured failure oracle; rejected
    candidates are discarded exactly like any other reducer's candidates.
    """

    name: str

    def propose(self, session: ReductionSession) -> Sequence[MutationCandidate]:
        ...


class NoopSemanticBackend:
    """Disabled semantic reducer.

    This is the default and keeps the reduction byte-for-byte equivalent to a
    run without the semantic reducer seam.
    """

    name = "none"

    def propose(self, session: ReductionSession) -> Sequence[MutationCandidate]:
        return ()


class HttpSemanticBackend:
    """OpenAI-compatible chat-completions backend with no third-party SDK.

    The endpoint must speak the common ``/v1/chat/completions`` JSON contract.
    Authentication is read only from the ``REPOMIN_SEMANTIC_TOKEN`` environment
    variable so that a secret never appears in ``argv`` or reports.
    """

    name = "http"

    def __init__(
        self,
        endpoint: str,
        model: str,
        token: Optional[str] = None,
        timeout: float = DEFAULT_SEMANTIC_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not endpoint:
            raise SemanticError("semantic endpoint is empty")
        if not model:
            raise SemanticError("semantic model is empty")
        self.endpoint = endpoint
        self.model = model
        self.token = token
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def propose(self, session: ReductionSession) -> Sequence[MutationCandidate]:
        prompt = build_prompt(session)
        content = self._complete(prompt)
        edits = parse_edits(content)
        return edits_to_candidates(session, edits)

    def _complete(self, prompt: str) -> str:
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You reduce failing repositories while preserving the "
                        "exact configured failure. Respond with JSON only."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
            ) as response:
                raw = response.read(self.max_response_bytes + 1)
        except (urllib.error.URLError, OSError) as exc:
            raise SemanticError("semantic backend request failed: %s" % exc) from exc
        if len(raw) > self.max_response_bytes:
            raise SemanticError("semantic backend response is too large")
        try:
            payload_obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SemanticError("semantic backend returned invalid JSON") from exc
        if not isinstance(payload_obj, dict):
            raise SemanticError("semantic backend response must be a JSON object")
        choices = payload_obj.get("choices")
        if not isinstance(choices, list) or not choices:
            raise SemanticError("semantic backend response has no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise SemanticError("semantic backend choice is malformed")
        message = first.get("message")
        if not isinstance(message, dict):
            raise SemanticError("semantic backend choice has no message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise SemanticError("semantic backend choice has no content")
        return content


class SemanticReducer:
    """Schedules semantic candidates through the existing oracle pipeline."""

    def __init__(
        self,
        session: ReductionSession,
        backend: SemanticBackend,
    ) -> None:
        self.session = session
        self.backend = backend

    def reduce(self) -> None:
        if self.backend.name == "none":
            return
        with self.session.measure_phase("semantic"):
            self._reduce()

    def _reduce(self) -> None:
        candidates = self.backend.propose(self.session)
        self.session.stats.semantic_calls += 1
        if not candidates:
            return
        accepted = self.session.try_mutations("semantic", list(candidates))
        if accepted is not None:
            self.session.stats.semantic_accepted += 1


def build_prompt(session: ReductionSession) -> str:
    failure = describe_failure(session)
    files = list(collect_text_files(session))
    rendered = "".join(
        "\n```file path: %s\n%s\n```\n" % (relative, content)
        for relative, content in files
    )
    if not rendered:
        rendered = "(no readable text files)"
    return (
        "Reduce the repository below to a minimal reproduction while "
        "preserving the configured failure.\n\n"
        "Failure configuration:\n%s\n\n"
        "Current files:\n%s\n\n"
        "Propose one small edit and respond with JSON only, in exactly one "
        "of these shapes:\n"
        '{"edits":[{"path":"relative/path","replace":"new file content"}]}\n'
        '{"edits":[{"path":"relative/path","delete":true}]}\n'
        "Do not include markdown, explanations, or text outside the JSON."
    ) % (failure, rendered)


def describe_failure(session: ReductionSession) -> str:
    identity = getattr(session, "identity", {}) or {}
    command = identity.get("command")
    lines: List[str] = []
    if command:
        lines.append("command: %s" % command)
    spec = session.oracle.spec
    if spec.match is not None:
        lines.append("output must match: %s" % spec.match)
    if spec.exit_code is not None:
        lines.append("required exit code: %d" % spec.exit_code)
    elif not (spec.java_exception or spec.python_exception or spec.process_failure):
        lines.append("required exit code: any non-zero")
    if spec.java_exception:
        lines.append("preserve normalized Java exception")
    if spec.python_exception:
        lines.append("preserve normalized Python exception")
    if spec.process_failure:
        lines.append("preserve process failure signature")
    signature = session.oracle.java_exception_signature
    if signature is not None:
        lines.append(
            "Java exception: %s: %s" % (signature.class_name, signature.message)
        )
    signature = session.oracle.python_exception_signature
    if signature is not None:
        lines.append(
            "Python exception: %s: %s" % (signature.class_name, signature.message)
        )
    signature = session.oracle.process_failure_signature
    if signature is not None:
        lines.append(
            "process failure: %s (code %d)" % (signature.kind, signature.code)
        )
    if not lines:
        return "unknown failure configuration"
    return "\n".join(lines)


def collect_text_files(
    session: ReductionSession,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> Sequence[tuple[str, str]]:
    root = session.current
    collected: List[tuple[str, str]] = []
    total = 0
    for path in sorted(
        root.rglob("*"),
        key=lambda candidate: candidate.relative_to(root).as_posix(),
    ):
        if len(collected) >= max_files or total >= max_total_bytes:
            break
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if session.ignores.matches(relative, is_directory=False):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if len(data) > max_file_bytes:
            text = text[:max_file_bytes] + "\n... (truncated)"
        remaining = max_total_bytes - total
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining]
        collected.append((relative.as_posix(), text))
        total += len(text)
    return collected


def parse_edits(content: str) -> Sequence[dict]:
    try:
        payload = json.loads(_strip_code_fences(content))
    except ValueError as exc:
        raise SemanticError("semantic backend content is not valid JSON") from exc
    if isinstance(payload, list):
        edits = payload
    elif isinstance(payload, dict) and isinstance(payload.get("edits"), list):
        edits = payload["edits"]
    else:
        raise SemanticError(
            "semantic backend JSON must be a list or have an edits array"
        )
    if not all(isinstance(edit, dict) for edit in edits):
        raise SemanticError("semantic backend edits must be objects")
    return edits


def _strip_code_fences(content: str) -> str:
    """Accept the markdown code fences that local models commonly add."""
    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def edits_to_candidates(
    session: ReductionSession,
    edits: Sequence[dict],
) -> Sequence[MutationCandidate]:
    candidates: List[MutationCandidate] = []
    for index, edit in enumerate(edits):
        path_text = edit.get("path")
        if not isinstance(path_text, str) or not path_text:
            raise SemanticError("semantic edit %d has no path" % index)
        relative = _safe_relative(path_text)
        has_delete = edit.get("delete") is True
        replace = edit.get("replace")
        has_replace = isinstance(replace, str)
        if has_delete == has_replace:
            raise SemanticError(
                "semantic edit %d must set exactly one of replace or delete" % index
            )
        if has_delete:
            candidates.append(_delete_candidate(session, relative))
        else:
            candidates.append(_replace_candidate(session, relative, replace))
    return candidates


def _safe_relative(path_text: str) -> Path:
    relative = PurePosixPath(path_text)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SemanticError("semantic edit path must be a safe relative path")
    return Path(*relative.parts)


def _delete_candidate(session: ReductionSession, relative: Path) -> MutationCandidate:
    kept = session.keeps(relative)

    def mutation(root: Path) -> bool:
        if kept:
            return False
        target = root / relative
        if target.is_file() and not target.is_symlink():
            target.unlink()
            return True
        return False

    return MutationCandidate(
        "semantic delete %s" % relative.as_posix(),
        mutation,
    )


def _replace_candidate(
    session: ReductionSession,
    relative: Path,
    content: str,
) -> MutationCandidate:
    def mutation(root: Path) -> bool:
        target = root / relative
        if not target.is_file() or target.is_symlink():
            return False
        target.write_text(content, encoding="utf-8")
        return True

    return MutationCandidate(
        "semantic replace %s" % relative.as_posix(),
        mutation,
    )
