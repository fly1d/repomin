from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from repomin import __version__
from repomin.model import (
    FailureSpec,
    ProcessFailureSignature,
    ReductionResult,
    TREE_CONTENT_FINGERPRINT_POLICY,
    TREE_FINGERPRINT_POLICY,
)
from repomin.session import _tree_content_digest, _tree_digest
from repomin.signature import (
    process_failure_name,
    valid_recorded_process_failure_signature,
)


REPORT_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ReportValidationError(ValueError):
    """The report is not a structurally valid ReproMin schema document."""


def validate_report_document(report: object) -> Dict[str, object]:
    """Validate the stable, machine-readable invariants of one report."""
    if not isinstance(report, dict):
        raise ReportValidationError("report root must be a JSON object")
    if (
        isinstance(report.get("schema_version"), bool)
        or not isinstance(report.get("schema_version"), int)
        or report.get("schema_version") != REPORT_SCHEMA_VERSION
    ):
        raise ReportValidationError(
            "unsupported report schema_version: %r" % report.get("schema_version")
        )
    if "repomin_version" in report:
        _require_text(report, "repomin_version", non_empty=True)
    command = _require_text(report, "command", non_empty=True)
    if "\x00" in command:
        raise ReportValidationError("command must not contain NUL")
    _require_optional_text(report, "failure_match")
    _require_int(report, "baseline_exit_code")
    _require_int(report, "final_exit_code")
    for name in ("source", "output"):
        section = _require_object(report, name)
        _require_nonnegative_int(section, "files", name)
        _require_nonnegative_int(section, "bytes", name)
    output = report["output"]
    assert isinstance(output, dict)
    tree_sha256 = output.get("tree_sha256")
    if tree_sha256 is not None and (
        not isinstance(tree_sha256, str) or _SHA256.fullmatch(tree_sha256) is None
    ):
        raise ReportValidationError("output.tree_sha256 must be SHA-256")
    tree_policy = output.get("tree_fingerprint_policy")
    if tree_sha256 is None and tree_policy is not None:
        raise ReportValidationError(
            "output.tree_fingerprint_policy requires tree_sha256"
        )
    if tree_sha256 is not None and tree_policy != TREE_FINGERPRINT_POLICY:
        raise ReportValidationError(
            "output.tree_fingerprint_policy is unsupported or missing"
        )
    content_sha256 = output.get("tree_content_sha256")
    if content_sha256 is not None and (
        not isinstance(content_sha256, str)
        or _SHA256.fullmatch(content_sha256) is None
    ):
        raise ReportValidationError("output.tree_content_sha256 must be SHA-256")
    content_policy = output.get("tree_content_fingerprint_policy")
    if content_sha256 is None and content_policy is not None:
        raise ReportValidationError(
            "output.tree_content_fingerprint_policy requires tree_content_sha256"
        )
    if content_sha256 is not None and content_policy != TREE_CONTENT_FINGERPRINT_POLICY:
        raise ReportValidationError(
            "output.tree_content_fingerprint_policy is unsupported or missing"
        )
    for name in ("attempts", "accepted_mutations", "cache_hits"):
        _require_nonnegative_int(report, name)

    execution = _require_object(report, "execution")
    backend = _require_text(execution, "backend", non_empty=True)
    if backend not in {"host", "docker"}:
        raise ReportValidationError("execution.backend must be host or docker")
    _require_positive_int(execution, "jobs", "execution")
    if "timeout_seconds" in execution:
        timeout = _require_nonnegative_number(
            execution, "timeout_seconds", "execution"
        )
        if timeout <= 0.0:
            raise ReportValidationError("execution.timeout_seconds must be positive")
    if "budget_exhausted" in execution and not isinstance(
        execution["budget_exhausted"], bool
    ):
        raise ReportValidationError("execution.budget_exhausted must be boolean")
    _validate_execution_limits(execution)
    _validate_execution_environment(execution)

    _validate_failure_spec(report)
    _validate_failure_signatures(report)

    phases = _require_object(report, "phase_statistics")
    _validate_optional_schema_version(phases, "phase_statistics")
    coverage = phases.get("coverage")
    if not isinstance(coverage, str) or coverage not in {"complete", "partial"}:
        raise ReportValidationError(
            "phase_statistics.coverage must be complete or partial"
        )
    phase_items = phases.get("phases")
    if not isinstance(phase_items, list):
        raise ReportValidationError("phase_statistics.phases must be an array")
    if coverage == "complete":
        phase_attempts = 0
        phase_accepted = 0
        for index, phase in enumerate(phase_items):
            if not isinstance(phase, dict):
                raise ReportValidationError("phase %d must be an object" % index)
            attempts = _require_nonnegative_int(phase, "attempts", "phase %d" % index)
            accepted = _require_nonnegative_int(phase, "accepted", "phase %d" % index)
            no_op = _require_nonnegative_int(phase, "no_op", "phase %d" % index)
            rejected = _require_nonnegative_int(phase, "rejected", "phase %d" % index)
            superseded = _require_nonnegative_int(
                phase, "superseded", "phase %d" % index
            )
            aborted = _require_nonnegative_int(phase, "aborted", "phase %d" % index)
            if attempts != no_op + rejected + accepted + superseded + aborted:
                raise ReportValidationError(
                    "phase %d attempts accounting is inconsistent" % index
                )
            sample_uses = _require_nonnegative_int(
                phase, "oracle_sample_uses", "phase %d" % index
            )
            samples = _require_nonnegative_int(
                phase, "oracle_samples", "phase %d" % index
            )
            cache_hits = _require_nonnegative_int(
                phase, "cache_hits", "phase %d" % index
            )
            if sample_uses != samples + cache_hits:
                raise ReportValidationError(
                    "phase %d oracle accounting is inconsistent" % index
                )
            phase_attempts += attempts
            phase_accepted += accepted
        if phase_attempts != report["attempts"]:
            raise ReportValidationError(
                "phase attempts do not equal report attempts"
            )
        if phase_accepted != report["accepted_mutations"]:
            raise ReportValidationError(
                "phase accepted count does not equal report accepted_mutations"
            )

    events = report.get("events")
    if not isinstance(events, list):
        raise ReportValidationError("events must be an array")
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ReportValidationError("event %d must be an object" % index)
        context = "event %d" % index
        _require_text(event, "phase", non_empty=True)
        _require_text(event, "description", non_empty=True)
        _require_nonnegative_number(event, "duration_seconds", context)
        oracle_runs = _require_nonnegative_int(
            event, "oracle_runs", context
        )
        oracle_passes = _require_nonnegative_int(
            event, "oracle_passes", context
        )
        if oracle_passes > oracle_runs:
            raise ReportValidationError("%s oracle passes exceed runs" % context)
        oracle_rate = _require_optional_probability(event, "oracle_rate", context)
        if oracle_rate is not None:
            if not _ratio_matches(
                oracle_rate,
                oracle_passes,
                oracle_runs,
            ):
                raise ReportValidationError(
                    "%s oracle_rate does not match pass/run counts" % context
                )
        for name in ("oracle_lower_bound", "oracle_anytime_lower_bound"):
            _require_optional_probability(event, name, context)
        early_acceptance = event.get("oracle_early_acceptance")
        if "oracle_early_acceptance" in event and not isinstance(
            early_acceptance, bool
        ):
            raise ReportValidationError(
                "%s oracle_early_acceptance must be boolean" % context
            )
        family_index = _require_optional_nonnegative_int(
            event, "candidate_family_index", context
        )
        family_confidence = _require_optional_probability(
            event, "candidate_confidence", context
        )
        family_alpha = _require_optional_probability(event, "candidate_alpha", context)
        if family_index is None:
            if family_confidence is not None or family_alpha is not None:
                raise ReportValidationError(
                    "%s candidate family evidence is incomplete" % context
                )
        elif family_confidence is None or family_alpha is None:
            raise ReportValidationError(
                "%s candidate family evidence is incomplete" % context
            )

    holdout = _require_object(report, "holdout_certification")
    _validate_optional_schema_version(holdout, "holdout_certification")
    holdout_schema_version = holdout.get("schema_version")
    status = _require_text(holdout, "status", non_empty=True)
    allowed_statuses = {
        "not_requested",
        "not_started",
        "certified",
        "not_certified",
        "rejected",
        "interrupted",
        "aborted",
    }
    if status not in allowed_statuses:
        raise ReportValidationError("unknown holdout_certification.status: %s" % status)
    planned = _require_nonnegative_int(
        holdout, "planned_runs", "holdout_certification"
    )
    completed = _require_nonnegative_int(
        holdout, "completed_runs", "holdout_certification"
    )
    passes = _require_nonnegative_int(holdout, "passes", "holdout_certification")
    if completed > planned or passes > completed:
        raise ReportValidationError("holdout run counts are inconsistent")
    terminal_fields = (
        "minimum_rate",
        "confidence",
        "required_passes",
        "observed_rate",
        "exact_lower_bound",
        "exact_p_value",
        "exact_rate_gate_passed",
    )
    terminal_present = [
        name for name in terminal_fields
        if name in holdout and holdout[name] is not None
    ]
    if terminal_present and len(terminal_present) != len(terminal_fields):
        raise ReportValidationError(
            "holdout terminal statistics must be complete when present"
        )
    if status == "certified" and holdout_schema_version == 1:
        missing_terminal = [
            name
            for name in terminal_fields
            if name not in holdout or holdout[name] is None
        ]
        if missing_terminal:
            raise ReportValidationError(
                "certified holdout terminal statistics are incomplete: %s"
                % ", ".join(missing_terminal)
            )
        if planned < 1 or completed != planned:
            raise ReportValidationError(
                "certified holdout must complete all planned runs"
            )
    minimum_rate = _require_optional_probability(
        holdout, "minimum_rate", "holdout_certification"
    )
    confidence = _require_optional_probability(
        holdout, "confidence", "holdout_certification"
    )
    if minimum_rate is not None and minimum_rate <= 0.0:
        raise ReportValidationError("holdout_certification.minimum_rate must be positive")
    if confidence is not None and not 0.0 < confidence < 1.0:
        raise ReportValidationError(
            "holdout_certification.confidence must be in (0, 1)"
        )
    required_passes = _require_optional_nonnegative_int(
        holdout, "required_passes", "holdout_certification"
    )
    if required_passes is not None and required_passes > planned:
        raise ReportValidationError(
            "holdout_certification.required_passes exceeds planned_runs"
        )
    observed_rate = _require_optional_probability(
        holdout, "observed_rate", "holdout_certification"
    )
    if observed_rate is not None:
        if not _ratio_matches(observed_rate, passes, planned):
            raise ReportValidationError(
                "holdout observed_rate does not match pass/run counts"
            )
    for name in ("exact_lower_bound", "exact_p_value"):
        _require_optional_probability(holdout, name, "holdout_certification")
    exact_gate = holdout.get("exact_rate_gate_passed")
    if "exact_rate_gate_passed" in holdout and exact_gate is not None:
        if not isinstance(exact_gate, bool):
            raise ReportValidationError(
                "holdout_certification.exact_rate_gate_passed must be boolean"
            )
    samples = holdout.get("samples")
    if not isinstance(samples, list) or len(samples) != completed:
        raise ReportValidationError(
            "holdout_certification.samples must match completed_runs"
        )
    sample_passes = 0
    allowed_outcomes = {
        "interrupted",
        "timed_out",
        "resource_exhausted",
        "passed",
        "failed",
    }
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ReportValidationError(
                "holdout sample %d must be an object" % index
            )
        context = "holdout sample %d" % index
        sample_index = _require_nonnegative_int(
            sample, "index", context
        )
        if sample_index != index + 1:
            raise ReportValidationError(
                "holdout sample indexes must be contiguous from 1"
            )
        accepted = sample.get("accepted")
        if not isinstance(accepted, bool):
            raise ReportValidationError(
                "holdout sample %d accepted must be boolean" % index
            )
        if accepted:
            sample_passes += 1
        outcome_present = "outcome" in sample
        outcome = sample.get("outcome")
        if outcome_present:
            _require_text(sample, "outcome", non_empty=True)
            assert isinstance(outcome, str)
            if outcome not in allowed_outcomes:
                raise ReportValidationError(
                    "%s has unknown outcome: %s" % (context, outcome)
                )
        returncode = sample.get("returncode")
        if returncode is not None and (
            isinstance(returncode, bool) or not isinstance(returncode, int)
        ):
            raise ReportValidationError(
                "%s returncode must be an integer or null" % context
            )
        duration = sample.get("duration_seconds")
        if duration is not None:
            _require_nonnegative_number(sample, "duration_seconds", context)
        timed_out = sample.get("timed_out", False)
        resource_exhausted = sample.get("resource_exhausted", False)
        for name, value in (
            ("timed_out", timed_out),
            ("resource_exhausted", resource_exhausted),
        ):
            if not isinstance(value, bool):
                raise ReportValidationError(
                    "%s %s must be boolean" % (context, name)
                )
        if timed_out and resource_exhausted:
            raise ReportValidationError(
                "%s cannot be timed out and resource exhausted" % context
            )
        resource_reason = sample.get("resource_reason")
        if resource_reason is not None and not isinstance(resource_reason, str):
            raise ReportValidationError(
                "%s resource_reason must be text or null" % context
            )
        output_sha256 = sample.get("output_sha256")
        if output_sha256 is not None and (
            not isinstance(output_sha256, str)
            or _SHA256.fullmatch(output_sha256) is None
        ):
            raise ReportValidationError("%s output_sha256 must be SHA-256" % context)
        if (timed_out or resource_exhausted) and accepted:
            raise ReportValidationError(
                "%s resource veto sample cannot be accepted" % context
            )
        if outcome_present:
            assert isinstance(outcome, str)
            if outcome == "interrupted":
                if (
                    accepted
                    or returncode is not None
                    or duration is not None
                    or timed_out
                    or resource_exhausted
                    or resource_reason is not None
                    or output_sha256 is not None
                ):
                    raise ReportValidationError(
                        "%s interrupted evidence is inconsistent" % context
                    )
            else:
                expected_outcome = (
                    "timed_out"
                    if timed_out
                    else "resource_exhausted"
                    if resource_exhausted
                    else "passed"
                    if accepted
                    else "failed"
                )
                if outcome != expected_outcome:
                    raise ReportValidationError(
                        "%s outcome does not match evidence" % context
                    )
    if "ordinary_failures" in holdout:
        ordinary_failures = _require_nonnegative_int(
            holdout, "ordinary_failures", "holdout_certification"
        )
        if ordinary_failures > completed:
            raise ReportValidationError(
                "holdout_certification.ordinary_failures exceeds completed_runs"
            )
        if not all("outcome" in sample for sample in samples):
            raise ReportValidationError(
                "holdout ordinary_failures requires outcomes for every sample"
            )
        observed = sum(sample.get("outcome") == "failed" for sample in samples)
        if ordinary_failures != observed:
            raise ReportValidationError(
                "holdout ordinary failures count does not match samples"
            )
    aggregate_fields = (
        ("timed_out_runs", "timed_out", "timed out"),
        ("resource_exhausted_runs", "resource_exhausted", "resource exhausted"),
    )
    for field, sample_field, label in aggregate_fields:
        if field not in holdout:
            continue
        count = _require_nonnegative_int(holdout, field, "holdout_certification")
        if count > completed:
            raise ReportValidationError(
                "holdout_certification.%s exceeds completed_runs" % field
            )
        if all(sample_field in sample for sample in samples):
            observed = sum(sample[sample_field] is True for sample in samples)
            if count != observed:
                raise ReportValidationError(
                    "holdout %s count does not match samples" % label
                )
    if "interrupted_runs" in holdout:
        interrupted = _require_nonnegative_int(
            holdout, "interrupted_runs", "holdout_certification"
        )
        if interrupted > completed:
            raise ReportValidationError(
                "holdout_certification.interrupted_runs exceeds completed_runs"
            )
        if all("outcome" in sample for sample in samples):
            observed = sum(
                sample.get("outcome") == "interrupted" for sample in samples
            )
            if interrupted != observed:
                raise ReportValidationError(
                    "holdout interrupted count does not match samples"
                )
    if sample_passes != passes:
        raise ReportValidationError("holdout passes do not match samples")
    fingerprint = holdout.get("artifact_fingerprint")
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None
    ):
        raise ReportValidationError("holdout artifact_fingerprint must be SHA-256")
    if status == "certified" and fingerprint is None:
        raise ReportValidationError(
            "certified holdout must include artifact_fingerprint"
        )
    artifact_policy = holdout.get("artifact_fingerprint_policy")
    if artifact_policy is not None and artifact_policy != TREE_FINGERPRINT_POLICY:
        raise ReportValidationError(
            "holdout artifact_fingerprint_policy is unsupported"
        )
    if fingerprint is not None and tree_sha256 is not None and fingerprint != tree_sha256:
        raise ReportValidationError(
            "output tree fingerprint differs from holdout artifact fingerprint"
        )
    return report


def validate_report_file(
    report_path: Path,
    payload: Optional[Path] = None,
) -> Dict[str, object]:
    """Validate a report file and, when supplied, its exported payload tree."""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReportValidationError(
            "report could not be read: %s" % report_path
        ) from exc
    validate_report_document(report)
    assert isinstance(report, dict)
    holdout = report["holdout_certification"]
    assert isinstance(holdout, dict)
    output = report["output"]
    assert isinstance(output, dict)
    if payload is not None:
        if payload.is_symlink():
            raise ReportValidationError("payload root must not be a symbolic link")
        if not payload.is_dir():
            raise ReportValidationError("payload is not a directory: %s" % payload)
        _payload_fingerprint_evidence(report, payload)
        files, size = measure_tree(payload)
        if files != output["files"] or size != output["bytes"]:
            raise ReportValidationError(
                "payload size differs from report: %s" % payload
            )
    return report


def _payload_fingerprint_evidence(
    report: Dict[str, object],
    payload: Path,
) -> Tuple[str, str, Optional[str]]:
    """Return ``(mode, full_digest, content_digest)`` for one payload.

    ``exact`` means the complete local tree fingerprint matched. ``content``
    is a deliberate fallback for archive transports that rewrite filesystem
    metadata; callers should surface that metadata may have drifted.
    ``unavailable`` is retained for legacy reports without either digest.
    """
    output = report["output"]
    holdout = report["holdout_certification"]
    assert isinstance(output, dict)
    assert isinstance(holdout, dict)
    expected_full = output.get("tree_sha256") or holdout.get("artifact_fingerprint")
    expected_content = output.get("tree_content_sha256")
    actual_full = _tree_digest(payload, set())
    if expected_full is not None and actual_full == expected_full:
        return "exact", actual_full, None

    if expected_content is not None:
        actual_content = _tree_content_digest(payload, set())
        if actual_content == expected_content:
            return "content", actual_full, actual_content
        raise ReportValidationError(
            "payload fingerprint differs from report: %s" % payload
        )
    if expected_full is not None:
        raise ReportValidationError(
            "payload fingerprint differs from report: %s" % payload
        )
    return "unavailable", actual_full, None


def _require_object(parent: Dict[str, object], name: str) -> Dict[str, object]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise ReportValidationError("%s must be an object" % name)
    return value


def _validate_optional_schema_version(
    section: Dict[str, object], name: str
) -> None:
    """Validate additive section versions while keeping legacy omissions valid."""
    if "schema_version" not in section:
        return
    value = section["schema_version"]
    if isinstance(value, bool) or not isinstance(value, int) or value != 1:
        raise ReportValidationError("%s.schema_version must be 1" % name)


def _validate_failure_spec(report: Dict[str, object]) -> None:
    if "failure_spec" not in report:
        return
    value = report["failure_spec"]
    if not isinstance(value, dict):
        raise ReportValidationError("failure_spec must be an object")
    if (
        isinstance(value.get("schema_version"), bool)
        or not isinstance(value.get("schema_version"), int)
        or value.get("schema_version") != 1
    ):
        raise ReportValidationError("failure_spec.schema_version must be 1")
    for name in ("match", "exit_code"):
        if name not in value:
            raise ReportValidationError("failure_spec.%s is required" % name)
    match = value["match"]
    if match is not None and not isinstance(match, str):
        raise ReportValidationError("failure_spec.match must be text or null")
    if match != report.get("failure_match"):
        raise ReportValidationError(
            "failure_spec.match does not equal failure_match"
        )
    exit_code = value["exit_code"]
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise ReportValidationError(
            "failure_spec.exit_code must be an integer or null"
        )
    modes = []
    for name in ("java_exception", "python_exception", "process_failure"):
        enabled = value.get(name)
        if not isinstance(enabled, bool):
            raise ReportValidationError("failure_spec.%s must be boolean" % name)
        modes.append(enabled)
    if sum(modes) > 1:
        raise ReportValidationError(
            "failure_spec enables more than one failure signature mode"
        )
    if value.get("process_failure") and exit_code is not None:
        raise ReportValidationError(
            "failure_spec process_failure cannot include exit_code"
        )
    if match is None and exit_code is None and not value.get("process_failure"):
        raise ReportValidationError("failure_spec does not define a failure oracle")


def _validate_failure_signatures(report: Dict[str, object]) -> None:
    signature_names = (
        "java_exception_signature",
        "python_exception_signature",
        "process_failure_signature",
    )
    present = []
    for name in signature_names:
        if name not in report:
            continue
        if report[name] is None:
            raise ReportValidationError("%s must be an object" % name)
        present.append(name)
    if len(present) > 1:
        raise ReportValidationError("report contains multiple failure signatures")
    for name in ("java_exception_signature", "python_exception_signature"):
        value = report.get(name)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ReportValidationError("%s must be an object" % name)
        class_name = value.get("class")
        message = value.get("message")
        frames = value.get("frames")
        if not isinstance(class_name, str) or not class_name:
            raise ReportValidationError("%s.class must be non-empty text" % name)
        if not isinstance(message, str):
            raise ReportValidationError("%s.message must be text" % name)
        if (
            not isinstance(frames, list)
            or not frames
            or any(not isinstance(frame, str) or not frame for frame in frames)
        ):
            raise ReportValidationError(
                "%s.frames must be a non-empty text array" % name
            )
    process = report.get("process_failure_signature")
    if process is not None:
        if not isinstance(process, dict):
            raise ReportValidationError(
                "process_failure_signature must be an object"
            )
        kind = process.get("kind")
        code = process.get("code")
        if (
            not isinstance(kind, str)
            or isinstance(code, bool)
            or not isinstance(code, int)
        ):
            raise ReportValidationError("process_failure_signature is invalid")
        if not valid_recorded_process_failure_signature(
            ProcessFailureSignature(kind, code)
        ):
            raise ReportValidationError("process_failure_signature is invalid")
        name = process.get("name")
        if name is not None and not isinstance(name, str):
            raise ReportValidationError(
                "process_failure_signature.name must be text or null"
            )

    spec = report.get("failure_spec")
    if not isinstance(spec, dict):
        return
    expected = None
    if spec["java_exception"]:
        expected = "java_exception_signature"
    elif spec["python_exception"]:
        expected = "python_exception_signature"
    elif spec["process_failure"]:
        expected = "process_failure_signature"
    if expected is not None and present != [expected]:
        raise ReportValidationError(
            "failure_spec signature mode does not match recorded signature"
        )
    if expected is None and present:
        raise ReportValidationError(
            "failure_spec disables the recorded failure signature"
        )


def _validate_execution_limits(execution: Dict[str, object]) -> None:
    """Validate serialized Docker limits before a replay can construct a runner."""
    if "limits" not in execution:
        return
    limits = execution["limits"]
    if not isinstance(limits, dict):
        raise ReportValidationError("execution.limits must be an object")

    if "cpus" in limits:
        cpus = _require_nonnegative_number(limits, "cpus", "execution.limits")
        if cpus <= 0.0:
            raise ReportValidationError("execution.limits.cpus must be positive")

    integer_limits = (
        ("memory_bytes", 6 * 1024 * 1024, "at least 6 MiB"),
        ("pids", 1, "positive"),
        ("tmpfs_bytes", 1, "positive"),
        ("workspace_bytes", 1, "positive"),
    )
    for name, minimum, description in integer_limits:
        if name not in limits:
            continue
        value = limits[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ReportValidationError(
                "execution.limits.%s must be an integer" % name
            )
        if value < minimum:
            raise ReportValidationError(
                "execution.limits.%s must be %s" % (name, description)
            )


def _validate_execution_environment(execution: Dict[str, object]) -> None:
    """Validate explicit environment metadata without requiring legacy fields."""
    if "environment_names" in execution:
        names = execution["environment_names"]
        if (
            not isinstance(names, list)
            or any(
                not isinstance(name, str)
                or _ENVIRONMENT_NAME.fullmatch(name) is None
                or name == "REPOMIN"
                for name in names
            )
            or _has_ambiguous_environment_names(names)
        ):
            raise ReportValidationError(
                "execution.environment_names contains invalid or ambiguous names"
            )
    if "environment_sha256" in execution:
        digest = execution["environment_sha256"]
        if digest is not None and (
            not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
        ):
            raise ReportValidationError(
                "execution.environment_sha256 must be SHA-256 or null"
            )


def _require_text(
    parent: Dict[str, object], name: str, *, non_empty: bool = False
) -> str:
    value = parent.get(name)
    if not isinstance(value, str) or (non_empty and not value):
        expected = "non-empty text" if non_empty else "text"
        raise ReportValidationError("%s must be %s" % (name, expected))
    return value


def _require_optional_text(parent: Dict[str, object], name: str) -> None:
    value = parent.get(name)
    if value is not None and not isinstance(value, str):
        raise ReportValidationError("%s must be text or null" % name)


def _require_int(parent: Dict[str, object], name: str) -> int:
    value = parent.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportValidationError("%s must be an integer" % name)
    return value


def _require_nonnegative_int(
    parent: Dict[str, object], name: str, context: str = ""
) -> int:
    value = _require_int(parent, name)
    if value < 0:
        prefix = (context + ".") if context else ""
        raise ReportValidationError(
            "%s%s must be non-negative" % (prefix, name)
        )
    return value


def _require_optional_nonnegative_int(
    parent: Dict[str, object], name: str, context: str = ""
) -> Optional[int]:
    if name not in parent or parent[name] is None:
        return None
    return _require_nonnegative_int(parent, name, context)


def _require_positive_int(
    parent: Dict[str, object], name: str, context: str = ""
) -> int:
    value = _require_int(parent, name)
    if value <= 0:
        prefix = (context + ".") if context else ""
        raise ReportValidationError("%s%s must be positive" % (prefix, name))
    return value


def _require_nonnegative_number(
    parent: Dict[str, object], name: str, context: str = ""
) -> float:
    value = parent.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        prefix = (context + ".") if context else ""
        raise ReportValidationError("%s%s must be a number" % (prefix, name))
    try:
        numeric = float(value)
    except OverflowError as exc:
        prefix = (context + ".") if context else ""
        raise ReportValidationError(
            "%s%s must be finite and non-negative" % (prefix, name)
        ) from exc
    if not math.isfinite(numeric) or numeric < 0:
        prefix = (context + ".") if context else ""
        raise ReportValidationError(
            "%s%s must be finite and non-negative" % (prefix, name)
        )
    return numeric


def _require_optional_probability(
    parent: Dict[str, object], name: str, context: str = ""
) -> Optional[float]:
    if name not in parent or parent[name] is None:
        return None
    value = _require_nonnegative_number(parent, name, context)
    if value > 1.0:
        prefix = (context + ".") if context else ""
        raise ReportValidationError("%s%s must be at most 1" % (prefix, name))
    return value


def _ratio_matches(observed: float, numerator: int, denominator: int) -> bool:
    """Compare a serialized ratio without allowing huge integers to escape."""
    if denominator == 0:
        return False
    try:
        expected = float(numerator) / denominator
    except (OverflowError, ZeroDivisionError):
        return False
    return math.isclose(observed, expected, rel_tol=1e-12)


def _has_ambiguous_environment_names(names: list[str]) -> bool:
    """Reject names that map to one variable on the current host platform."""
    if os.name != "nt":
        return len(set(names)) != len(names)
    folded = [name.casefold() for name in names]
    return len(set(folded)) != len(folded)


def measure_tree(root: Path) -> Tuple[int, int]:
    files = 0
    size = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            files += 1
            size += path.stat().st_size
    return files, size


def write_report(
    result: ReductionResult,
    command: str,
    match: Optional[str],
    metadata: Path,
    *,
    failure_spec: Optional[FailureSpec] = None,
    timeout_seconds: Optional[float] = None,
) -> None:
    metadata.mkdir()
    report = _build_report(
        result,
        command,
        match,
        failure_spec=failure_spec,
        timeout_seconds=timeout_seconds,
    )
    (metadata / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (metadata / "REPOMIN.md").write_text(
        _reproduction_markdown(result, command, match),
        encoding="utf-8",
    )


def verify_existing_report(
    result: ReductionResult,
    command: str,
    match: Optional[str],
    metadata: Path,
    *,
    failure_spec: Optional[FailureSpec] = None,
    timeout_seconds: Optional[float] = None,
) -> None:
    """Verify a sidecar left by a crash without overwriting user-visible files."""
    if not metadata.is_dir():
        raise ValueError("metadata output is not a directory: %s" % metadata)
    expected_names = {"report.json", "REPOMIN.md"}
    actual_names = {path.name for path in metadata.iterdir()}
    if actual_names != expected_names or not all(
        (metadata / name).is_file() for name in expected_names
    ):
        raise ValueError(
            "metadata output is incomplete or has unexpected entries: %s" % metadata
        )
    try:
        actual_report = json.loads(
            (metadata / "report.json").read_text(encoding="utf-8")
        )
        actual_markdown = (metadata / "REPOMIN.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("metadata output could not be verified: %s" % metadata) from exc

    expected_report = _build_report(
        result,
        command,
        match,
        failure_spec=failure_spec,
        timeout_seconds=timeout_seconds,
    )
    # Reports produced before version provenance was added remain resumable.
    if isinstance(actual_report, dict) and "repomin_version" not in actual_report:
        expected_report.pop("repomin_version", None)
    if isinstance(actual_report, dict) and "failure_spec" not in actual_report:
        expected_report.pop("failure_spec", None)
    if isinstance(actual_report, dict):
        actual_execution = actual_report.get("execution")
        expected_execution = expected_report.get("execution")
        if (
            isinstance(actual_execution, dict)
            and isinstance(expected_execution, dict)
            and "timeout_seconds" not in actual_execution
        ):
            expected_execution.pop("timeout_seconds", None)
        actual_output = actual_report.get("output")
        expected_output = expected_report.get("output")
        if isinstance(actual_output, dict) and isinstance(expected_output, dict):
            if "tree_sha256" not in actual_output:
                expected_output.pop("tree_sha256", None)
                expected_output.pop("tree_fingerprint_policy", None)
            if "tree_content_sha256" not in actual_output:
                expected_output.pop("tree_content_sha256", None)
                expected_output.pop("tree_content_fingerprint_policy", None)
    # A report may have been fully written immediately before the process
    # crashed. Restoration itself changes only these provenance booleans.
    for report in (actual_report, expected_report):
        if isinstance(report, dict):
            execution = report.get("execution")
            if isinstance(execution, dict):
                execution.pop("resumed", None)
            certification = report.get("holdout_certification")
            if isinstance(certification, dict):
                certification.pop("resumed", None)
    if actual_report != expected_report:
        raise ValueError(
            "metadata output does not match the certified payload: %s" % metadata
        )
    if actual_markdown != _reproduction_markdown(result, command, match):
        raise ValueError(
            "metadata reproduction instructions do not match the certified payload: %s"
            % metadata
        )


def _build_report(
    result: ReductionResult,
    command: str,
    match: Optional[str],
    *,
    failure_spec: Optional[FailureSpec] = None,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, object]:
    stats = result.stats
    holdout = result.holdout_certification
    output_fingerprint = (
        _tree_digest(result.output, set())
        if result.output.is_dir() and not result.output.is_symlink()
        else None
    )
    output_content_fingerprint = (
        _tree_content_digest(result.output, set())
        if result.output.is_dir() and not result.output.is_symlink()
        else None
    )
    report: Dict[str, object] = {
        "schema_version": 1,
        "repomin_version": __version__,
        "command": command,
        "failure_match": match,
        "baseline_exit_code": result.baseline.returncode,
        "final_exit_code": result.final_run.returncode,
        "source": {
            "files": stats.source_files,
            "bytes": stats.source_bytes,
        },
        "output": {
            "files": stats.output_files,
            "bytes": stats.output_bytes,
        },
        "attempts": stats.attempts,
        "accepted_mutations": stats.accepted,
        "cache_hits": stats.cache_hits,
        "execution": {
            "jobs": stats.jobs,
            "cache_enabled": stats.cache_enabled,
            "backend": stats.backend,
            "ignored_names": list(stats.ignored_names),
            "ignored_paths": list(stats.ignored_paths),
            "gitignore_files": list(stats.gitignore_files),
            "gitignore_sha256": stats.gitignore_sha256,
            "gitignore_recursive": stats.gitignore_recursive,
            "keep_paths": list(stats.keep_paths),
            "text_files": list(stats.text_files),
            "max_attempts": stats.max_attempts,
            "budget_exhausted": stats.budget_exhausted,
            "max_duration_seconds": stats.max_duration_seconds,
            "semantic_reducer": stats.semantic_reducer,
            "semantic_model": stats.semantic_model,
            "semantic_endpoint": stats.semantic_endpoint,
            "semantic_calls": stats.semantic_calls,
            "semantic_accepted": stats.semantic_accepted,
            "environment_names": list(stats.environment_names),
            "environment_sha256": stats.environment_sha256,
            "working_directory_policy": stats.working_directory_policy,
            "working_directory_basename": stats.working_directory_basename,
            "resumed": stats.resumed,
            "baseline_runs": stats.baseline_runs,
            "baseline_passes": stats.baseline_passes,
            "min_baseline_rate": stats.min_baseline_rate,
            "min_candidate_rate": stats.min_candidate_rate,
            "confidence": stats.confidence,
            "candidate_sampling_policy": stats.candidate_sampling_policy,
            "run_confidence": stats.run_confidence,
            "candidate_family_control_policy": (
                stats.candidate_family_control_policy
            ),
            "candidate_family_count": stats.candidate_family_count,
            "candidate_family_alpha_upper_bound": (
                stats.candidate_family_alpha_upper_bound
            ),
            "reduction_strategy": stats.reduction_strategy,
            "baseline_rate": stats.baseline_rate,
            "baseline_lower_bound": stats.baseline_lower_bound,
            "baseline_rate_evidence_runs": stats.baseline_rate_evidence_runs,
            "baseline_rate_evidence_passes": stats.baseline_rate_evidence_passes,
            "baseline_exact_lower_bound": stats.baseline_exact_lower_bound,
            "baseline_exact_p_value": stats.baseline_exact_p_value,
            "baseline_exact_rate_gate_passed": (
                stats.baseline_exact_rate_gate_passed
            ),
            "candidate_runs": stats.candidate_runs,
            "candidate_min_passes": stats.candidate_min_passes,
            "candidate_samples": stats.candidate_samples,
            "candidate_passes": stats.candidate_passes,
            "candidate_early_rejections": stats.candidate_early_rejections,
            "candidate_early_acceptances": stats.candidate_early_acceptances,
            "candidate_samples_saved": stats.candidate_samples_saved,
            "final_runs": stats.final_runs,
            "final_passes": stats.final_passes,
            "final_rate": stats.final_rate,
            "final_lower_bound": stats.final_lower_bound,
        },
        "phase_statistics": {
            "schema_version": 1,
            "coverage": (
                "complete" if stats.phase_statistics_complete else "partial"
            ),
            "byte_accounting": "net-regular-file-bytes-v1",
            "oracle_time_accounting": "sum-run-result-duration-v1",
            "phases": [
                {
                    "phase": phase.phase,
                    "passes": phase.passes,
                    "completed_passes": phase.completed_passes,
                    "aborted_passes": phase.aborted_passes,
                    "wall_seconds": round(phase.wall_seconds, 4),
                    "bytes_removed": phase.bytes_removed,
                    "bytes_added": phase.bytes_added,
                    "attempts": phase.attempts,
                    "no_op": phase.no_op,
                    "rejected": phase.rejected,
                    "accepted": phase.accepted,
                    "superseded": phase.superseded,
                    "aborted": phase.aborted,
                    "oracle_sample_uses": phase.oracle_sample_uses,
                    "oracle_samples": phase.oracle_samples,
                    "oracle_passing_sample_uses": (
                        phase.oracle_passing_sample_uses
                    ),
                    "oracle_seconds": round(phase.oracle_seconds, 4),
                    "cache_hits": phase.cache_hits,
                    "samples_saved": phase.samples_saved,
                }
                for phase in stats.phase_stats.values()
            ],
        },
        "holdout_certification": {
            "schema_version": 1,
            "status": holdout.status,
            "policy": holdout.policy,
            "attempt_id": holdout.attempt_id,
            "planned_runs": holdout.planned_runs,
            "completed_runs": holdout.completed_runs,
            "passes": holdout.passes,
            "ordinary_failures": sum(
                sample.outcome == "failed" for sample in holdout.samples
            ),
            "minimum_rate": holdout.minimum_rate,
            "confidence": holdout.confidence,
            "alpha": (
                None if holdout.confidence is None else 1.0 - holdout.confidence
            ),
            "required_passes": holdout.required_passes,
            "observed_rate": holdout.observed_rate,
            "exact_lower_bound": holdout.exact_lower_bound,
            "exact_p_value": holdout.exact_p_value,
            "exact_rate_gate_passed": holdout.exact_rate_gate_passed,
            "timed_out_runs": holdout.timed_out_runs,
            "resource_exhausted_runs": holdout.resource_exhausted_runs,
            "interrupted_runs": holdout.interrupted_runs,
            "artifact_fingerprint": holdout.artifact_fingerprint,
            "artifact_fingerprint_policy": TREE_FINGERPRINT_POLICY,
            "artifact_scope": "exported-payload-tree-v1",
            "oracle_identity_sha256": holdout.oracle_identity_sha256,
            "fresh_repository_copy_per_run": (
                holdout.fresh_repository_copy_per_run
            ),
            "cache_used": holdout.cache_used,
            "early_stopping": holdout.early_stopping,
            "resumed": holdout.resumed,
            "iid_assumption": "required-not-verified",
            "samples": [
                {
                    "index": sample.index,
                    "outcome": sample.outcome,
                    "accepted": sample.accepted,
                    "returncode": sample.returncode,
                    "duration_seconds": (
                        None
                        if sample.duration_seconds is None
                        else round(sample.duration_seconds, 4)
                    ),
                    "timed_out": sample.timed_out,
                    "resource_exhausted": sample.resource_exhausted,
                    "resource_reason": sample.resource_reason,
                    "output_sha256": sample.output_sha256,
                }
                for sample in holdout.samples
            ],
        },
        "events": [
            {
                "phase": event.phase,
                "description": event.description,
                "duration_seconds": round(event.duration_seconds, 4),
                "oracle_runs": event.oracle_runs,
                "oracle_passes": event.oracle_passes,
                "oracle_rate": event.oracle_rate,
                "oracle_lower_bound": event.oracle_lower_bound,
                "oracle_anytime_lower_bound": event.oracle_anytime_lower_bound,
                "oracle_early_acceptance": event.oracle_early_acceptance,
                "candidate_family_index": event.candidate_family_index,
                "candidate_confidence": event.candidate_confidence,
                "candidate_alpha": event.candidate_alpha,
            }
            for event in stats.events
        ],
    }
    output = report["output"]
    assert isinstance(output, dict)
    if output_fingerprint is not None:
        output["tree_sha256"] = output_fingerprint
        output["tree_fingerprint_policy"] = TREE_FINGERPRINT_POLICY
    if output_content_fingerprint is not None:
        output["tree_content_sha256"] = output_content_fingerprint
        output["tree_content_fingerprint_policy"] = (
            TREE_CONTENT_FINGERPRINT_POLICY
        )
    if failure_spec is not None:
        report["failure_spec"] = {
            "schema_version": 1,
            "match": failure_spec.match,
            "exit_code": failure_spec.exit_code,
            "java_exception": failure_spec.java_exception,
            "python_exception": failure_spec.python_exception,
            "process_failure": failure_spec.process_failure,
        }
    execution = report["execution"]
    assert isinstance(execution, dict)
    if timeout_seconds is not None:
        execution["timeout_seconds"] = timeout_seconds
    if stats.container_image is not None:
        execution["image"] = stats.container_image
    if stats.container_image_id is not None:
        execution["image_id"] = stats.container_image_id
    if stats.session_path is not None:
        execution["session_path"] = stats.session_path
    if stats.container_network is not None:
        execution["network"] = stats.container_network
    limits = {}
    if stats.container_cpus is not None:
        limits["cpus"] = stats.container_cpus
    if stats.container_memory_bytes is not None:
        limits["memory_bytes"] = stats.container_memory_bytes
    if stats.container_pids_limit is not None:
        limits["pids"] = stats.container_pids_limit
    if stats.container_tmpfs_bytes is not None:
        limits["tmpfs_bytes"] = stats.container_tmpfs_bytes
    if stats.container_workspace_limit_bytes is not None:
        limits["workspace_bytes"] = stats.container_workspace_limit_bytes
    if limits:
        execution["limits"] = limits
    if result.java_exception_signature is not None:
        signature = result.java_exception_signature
        report["java_exception_signature"] = {
            "class": signature.class_name,
            "message": signature.message,
            "frames": list(signature.frames),
        }
    if result.python_exception_signature is not None:
        signature = result.python_exception_signature
        report["python_exception_signature"] = {
            "class": signature.class_name,
            "message": signature.message,
            "frames": list(signature.frames),
        }
    if result.process_failure_signature is not None:
        signature = result.process_failure_signature
        process_signature: Dict[str, object] = {
            "kind": signature.kind,
            "code": signature.code,
        }
        name = process_failure_name(signature)
        if name is not None:
            process_signature["name"] = name
        report["process_failure_signature"] = process_signature
    return report


def _reproduction_markdown(
    result: ReductionResult,
    command: str,
    match: Optional[str],
) -> str:
    match_markdown = ""
    if match is not None:
        match_markdown = "Expected output match: `%s`\n\n" % match.replace(
            "`", "\\`"
        )
    return (
        "# Minimal reproduction\n\n"
        "This repository was reduced by ReproMin while preserving the configured "
        "failure signature.\n\n"
        "## Reproduce\n\n"
        + _shell_markdown(command)
        + _execution_markdown(result)
        + match_markdown
        + _java_signature_markdown(result)
        + _python_signature_markdown(result)
        + _process_signature_markdown(result)
        + _holdout_markdown(result)
        + _payload_markdown(result)
        + "See `report.json` in this metadata directory for reduction statistics.\n"
    )


def _shell_markdown(command: str) -> str:
    """Wrap a shell command without allowing its backticks to close the fence."""
    fence = "```"
    while fence in command:
        fence += "`"
    return "%ssh\n%s\n%s\n\n" % (fence, command, fence)


def _payload_markdown(result: ReductionResult) -> str:
    """Summarize the exported files without making reports unbounded."""
    files = sorted(
        path.relative_to(result.output).as_posix()
        for path in result.output.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    display_limit = 200
    displayed = files[:display_limit]
    lines = [
        "## Payload",
        "",
        "Reduced payload: `%d` files, `%d` bytes."
        % (result.stats.output_files, result.stats.output_bytes),
        "",
        "```text",
    ]
    lines.extend(displayed)
    if len(files) > display_limit:
        lines.append("... (%d more files)" % (len(files) - display_limit))
    lines.extend(["```", ""])
    return "\n".join(lines)


def _execution_markdown(result: ReductionResult) -> str:
    """Describe the recorded execution boundary without exposing env values."""
    stats = result.stats
    lines = ["## Execution\n", "Backend: `%s`" % stats.backend]
    if stats.backend == "docker":
        if stats.container_image is not None:
            lines.append("Docker image reference: `%s`" % stats.container_image)
        if stats.container_image_id is not None:
            lines.append("Docker image ID: `%s`" % stats.container_image_id)
        if stats.container_network is not None:
            lines.append("Docker network policy: `%s`" % stats.container_network)
    if stats.environment_names:
        names = ", ".join("`%s`" % name for name in stats.environment_names)
        lines.append("Environment variable names: %s (values are not recorded)" % names)
    return "\n".join(lines) + "\n\n"


def _java_signature_markdown(result: ReductionResult) -> str:
    signature = result.java_exception_signature
    if signature is None:
        return ""
    location = signature.frames[0] if signature.frames else "<no frame>"
    return (
        "Expected Java exception: `%s: %s` at `%s`\n\n"
        % (
            signature.class_name.replace("`", "\\`"),
            signature.message.replace("`", "\\`"),
            location.replace("`", "\\`"),
        )
    )


def _python_signature_markdown(result: ReductionResult) -> str:
    signature = result.python_exception_signature
    if signature is None:
        return ""
    location = signature.frames[0] if signature.frames else "<no frame>"
    return (
        "Expected Python exception: `%s: %s` at `%s`\n\n"
        % (
            signature.class_name.replace("`", "\\`"),
            signature.message.replace("`", "\\`"),
            location.replace("`", "\\`"),
        )
    )


def _process_signature_markdown(result: ReductionResult) -> str:
    signature = result.process_failure_signature
    if signature is None:
        return ""
    name = process_failure_name(signature)
    if signature.kind == "posix_signal":
        detail = "POSIX signal `%s` (`%d`)" % (name or "unknown", signature.code)
    elif signature.kind == "windows_status":
        detail = "Windows status `0x%08X`" % signature.code
        if name is not None:
            detail += " (`%s`)" % name
    else:
        detail = "exit code `%d`" % signature.code
    return "Expected process failure: %s\n\n" % detail


def _holdout_markdown(result: ReductionResult) -> str:
    certification = result.holdout_certification
    if certification.status != "certified":
        return ""
    return (
        "Holdout certification: `%d/%d` fresh samples passed; the %.1f%% "
        "one-sided exact lower bound is `%.4f`.\n\n"
        % (
            certification.passes,
            certification.planned_runs,
            100.0 * (certification.confidence or 0.0),
            certification.exact_lower_bound or 0.0,
        )
    )
