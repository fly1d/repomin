"""Fresh-copy replay of a failure contract recorded in ``report.json``."""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from repomin.execution import CommandRunner, DockerRunner
from repomin.model import (
    FailureSpec,
    JavaExceptionSignature,
    ProcessFailureSignature,
    PythonExceptionSignature,
)
from repomin.oracle import FailureOracle
from repomin.report import (
    _payload_fingerprint_evidence,
    measure_tree,
    validate_report_file,
)
from repomin.session import (
    _cleanup_tool_owned_paths,
    _copy_repository,
    _run_observation_digest,
    _tree_content_digest,
    _tree_digest,
)


REPLAY_SCHEMA_VERSION = 1
DEFAULT_REPLAY_TIMEOUT_SECONDS = 120.0
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOCKER_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReplayError(ValueError):
    """The report or replay configuration cannot be executed faithfully."""


def replay_report(
    report_path: Path,
    payload: Path,
    *,
    runs: int = 1,
    timeout_seconds: Optional[float] = None,
    environment: Optional[Mapping[str, str]] = None,
    backend: str = "recorded",
    docker_image: Optional[str] = None,
    docker_network: Optional[str] = None,
    legacy_exit_code: Optional[int] = None,
) -> Tuple[bool, Dict[str, object]]:
    """Execute every replay sample in a new copy and return its evidence."""
    _validate_runs(runs)
    report_path = _regular_report_path(Path(report_path))
    payload = _payload_path(Path(payload))
    report = validate_report_file(report_path, payload)
    execution = report["execution"]
    output = report["output"]
    holdout = report["holdout_certification"]
    assert isinstance(execution, dict)
    assert isinstance(output, dict)
    assert isinstance(holdout, dict)

    fingerprint_mode, initial_full_fingerprint, initial_content_fingerprint = (
        _payload_fingerprint_evidence(report, payload)
    )
    expected_fingerprint = output.get("tree_sha256") or holdout.get(
        "artifact_fingerprint"
    )
    expected_content_fingerprint = output.get("tree_content_sha256")
    initial_fingerprint = (
        initial_content_fingerprint
        if fingerprint_mode == "content"
        else initial_full_fingerprint
    )
    measured_files, measured_bytes = measure_tree(payload)
    if measured_files != output["files"] or measured_bytes != output["bytes"]:
        raise ReplayError(
            "payload size differs from report: expected %d files/%d bytes, got %d/%d"
            % (
                output["files"],
                output["bytes"],
                measured_files,
                measured_bytes,
            )
        )

    configured_environment = dict(environment or {})
    _validate_environment(execution, configured_environment)
    spec, signature, oracle_source, oracle_mode = _recorded_oracle(
        report,
        legacy_exit_code,
    )
    timeout, timeout_source = _replay_timeout(execution, timeout_seconds)
    runner, runner_evidence = _build_runner(
        report,
        spec,
        configured_environment,
        timeout,
        backend,
        docker_image,
        docker_network,
    )
    oracle = FailureOracle(runner, spec)
    if signature is not None:
        oracle.pin_failure_signature(signature)

    effective_backend = runner_evidence["backend"]
    assert isinstance(effective_backend, str)
    working_basename = _working_basename(execution, payload, effective_backend)
    temporary_parent = payload.parent if effective_backend == "docker" else None
    temporary_root = Path(
        tempfile.mkdtemp(
            prefix=".repomin-replay-",
            dir=str(temporary_parent) if temporary_parent is not None else None,
        )
    )
    samples = []
    try:
        for index in range(1, runs + 1):
            current_fingerprint = (
                _tree_content_digest(payload, set())
                if fingerprint_mode == "content"
                else _tree_digest(payload, set())
            )
            if current_fingerprint != initial_fingerprint:
                raise ReplayError("payload changed while replay was running")
            attempt_root = temporary_root / ("run-%04d" % index)
            attempt_root.mkdir()
            working_copy = attempt_root / working_basename
            try:
                _copy_repository(payload, working_copy, set())
                run_result = runner.run(working_copy)
                accepted = oracle.accepts(run_result)
                sample = {
                    "index": index,
                    "outcome": _outcome(run_result, accepted),
                    "accepted": accepted,
                    "mismatch_reason": _mismatch_reason(
                        oracle,
                        run_result,
                        accepted,
                    ),
                    "returncode": run_result.returncode,
                    "duration_seconds": round(run_result.duration_seconds, 4),
                    "timed_out": run_result.timed_out,
                    "resource_exhausted": run_result.resource_exhausted,
                    "output_sha256": _run_observation_digest(run_result),
                }
                if not accepted:
                    # Keep mismatch diagnostics useful without retaining the
                    # configured regular expression or command output.
                    sample.update(
                        {
                            "expected_exit_code": oracle.spec.exit_code,
                            "actual_exit_code": run_result.returncode,
                        }
                    )
                samples.append(sample)
            finally:
                _cleanup_tool_owned_paths(
                    [attempt_root],
                    "replay command working directory",
                )
    finally:
        _cleanup_tool_owned_paths([temporary_root], "replay temporary directory")

    final_full_fingerprint = _tree_digest(payload, set())
    final_content_fingerprint = (
        _tree_content_digest(payload, set())
        if fingerprint_mode == "content"
        else None
    )
    final_fingerprint = (
        final_content_fingerprint
        if fingerprint_mode == "content"
        else final_full_fingerprint
    )
    if final_fingerprint != initial_fingerprint:
        raise ReplayError("payload changed while replay was running")
    passes = sum(sample["accepted"] is True for sample in samples)
    reproduced = passes == runs
    result: Dict[str, object] = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "valid_report": True,
        "reproduced": reproduced,
        "report": str(report_path),
        "payload": str(payload),
        "report_schema_version": report["schema_version"],
        "holdout_status": holdout["status"],
        "backend": effective_backend,
        "recorded_backend": execution["backend"],
        "oracle_mode": oracle_mode,
        "oracle_source": oracle_source,
        "timeout_seconds": timeout,
        "timeout_source": timeout_source,
        "runs": runs,
        "passes": passes,
        "failures": runs - passes,
        "fresh_repository_copy_per_run": True,
        "cache_used": False,
        "ambient_environment_pinned": False,
        "environment_names": sorted(configured_environment),
        "expected_fingerprint": expected_fingerprint,
        "actual_fingerprint": final_full_fingerprint,
        "expected_content_fingerprint": expected_content_fingerprint,
        "actual_content_fingerprint": final_content_fingerprint,
        "fingerprint_mode": fingerprint_mode,
        "metadata_drift_possible": fingerprint_mode == "content",
        "fingerprint_verified": fingerprint_mode != "unavailable",
        "samples": samples,
    }
    result.update(runner_evidence)
    return reproduced, result


def format_replay(result: Mapping[str, object]) -> str:
    """Render replay evidence without exposing command output or env values."""
    reproduced = bool(result.get("reproduced"))
    lines = [
        "ReproMin replay: %s" % ("reproduced" if reproduced else "not reproduced"),
        "Fresh runs: %s/%s passed" % (result.get("passes"), result.get("runs")),
        "Backend: %s (recorded: %s)"
        % (result.get("backend"), result.get("recorded_backend")),
    ]
    fingerprint_mode = result.get("fingerprint_mode")
    if fingerprint_mode == "exact":
        lines.append("Payload fingerprint: verified")
    elif fingerprint_mode == "content":
        lines.append(
            "Payload fingerprint: content verified (metadata may have drifted)"
        )
    else:
        lines.append("Payload fingerprint: unavailable in this legacy report")
    if not reproduced:
        samples = result.get("samples", [])
        if isinstance(samples, list):
            for sample in samples:
                if isinstance(sample, dict) and not sample.get("accepted"):
                    details = [str(sample.get("mismatch_reason"))]
                    expected = sample.get("expected_exit_code")
                    actual = sample.get("actual_exit_code")
                    if "actual_exit_code" in sample:
                        if expected is None:
                            details.append(
                                "exit code actual %s (no exact exit-code contract)"
                                % actual
                            )
                        else:
                            details.append(
                                "exit code expected %s, actual %s"
                                % (expected, actual)
                            )
                    lines.append(
                        "Run %s: %s (%s)"
                        % (
                            sample.get("index"),
                            sample.get("outcome"),
                            "; ".join(details),
                        )
                    )
    lines.append(
        "This is a current-environment replay, not a correctness or root-cause proof."
    )
    return "\n".join(lines) + "\n"


def _regular_report_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ReplayError("report path must not be a symbolic link")
    try:
        resolved = expanded.resolve()
    except (OSError, RuntimeError) as exc:
        raise ReplayError("report path could not be resolved: %s" % expanded) from exc
    if not resolved.is_file():
        raise ReplayError("report is not a regular file: %s" % resolved)
    return resolved


def _payload_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ReplayError("payload root must not be a symbolic link")
    try:
        resolved = expanded.resolve()
    except (OSError, RuntimeError) as exc:
        raise ReplayError("payload path could not be resolved: %s" % expanded) from exc
    if not resolved.is_dir():
        raise ReplayError("payload is not a directory: %s" % resolved)
    return resolved


def _validate_runs(runs: int) -> None:
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        raise ReplayError("replay runs must be an integer of at least 1")


def _environment_digest(environment: Mapping[str, str]) -> str:
    encoded = "".join(
        "%s=%s\0" % (name, environment[name]) for name in sorted(environment)
    ).encode("utf-8", errors="surrogateescape")
    return hashlib.sha256(encoded).hexdigest()


def _validate_environment(
    execution: Mapping[str, object],
    environment: Mapping[str, str],
) -> None:
    if any(
        not isinstance(name, str)
        or _ENVIRONMENT_NAME.fullmatch(name) is None
        or name == "REPOMIN"
        or not isinstance(value, str)
        or "\x00" in value
        for name, value in environment.items()
    ):
        raise ReplayError("replay environment contains an invalid name or value")
    names = execution.get("environment_names", [])
    if (
        not isinstance(names, list)
        or any(
            not isinstance(name, str)
            or _ENVIRONMENT_NAME.fullmatch(name) is None
            or name == "REPOMIN"
            for name in names
        )
        or _has_ambiguous_environment_names(names)
        or _has_ambiguous_environment_names(list(environment))
    ):
        raise ReplayError(
            "report contains invalid or case-insensitive duplicate environment names"
        )
    if set(environment) != set(names):
        raise ReplayError(
            "replay environment names must exactly match the report: %s"
            % (", ".join(sorted(names)) if names else "none")
        )
    expected = execution.get("environment_sha256")
    if expected is None:
        if names:
            raise ReplayError(
                "report is missing the environment digest for explicit variables"
            )
        return
    if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
        raise ReplayError("report contains an invalid environment digest")
    if _environment_digest(environment) != expected:
        raise ReplayError(
            "replay environment values do not match the report digest"
        )


def _recorded_oracle(
    report: Mapping[str, object],
    legacy_exit_code: Optional[int],
) -> Tuple[FailureSpec, Optional[object], str, str]:
    if legacy_exit_code is not None and (
        isinstance(legacy_exit_code, bool)
        or not isinstance(legacy_exit_code, int)
    ):
        raise ReplayError("legacy exit code must be an integer")
    failure_spec = report.get("failure_spec")
    if isinstance(failure_spec, dict):
        if legacy_exit_code is not None:
            raise ReplayError(
                "--exit-code is only valid for a legacy report without failure_spec"
            )
        spec = FailureSpec(
            failure_spec["match"],
            failure_spec["exit_code"],
            java_exception=failure_spec["java_exception"],
            python_exception=failure_spec["python_exception"],
            process_failure=failure_spec["process_failure"],
        )
        source = "recorded"
    else:
        java = "java_exception_signature" in report
        python = "python_exception_signature" in report
        process = "process_failure_signature" in report
        match = report.get("failure_match")
        if process and legacy_exit_code is not None:
            raise ReplayError(
                "--exit-code cannot override a recorded process failure signature"
            )
        if match is None and not (java or python or process) and legacy_exit_code is None:
            raise ReplayError(
                "legacy report does not record its exit-code contract; pass --exit-code N"
            )
        spec = FailureSpec(
            match if isinstance(match, str) else None,
            legacy_exit_code,
            java_exception=java,
            python_exception=python,
            process_failure=process,
        )
        source = "legacy_inferred"

    signature: Optional[object] = None
    mode = "match_or_exit"
    if spec.java_exception:
        signature = _exception_signature(report, "java_exception_signature", True)
        mode = "java_exception"
    elif spec.python_exception:
        signature = _exception_signature(report, "python_exception_signature", False)
        mode = "python_exception"
    elif spec.process_failure:
        value = report["process_failure_signature"]
        assert isinstance(value, dict)
        signature = ProcessFailureSignature(value["kind"], value["code"])
        mode = "process_failure"
    elif spec.exit_code is not None and spec.match is None:
        mode = "exit_code"
    elif spec.exit_code is not None:
        mode = "match_and_exit_code"
    return spec, signature, source, mode


def _exception_signature(
    report: Mapping[str, object],
    name: str,
    java: bool,
) -> object:
    value = report[name]
    if not isinstance(value, dict):
        raise ReplayError("report contains an invalid %s" % name)
    class_name = value.get("class")
    message = value.get("message")
    frames = value.get("frames")
    if (
        not isinstance(class_name, str)
        or not class_name
        or not isinstance(message, str)
        or not isinstance(frames, list)
        or not frames
        or any(not isinstance(frame, str) or not frame for frame in frames)
    ):
        raise ReplayError("report contains an invalid %s" % name)
    signature_type = JavaExceptionSignature if java else PythonExceptionSignature
    return signature_type(
        class_name,
        message,
        tuple(frames),
    )


def _replay_timeout(
    execution: Mapping[str, object],
    override: Optional[float],
) -> Tuple[float, str]:
    recorded = execution.get("timeout_seconds")
    value = override if override is not None else recorded
    source = "override" if override is not None else "report"
    if value is None:
        value = DEFAULT_REPLAY_TIMEOUT_SECONDS
        source = "default"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplayError("replay timeout must be a finite number greater than zero")
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise ReplayError(
            "replay timeout must be a finite number greater than zero"
        ) from exc
    if not math.isfinite(numeric) or numeric <= 0.0:
        raise ReplayError("replay timeout must be a finite number greater than zero")
    return numeric, source


def _build_runner(
    report: Mapping[str, object],
    spec: FailureSpec,
    environment: Mapping[str, str],
    timeout: float,
    backend: str,
    docker_image: Optional[str],
    docker_network: Optional[str],
) -> Tuple[object, Dict[str, object]]:
    execution = report["execution"]
    assert isinstance(execution, dict)
    recorded_backend = execution["backend"]
    if not isinstance(backend, str) or backend not in {"recorded", "host", "docker"}:
        raise ReplayError("replay backend must be recorded, host, or docker")
    effective_backend = recorded_backend if backend == "recorded" else backend
    command = report["command"]
    if not isinstance(command, str) or not command:
        raise ReplayError("report command must be non-empty text")
    if "\x00" in command:
        raise ReplayError("report command must not contain NUL")
    if effective_backend == "host":
        if docker_image is not None or docker_network is not None:
            raise ReplayError("Docker options require the docker replay backend")
        runner = CommandRunner(
            command,
            timeout,
            environment=environment,
            collect_java_diagnostics=spec.java_exception,
        )
        return runner, {
            "backend": "host",
            "recorded_backend": recorded_backend,
            "docker_image": None,
            "docker_image_id": None,
            "docker_network": None,
        }

    recorded_image_id = execution.get("image_id")
    if docker_image is None:
        if (
            not isinstance(recorded_image_id, str)
            or _DOCKER_IMAGE_ID.fullmatch(recorded_image_id) is None
        ):
            raise ReplayError(
                "Docker replay requires a recorded immutable image ID or --docker-image"
            )
        selected_image = recorded_image_id
    else:
        selected_image = docker_image
    if not isinstance(selected_image, str) or not selected_image:
        raise ReplayError("Docker image reference must be non-empty text")
    network = "none" if docker_network is None else docker_network
    if not isinstance(network, str):
        raise ReplayError("Docker network policy must be text")
    limits = execution.get("limits", {})
    if not isinstance(limits, dict):
        raise ReplayError("report execution.limits must be an object")
    runner = DockerRunner(
        command,
        timeout,
        image=selected_image,
        network=network,
        environment=environment,
        collect_java_diagnostics=spec.java_exception,
        cpus=_optional_number(limits, "cpus"),
        memory_bytes=_optional_int(limits, "memory_bytes"),
        pids_limit=_optional_int(limits, "pids", 512),
        tmpfs_bytes=_optional_int(limits, "tmpfs_bytes", 1024 * 1024 * 1024),
        workspace_limit_bytes=_optional_int(limits, "workspace_bytes"),
    )
    runner.validate()
    return runner, {
        "backend": "docker",
        "recorded_backend": recorded_backend,
        "docker_image": selected_image,
        "docker_image_id": runner.resolved_image_id,
        "recorded_docker_image_id": recorded_image_id,
        "docker_network": network,
        "recorded_docker_network": execution.get("network"),
    }


def _optional_number(
    values: Mapping[str, object],
    name: str,
) -> Optional[float]:
    if name not in values:
        return None
    value = values[name]
    if value is None:
        raise ReplayError(
            "report execution.limits.%s must be a finite number greater than zero"
            % name
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplayError("report execution.limits.%s must be a number" % name)
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise ReplayError(
            "report execution.limits.%s must be a finite number greater than zero"
            % name
        ) from exc
    if not math.isfinite(numeric) or numeric <= 0.0:
        raise ReplayError(
            "report execution.limits.%s must be a finite number greater than zero"
            % name
        )
    return numeric


def _optional_int(
    values: Mapping[str, object],
    name: str,
    default: Optional[int] = None,
) -> Optional[int]:
    if name not in values:
        value = default
    else:
        value = values[name]
        if value is None:
            raise ReplayError("report execution.limits.%s must be an integer" % name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayError("report execution.limits.%s must be an integer" % name)
    return value


def _working_basename(
    execution: Mapping[str, object],
    payload: Path,
    backend: str,
) -> str:
    if backend == "docker":
        return "repository"
    if "working_directory_basename" not in execution:
        # Legacy reports did not persist the host working-directory name.
        value = payload.name
    else:
        value = execution["working_directory_basename"]
    if not isinstance(value, str):
        raise ReplayError("recorded host working directory basename is invalid")
    parsed = Path(value)
    if (
        not value
        or value in {".", ".."}
        or "\x00" in value
        or parsed.is_absolute()
        or len(parsed.parts) != 1
        or parsed.name != value
    ):
        raise ReplayError("recorded host working directory basename is invalid")
    return value


def _has_ambiguous_environment_names(names: list[str]) -> bool:
    """Detect names that collapse on Windows while retaining exact-name policy."""
    if os.name != "nt":
        return len(set(names)) != len(names)
    folded = [name.casefold() for name in names]
    return len(set(folded)) != len(folded)


def _outcome(result: object, accepted: bool) -> str:
    if result.timed_out:
        return "timed_out"
    if result.resource_exhausted:
        return "resource_exhausted"
    return "passed" if accepted else "failed"


def _mismatch_reason(
    oracle: FailureOracle,
    result: object,
    accepted: bool,
) -> Optional[str]:
    if accepted:
        return None
    if result.timed_out:
        return "timeout"
    if result.resource_exhausted:
        return "resource"
    expected_exit = oracle.spec.exit_code
    if (expected_exit is None and result.returncode == 0) or (
        expected_exit is not None and result.returncode != expected_exit
    ):
        return "exit_code"
    match_text = result.output
    if oracle.spec.java_exception and result.diagnostics:
        match_text += "\n" + result.diagnostics
    if oracle.spec.match and re.search(oracle.spec.match, match_text) is None:
        return "match"
    if (
        oracle.spec.java_exception
        or oracle.spec.python_exception
        or oracle.spec.process_failure
    ):
        return "signature"
    return "unknown"
