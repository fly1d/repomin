"""Privacy-safe comparison of ReproMin reduction reports.

The comparison is deliberately descriptive.  It validates reports locally,
never executes their recorded commands, and exposes only a small allow-list of
aggregate fields that are useful when a reduction is repeated under a changed
configuration or ReproMin version.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from repomin.report import ReportValidationError, validate_report_file


COMPARISON_SCHEMA_VERSION = 1
MAX_REPORTS = 32
MAX_LABEL_LENGTH = 64
# Keep an adversarially large integer delta from expanding a shareable result.
# This is a bit-length limit so the guard does not itself stringify huge ints.
_MAX_INTEGER_DELTA_BITS = 4096

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){2}[A-Za-z0-9._+-]*$")
_MAX_VERSION_LENGTH = 128

_NUMERIC_FIELDS = (
    "source_files",
    "source_bytes",
    "output_files",
    "output_bytes",
    "file_retention_ratio",
    "byte_retention_ratio",
    "attempts",
    "accepted_mutations",
    "cache_hits",
    "holdout_planned_runs",
    "holdout_completed_runs",
    "holdout_passes",
)
_CATEGORICAL_FIELDS = (
    "repomin_version",
    "backend",
    "oracle_mode",
    "budget_exhausted",
    "holdout_status",
    "phase_coverage",
)

_INPUT_SELECTION_FIELDS = (
    "ignored_names",
    "ignored_paths",
    "gitignore_files",
    "gitignore_sha256",
    "gitignore_recursive",
    "keep_paths",
    "text_files",
)


class ReportComparisonError(ValueError):
    """Raised when reports cannot be compared safely."""


def _safe_version(value: object) -> Optional[str]:
    """Return version provenance only when it has a bounded safe shape."""
    if (
        not isinstance(value, str)
        or len(value) > _MAX_VERSION_LENGTH
        or _VERSION.fullmatch(value) is None
    ):
        return None
    return value


def _oracle_mode(report: Mapping[str, object]) -> str:
    """Classify an oracle without exposing its configured expression."""
    spec = report.get("failure_spec")
    if isinstance(spec, Mapping):
        if spec.get("java_exception"):
            return "java_exception"
        if spec.get("python_exception"):
            return "python_exception"
        if spec.get("process_failure"):
            return "process_failure"
        has_match = spec.get("match") is not None
        has_exit_code = spec.get("exit_code") is not None
        if has_match and has_exit_code:
            return "match_and_exit_code"
        if has_exit_code:
            return "exit_code"
        if has_match:
            return "match"
    if report.get("java_exception_signature") is not None:
        return "java_exception"
    if report.get("python_exception_signature") is not None:
        return "python_exception"
    if report.get("process_failure_signature") is not None:
        return "process_failure"
    if report.get("failure_match") is not None:
        return "match"
    return "legacy"


def _round_float(value: float) -> Optional[float]:
    if not math.isfinite(value):
        return None
    rounded = round(value, 6)
    return 0.0 if rounded == 0.0 else rounded


def _opaque_identity(value: object) -> Optional[str]:
    """Return a non-reversible identity for private context comparisons.

    Some execution settings (for example a semantic endpoint, container image,
    or environment names) are useful for deciding whether two reports were
    produced under the same conditions, but must never be copied into a
    shareable comparison.  Canonical JSON plus SHA-256 lets us compare those
    settings without retaining or rendering their values in the result.
    """
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _context_scalar(value: object) -> object:
    """Normalize a context value while keeping arbitrary text out of memory.

    Numeric and boolean settings remain directly comparable.  Strings and
    structured values are represented by an opaque digest; malformed values
    become a stable unavailable marker rather than being echoed in warnings.
    Missing and explicit ``null`` are intentionally equivalent for optional
    legacy fields.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return ("unavailable",)
        return ("float", value)
    identity = _opaque_identity(value)
    if identity is None:
        return ("unavailable",)
    return ("opaque", identity)


def _ratio(numerator: object, denominator: object) -> Optional[float]:
    """Calculate a bounded display ratio without overflowing a platform float."""
    if (
        isinstance(numerator, bool)
        or not isinstance(numerator, int)
        or isinstance(denominator, bool)
        or not isinstance(denominator, int)
        or denominator <= 0
    ):
        return None
    try:
        value = float(numerator) / float(denominator)
    except (OverflowError, TypeError, ValueError, ZeroDivisionError):
        return None
    return _round_float(value)


def _phase_coverage(report: Mapping[str, object]) -> str:
    """Return the validated phase coverage without interpreting timings."""
    phases = report["phase_statistics"]
    assert isinstance(phases, Mapping)
    coverage = phases["coverage"]
    assert isinstance(coverage, str)
    return coverage


def _phase_identity(report: Mapping[str, object]) -> object:
    """Hash phase names without retaining arbitrary report text."""
    phases = report["phase_statistics"]
    assert isinstance(phases, Mapping)
    items = phases["phases"]
    assert isinstance(items, list)
    names = [
        item.get("phase") if isinstance(item, Mapping) else None
        for item in items
    ]
    return _context_scalar(names)


def _oracle_identity(report: Mapping[str, object]) -> object:
    """Hash oracle configuration/evidence while keeping expressions private.

    ``failure_spec`` identifies the configured mode and match/exit contract,
    while exception/process modes also depend on their recorded signature.
    Keep only those stable fields in the hashed value so an unrelated future
    extension cannot create a spurious comparison warning.
    """
    spec = report.get("failure_spec")
    if isinstance(spec, Mapping):
        mode = _oracle_mode(report)
        identity: Dict[str, object] = {
            "mode": mode,
            "failure_spec": {
                name: spec.get(name)
                for name in (
                    "schema_version",
                    "match",
                    "exit_code",
                    "java_exception",
                    "python_exception",
                    "process_failure",
                )
            },
        }
        signature_name = {
            "java_exception": "java_exception_signature",
            "python_exception": "python_exception_signature",
            "process_failure": "process_failure_signature",
        }.get(mode)
        if signature_name is not None:
            identity["signature"] = report.get(signature_name)
    else:
        # Legacy reports have no failure_spec. Include all recorded oracle
        # evidence because it is the only stable identity available there.
        identity = {
            "mode": _oracle_mode(report),
            "failure_match": report.get("failure_match"),
            "java_exception_signature": report.get("java_exception_signature"),
            "python_exception_signature": report.get("python_exception_signature"),
            "process_failure_signature": report.get("process_failure_signature"),
        }
    return _context_scalar(identity)


def _validate_labels(count: int, labels: Optional[Sequence[str]]) -> List[str]:
    if labels is None:
        return ["run-%d" % index for index in range(1, count + 1)]
    if len(labels) != count:
        raise ReportComparisonError(
            "--label must be supplied exactly once for each report"
        )
    normalized: List[str] = []
    seen = set()
    for index, label in enumerate(labels, start=1):
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise ReportComparisonError(
                "label %d must be 1-%d ASCII characters matching [A-Za-z0-9._-]"
                % (index, MAX_LABEL_LENGTH)
            )
        if label in seen:
            raise ReportComparisonError("labels must be unique")
        seen.add(label)
        normalized.append(label)
    return normalized


def _row(
    report: Mapping[str, object],
    index: int,
    label: str,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    source = report["source"]
    output = report["output"]
    execution = report["execution"]
    holdout = report["holdout_certification"]
    assert isinstance(source, Mapping)
    assert isinstance(output, Mapping)
    assert isinstance(execution, Mapping)
    assert isinstance(holdout, Mapping)
    phase_coverage = _phase_coverage(report)
    phase_identity = _phase_identity(report)
    oracle_identity = _oracle_identity(report)
    source_files = source["files"]
    source_bytes = source["bytes"]
    output_files = output["files"]
    output_bytes = output["bytes"]
    repomin_version = _safe_version(report.get("repomin_version"))
    row: Dict[str, object] = {
        "index": index,
        "label": label,
        "repomin_version": repomin_version,
        "backend": execution["backend"],
        "oracle_mode": _oracle_mode(report),
        "source_files": source_files,
        "source_bytes": source_bytes,
        "output_files": output_files,
        "output_bytes": output_bytes,
        "file_retention_ratio": _ratio(output_files, source_files),
        "byte_retention_ratio": _ratio(output_bytes, source_bytes),
        "attempts": report["attempts"],
        "accepted_mutations": report["accepted_mutations"],
        "cache_hits": report["cache_hits"],
        "budget_exhausted": execution.get("budget_exhausted", False),
        "holdout_status": holdout["status"],
        "holdout_planned_runs": holdout["planned_runs"],
        "holdout_completed_runs": holdout["completed_runs"],
        "holdout_passes": holdout["passes"],
        "phase_coverage": phase_coverage,
    }
    context: Dict[str, object] = {
        "repomin_version": repomin_version,
        "version_available": repomin_version is not None,
        "backend": execution["backend"],
        "oracle_mode": row["oracle_mode"],
        "oracle_identity": oracle_identity,
        "source_files": source_files,
        "source_bytes": source_bytes,
        # These settings are part of the execution evidence but are kept out
        # of the public rows.  A digest is used for text/structured values so
        # endpoint URLs, image references, and environment names cannot leak.
        "jobs": execution["jobs"],
        "timeout_seconds": _context_scalar(execution.get("timeout_seconds")),
        "cache_enabled": _context_scalar(execution.get("cache_enabled")),
        "max_attempts": _context_scalar(execution.get("max_attempts")),
        "max_duration_seconds": _context_scalar(
            execution.get("max_duration_seconds")
        ),
        "semantic_reducer": _context_scalar(execution.get("semantic_reducer")),
        "semantic_model": _context_scalar(execution.get("semantic_model")),
        "semantic_endpoint": _context_scalar(execution.get("semantic_endpoint")),
        "semantic_timeout": _context_scalar(execution.get("semantic_timeout")),
        "working_directory_policy": _context_scalar(
            execution.get("working_directory_policy")
        ),
        "working_directory_basename": _context_scalar(
            execution.get("working_directory_basename")
        ),
        "resumed": _context_scalar(execution.get("resumed")),
        "container_context": tuple(
            (name, _context_scalar(execution.get(name)))
            for name in ("image", "image_id", "network", "limits")
        ),
        "input_selection": _context_scalar(
            {
                name: execution.get(name)
                for name in _INPUT_SELECTION_FIELDS
            }
        ),
        "environment_context": (
            _context_scalar(execution.get("environment_names")),
            _context_scalar(execution.get("environment_sha256")),
        ),
        "candidate_sampling_policy": _context_scalar(
            execution.get("candidate_sampling_policy")
        ),
        "candidate_runs": _context_scalar(execution.get("candidate_runs")),
        "candidate_min_passes": _context_scalar(
            execution.get("candidate_min_passes")
        ),
        "min_candidate_rate": _context_scalar(
            execution.get("min_candidate_rate")
        ),
        "confidence": _context_scalar(execution.get("confidence")),
        "run_confidence": _context_scalar(execution.get("run_confidence")),
        "candidate_family_control_policy": _context_scalar(
            execution.get("candidate_family_control_policy")
        ),
        "baseline_runs": _context_scalar(execution.get("baseline_runs")),
        "min_baseline_rate": _context_scalar(
            execution.get("min_baseline_rate")
        ),
        "reduction_strategy": _context_scalar(
            execution.get("reduction_strategy")
        ),
        "holdout_policy": _context_scalar(holdout.get("policy")),
        "holdout_status": holdout["status"],
        "holdout_cache_used": _context_scalar(holdout.get("cache_used")),
        "holdout_early_stopping": _context_scalar(holdout.get("early_stopping")),
        "holdout_fresh_copy": _context_scalar(
            holdout.get("fresh_repository_copy_per_run")
        ),
        "phase_coverage": phase_coverage,
        "phase_identity": phase_identity,
        "ratio_missing": row["file_retention_ratio"] is None
        or row["byte_retention_ratio"] is None,
        "budget_exhausted": row["budget_exhausted"],
    }
    return row, context


def _values_differ(contexts: Sequence[Mapping[str, object]], key: str) -> bool:
    if not contexts:
        return False
    first = contexts[0].get(key)
    return any(context.get(key) != first for context in contexts[1:])


def _context_warnings(contexts: Sequence[Mapping[str, object]]) -> List[str]:
    warnings: List[str] = []
    if _values_differ(contexts, "source_files") or _values_differ(
        contexts, "source_bytes"
    ):
        warnings.append(
            "source sizes differ; retention ratios are descriptive and not directly comparable"
        )
    if _values_differ(contexts, "input_selection"):
        warnings.append("input selection and exclusion controls differ")
    if any(not context.get("version_available", False) for context in contexts):
        warnings.append(
            "ReproMin version provenance is unavailable for at least one report"
        )
    elif _values_differ(contexts, "repomin_version"):
        warnings.append("ReproMin versions differ")
    if _values_differ(contexts, "backend"):
        warnings.append("execution backends differ")
    if _values_differ(contexts, "jobs"):
        warnings.append("execution jobs differ")
    if _values_differ(contexts, "timeout_seconds"):
        warnings.append(
            "execution timeouts differ or are unavailable (timeout_seconds)"
        )
    if _values_differ(contexts, "cache_enabled"):
        warnings.append("execution cache settings (cache_enabled) differ")
    if any(
        _values_differ(contexts, key)
        for key in ("max_attempts", "max_duration_seconds")
    ):
        warnings.append("execution budget limits differ or are unavailable")
    if any(
        _values_differ(contexts, key)
        for key in (
            "semantic_reducer",
            "semantic_model",
            "semantic_endpoint",
            "semantic_timeout",
        )
    ):
        warnings.append("semantic reducer configuration differs")
    if _values_differ(contexts, "container_context"):
        warnings.append("container execution configuration differs")
    if _values_differ(contexts, "environment_context"):
        warnings.append("execution environment metadata differs")
    if any(
        _values_differ(contexts, key)
        for key in ("working_directory_policy", "working_directory_basename")
    ):
        warnings.append("working-directory execution policies differ")
    if _values_differ(contexts, "resumed"):
        warnings.append("report resumption states differ")
    if any(context.get("budget_exhausted") is True for context in contexts):
        warnings.append("at least one report exhausted an execution budget")
    if _values_differ(contexts, "oracle_mode"):
        warnings.append("failure oracle modes differ")
    if _values_differ(contexts, "oracle_identity"):
        warnings.append("failure oracle configuration or identity differs")
    sampling_keys = (
        "candidate_sampling_policy",
        "candidate_runs",
        "candidate_min_passes",
        "min_candidate_rate",
        "confidence",
        "run_confidence",
        "candidate_family_control_policy",
    )
    if any(_values_differ(contexts, key) for key in sampling_keys):
        warnings.append("candidate sampling configuration differs")
    baseline_keys = ("baseline_runs", "min_baseline_rate")
    if any(_values_differ(contexts, key) for key in baseline_keys):
        warnings.append("baseline sampling configuration differs")
    if _values_differ(contexts, "reduction_strategy"):
        warnings.append("reduction strategies differ")
    if _values_differ(contexts, "holdout_policy"):
        warnings.append("holdout certification policies differ")
    if _values_differ(contexts, "holdout_status"):
        warnings.append("holdout certification statuses differ")
    holdout_execution_keys = (
        "holdout_cache_used",
        "holdout_early_stopping",
        "holdout_fresh_copy",
    )
    if any(_values_differ(contexts, key) for key in holdout_execution_keys):
        warnings.append("holdout execution controls differ")
    if _values_differ(contexts, "phase_coverage"):
        warnings.append("phase statistics coverage differs")
    if _values_differ(contexts, "phase_identity"):
        warnings.append("phase definitions differ")
    if any(context.get("phase_coverage") == "partial" for context in contexts):
        warnings.append("at least one report has partial phase statistics")
    if any(context.get("ratio_missing") for context in contexts):
        warnings.append("a retention ratio is unavailable for at least one report")
    return warnings


def _difference(left: object, right: object) -> Optional[object]:
    if (
        isinstance(left, bool)
        or isinstance(right, bool)
        or not isinstance(left, (int, float))
        or not isinstance(right, (int, float))
    ):
        return None
    if isinstance(left, int) and isinstance(right, int):
        # Integer counters stay exact even when they exceed binary64 range;
        # only mixed numeric values take the guarded floating-point path.
        value = right - left
        if value != 0 and value.bit_length() > _MAX_INTEGER_DELTA_BITS:
            return None
        return value
    try:
        value = float(right) - float(left)
    except (OverflowError, TypeError, ValueError):
        return None
    return _round_float(value)


def _delta(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> Dict[str, object]:
    numeric: Dict[str, object] = {
        field: _difference(left.get(field), right.get(field))
        for field in _NUMERIC_FIELDS
    }
    changed = [
        field for field in _CATEGORICAL_FIELDS if left.get(field) != right.get(field)
    ]
    return {
        "from_index": left["index"],
        "from_label": left["label"],
        "to_index": right["index"],
        "to_label": right["label"],
        "numeric_deltas": numeric,
        "changed_fields": changed,
    }


def _safe_error(index: int, path: Path, error: BaseException) -> str:
    """Keep validator diagnostics useful without echoing untrusted values.

    ``validate_report_document`` intentionally gives detailed diagnostics, but
    a few legacy branches include the offending value (for example an unknown
    schema version or holdout outcome).  A comparison error is commonly copied
    into an issue or CI log, so reduce those branches to their stable field
    prefix and strip every recognizable spelling of the input path.
    """
    if isinstance(error, OSError):
        message = "report could not be read"
    elif not isinstance(error, ReportValidationError):
        # ValueError is caught defensively around report validation, but its
        # text is not part of the validator's public diagnostic contract.  Do
        # not try to infer every possible path spelling in an unexpected
        # exception; a fixed fallback is the only reliable privacy boundary.
        message = "validation failed"
    else:
        message = str(error)
    normalized_message = " ".join(message.split())
    dynamic_prefixes = (
        "report could not be read",
        "unsupported report schema_version",
        "unknown holdout_certification.status",
        "unknown outcome",
        "has unknown outcome",
        "payload is not a directory",
        "payload size differs from report",
    )
    dynamic_message = False
    for prefix in dynamic_prefixes:
        if normalized_message.startswith(prefix):
            message = prefix
            dynamic_message = True
            break
    else:
        unknown_marker = " has unknown outcome:"
        if unknown_marker in normalized_message:
            message = (
                normalized_message.split(unknown_marker, 1)[0]
                + " has unknown outcome"
            )
            dynamic_message = True
    if not dynamic_message:
        path_candidates = {
            str(path),
            path.name,
        }
        try:
            path_candidates.add(str(path.absolute()))
        except (OSError, RuntimeError):
            pass
        try:
            path_candidates.add(str(path.resolve()))
        except (OSError, RuntimeError):
            pass
        # Validation errors can cross a shell, log transport, or platform
        # boundary before comparison.  Match both separator spellings so a
        # Windows Path still redacts a POSIX-rendered path and vice versa.
        for candidate in tuple(path_candidates):
            if "/" in candidate:
                path_candidates.add(candidate.replace("/", "\\"))
            if "\\" in candidate:
                path_candidates.add(candidate.replace("\\", "/"))
        for candidate in tuple(path_candidates):
            if not candidate.strip("/\\"):
                continue
            uri_path = candidate.replace("\\", "/")
            if uri_path.startswith("//"):
                path_candidates.add("file:" + uri_path)
            elif uri_path.startswith("/"):
                path_candidates.add("file://" + uri_path)
            elif re.match(r"^[A-Za-z]:/", uri_path):
                path_candidates.add("file:///" + uri_path)
        # Replace complete path spellings first, then redact a basename only
        # when it is visibly the final segment of another path.  A private
        # marker protects the first replacement from the fallback pass when a
        # basename is itself a word such as ``report``.
        full_paths = sorted(
            (
                candidate
                for candidate in path_candidates
                if candidate
                and candidate.strip("/\\")
                and ("/" in candidate or "\\" in candidate)
            ),
            key=lambda candidate: (-len(candidate), candidate),
        )
        marker = "\x00REPOMIN_PATH_%d\x00" % index
        while marker in message:
            marker += "_"
        if full_paths:
            path_prefix = r"(?:^|(?<=[\s,;(\[{'\"`=<]))"
            path_suffix = r"(?=$|[\s:;,\)\]\}'\"`>])"
            message = re.sub(
                path_prefix
                + "(?:"
                + "|".join(re.escape(candidate) for candidate in full_paths)
                + ")"
                + path_suffix,
                marker,
                message,
            )
        basenames = {
            re.split(r"[/\\]", candidate)[-1]
            for candidate in path_candidates
            if candidate
        }
        basenames.discard("")
        for basename in sorted(basenames, key=lambda value: (-len(value), value)):
            # Restrict the trailing boundary to punctuation used to delimit a
            # rendered path.  Whitespace would make ``/x/report details``
            # indistinguishable from a different filename beginning with the
            # ordinary word ``report``.
            basename_pattern = (
                r"(?<=[/\\])%s(?=$|[:;,\)\]\}'\"`]"
                r"|\s+(?:and|or)\s)" % re.escape(basename)
            )
            message = re.sub(basename_pattern, marker, message)
        message = message.replace(marker, "input report %d" % index)
        # Redact before folding whitespace so tabs or newlines embedded in a
        # path cannot be transformed into a spelling absent from the candidate
        # set and survive into the diagnostic.
        message = " ".join(message.split())
    if not message:
        return "validation failed"
    if len(message) > 240:
        message = message[:237] + "..."
    return "report %d: %s" % (index, message)


def compare_reports(
    report_paths: Sequence[Path],
    *,
    labels: Optional[Sequence[str]] = None,
) -> Dict[str, object]:
    """Validate and compare at least two reports in the supplied order.

    Validation is structural only.  No payload is read and no recorded command
    is executed.  The returned object is safe to serialize or share as it does
    not contain report paths or other high-cardinality report fields.
    """
    # Normalize path-like inputs once so both the public helper and CLI share
    # identical diagnostics and no string path can bypass redaction.
    path_error = False
    try:
        paths = [Path(path) for path in report_paths]
    except (TypeError, ValueError):
        paths = []
        path_error = True
    if path_error:
        # Raise after leaving the handler so the public exception has no raw,
        # potentially sensitive exception context attached to its traceback.
        raise ReportComparisonError("report paths must be path-like") from None
    if len(paths) < 2:
        raise ReportComparisonError("at least two report paths are required")
    if len(paths) > MAX_REPORTS:
        raise ReportComparisonError("at most %d reports may be compared" % MAX_REPORTS)
    normalized_labels = _validate_labels(len(paths), labels)
    rows: List[Dict[str, object]] = []
    contexts: List[Dict[str, object]] = []
    for index, path in enumerate(paths, start=1):
        safe_error = None
        try:
            report = validate_report_file(path)
        except (OSError, ReportValidationError, ValueError) as exc:
            safe_error = _safe_error(index, path, exc)
        if safe_error is not None:
            # Deliberately raise outside the handler.  Suppressing display with
            # ``from None`` alone would still retain the private exception in
            # ``__context__`` for callers of this public API.
            raise ReportComparisonError(safe_error) from None
        row, context = _row(report, index, normalized_labels[index - 1])
        rows.append(row)
        contexts.append(context)
    deltas = [_delta(rows[index - 1], rows[index]) for index in range(1, len(rows))]
    return {
        "comparison_schema_version": COMPARISON_SCHEMA_VERSION,
        "descriptive_only": True,
        "run_count": len(rows),
        "runs": rows,
        "deltas": deltas,
        "context_warnings": _context_warnings(contexts),
    }


def _display(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "%.6f" % value
    return str(value)


def render_comparison_text(comparison: Mapping[str, object]) -> str:
    """Render a compact human-readable comparison without sensitive fields."""
    runs = comparison["runs"]
    deltas = comparison["deltas"]
    warnings = comparison["context_warnings"]
    assert isinstance(runs, list)
    assert isinstance(deltas, list)
    assert isinstance(warnings, list)
    lines = [
        "ReproMin report comparison (descriptive only; not a performance, correctness, or causal claim)",
        "runs: %d" % comparison["run_count"],
    ]
    for row in runs:
        assert isinstance(row, Mapping)
        lines.extend(
            [
                "",
                "run %s [%s]: repomin=%s | backend=%s | oracle=%s"
                % (
                    row["index"],
                    row["label"],
                    _display(row["repomin_version"]),
                    row["backend"],
                    row["oracle_mode"],
                ),
                "  source=%s files/%s bytes -> output=%s files/%s bytes"
                % (
                    row["source_files"],
                    row["source_bytes"],
                    row["output_files"],
                    row["output_bytes"],
                ),
                "  retention=%s files/%s bytes | attempts=%s accepted=%s cache=%s budget=%s"
                % (
                    _display(row["file_retention_ratio"]),
                    _display(row["byte_retention_ratio"]),
                    row["attempts"],
                    row["accepted_mutations"],
                    row["cache_hits"],
                    _display(row["budget_exhausted"]),
                ),
                "  holdout=%s passes=%s/%s completed (%s planned) | phase=%s"
                % (
                    row["holdout_status"],
                    row["holdout_passes"],
                    row["holdout_completed_runs"],
                    row["holdout_planned_runs"],
                    row["phase_coverage"],
                ),
            ]
        )
    if deltas:
        lines.extend(["", "adjacent numeric deltas (next minus previous):"])
        for delta in deltas:
            assert isinstance(delta, Mapping)
            numeric = delta["numeric_deltas"]
            assert isinstance(numeric, Mapping)
            shown = ", ".join(
                "%s=%s" % (field, _display(numeric[field]))
                for field in _NUMERIC_FIELDS
            )
            changed = delta["changed_fields"]
            assert isinstance(changed, list)
            lines.append(
                "%s -> %s: %s | changed=%s"
                % (
                    delta["from_label"],
                    delta["to_label"],
                    shown,
                    ",".join(changed) if changed else "none",
                )
            )
    lines.extend(["", "context warnings:"])
    if warnings:
        lines.extend("- %s" % warning for warning in warnings)
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def _markdown_cell(value: object) -> str:
    """Escape an untrusted scalar for a Markdown table cell."""
    if value is None:
        text = "n/a"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    escaped: List[str] = []
    for character in text:
        codepoint = ord(character)
        if character == "\\":
            escaped.append("\\\\")
        elif character == "|":
            escaped.append("\\|")
        elif character in "\r\n":
            escaped.append("\\r" if character == "\r" else "\\n")
        elif codepoint < 0x20 or codepoint == 0x7F:
            escaped.append("\\u%04x" % codepoint)
        else:
            escaped.append(character)
    rendered = "".join(escaped)
    longest_run = 0
    current_run = 0
    for character in rendered:
        if character == "`":
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    fence = "`" * (longest_run + 1)
    return "%s%s%s" % (fence, rendered, fence)


def _markdown_pair(left: object, right: object, separator: str = " / ") -> str:
    return _markdown_cell("%s%s%s" % (_display(left), separator, _display(right)))


def render_comparison_markdown(comparison: Mapping[str, object]) -> str:
    """Render the fixed comparison allow-list as deterministic Markdown."""
    runs = comparison["runs"]
    deltas = comparison["deltas"]
    warnings = comparison["context_warnings"]
    assert isinstance(runs, list)
    assert isinstance(deltas, list)
    assert isinstance(warnings, list)
    lines = [
        "# ReproMin report comparison",
        "",
        "This comparison is descriptive only; it is not a performance, correctness, or causal claim.",
        "It validates report structure locally and does not execute commands, read payloads, or access the network.",
        "",
        "## Runs",
        "",
        "| # | Label | ReproMin | Backend | Oracle | Source (files / bytes) | Output (files / bytes) | Retention (files / bytes) | Attempts | Accepted | Cache hits | Budget | Holdout | Phase |",
        "| ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for row in runs:
        assert isinstance(row, Mapping)
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                _markdown_cell(row["index"]),
                _markdown_cell(row["label"]),
                _markdown_cell(row["repomin_version"]),
                _markdown_cell(row["backend"]),
                _markdown_cell(row["oracle_mode"]),
                _markdown_pair(row["source_files"], row["source_bytes"]),
                _markdown_pair(row["output_files"], row["output_bytes"]),
                _markdown_pair(
                    row["file_retention_ratio"], row["byte_retention_ratio"]
                ),
                _markdown_cell(row["attempts"]),
                _markdown_cell(row["accepted_mutations"]),
                _markdown_cell(row["cache_hits"]),
                _markdown_cell(row["budget_exhausted"]),
                _markdown_pair(
                    row["holdout_status"],
                    "%s/%s/%s"
                    % (
                        row["holdout_passes"],
                        row["holdout_completed_runs"],
                        row["holdout_planned_runs"],
                    ),
                    separator="; ",
                ),
                _markdown_cell(row["phase_coverage"]),
            )
        )
    if deltas:
        lines.extend(
            [
                "",
                "## Adjacent Deltas",
                "",
                "Numeric values are next minus previous; ratios are rounded to six decimal places.",
                "",
                "| From | To | Source files | Source bytes | Output files | Output bytes | File retention | Byte retention | Attempts | Accepted | Cache hits | Changed fields |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for delta in deltas:
            assert isinstance(delta, Mapping)
            numeric = delta["numeric_deltas"]
            assert isinstance(numeric, Mapping)
            changed = delta["changed_fields"]
            assert isinstance(changed, list)
            lines.append(
                "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                % (
                    _markdown_cell(delta["from_label"]),
                    _markdown_cell(delta["to_label"]),
                    _markdown_cell(numeric["source_files"]),
                    _markdown_cell(numeric["source_bytes"]),
                    _markdown_cell(numeric["output_files"]),
                    _markdown_cell(numeric["output_bytes"]),
                    _markdown_cell(numeric["file_retention_ratio"]),
                    _markdown_cell(numeric["byte_retention_ratio"]),
                    _markdown_cell(numeric["attempts"]),
                    _markdown_cell(numeric["accepted_mutations"]),
                    _markdown_cell(numeric["cache_hits"]),
                    _markdown_cell(", ".join(changed) if changed else "none"),
                )
            )
    lines.extend(["", "## Context Warnings", ""])
    if warnings:
        lines.extend("- %s" % _markdown_cell(warning) for warning in warnings)
    else:
        lines.append("- None observed.")
    return "\n".join(lines) + "\n"


__all__ = [
    "COMPARISON_SCHEMA_VERSION",
    "MAX_REPORTS",
    "ReportComparisonError",
    "compare_reports",
    "render_comparison_markdown",
    "render_comparison_text",
]
