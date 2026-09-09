from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
from typing import Mapping, Optional, Sequence, Tuple

from repomin.cli import _validation_summary, format_validation_markdown
from repomin.report import ReportValidationError, validate_report_file


_ACTION_INPUTS = (
    "REPOMIN_CONFIG",
    "REPOMIN_SOURCE",
    "REPOMIN_OUTPUT",
    "REPOMIN_COMMAND",
    "REPOMIN_MATCH",
    "REPOMIN_EXIT_CODE",
    "REPOMIN_JAVA_EXCEPTION",
    "REPOMIN_PYTHON_EXCEPTION",
    "REPOMIN_PROCESS_FAILURE",
    "REPOMIN_HOLDOUT_RUNS",
    "REPOMIN_MIN_HOLDOUT_RATE",
    "REPOMIN_HOLDOUT_CONFIDENCE",
    "REPOMIN_IGNORE",
    "REPOMIN_IGNORE_PATH",
    "REPOMIN_KEEP",
    "REPOMIN_TEXT_FILE",
    "REPOMIN_GITIGNORE",
    "REPOMIN_GITIGNORE_RECURSIVE",
    "REPOMIN_ADAPTER",
    "REPOMIN_SOURCE_REDUCER",
    "REPOMIN_BACKEND",
    "REPOMIN_DOCKER_IMAGE",
    "REPOMIN_DOCKER_NETWORK",
    "REPOMIN_TIMEOUT",
    "REPOMIN_MAX_ATTEMPTS",
    "REPOMIN_MAX_DURATION",
    "REPOMIN_JOBS",
    "REPOMIN_STEP_SUMMARY",
)
_REQUIRED_RUNNER_VARIABLES = ("GITHUB_WORKSPACE", "GITHUB_OUTPUT")
_CONFIG_SEMANTIC_DEFAULTS = (
    ("command", "REPOMIN_COMMAND", ""),
    ("match", "REPOMIN_MATCH", ""),
    ("exit-code", "REPOMIN_EXIT_CODE", ""),
    ("java-exception", "REPOMIN_JAVA_EXCEPTION", "false"),
    ("python-exception", "REPOMIN_PYTHON_EXCEPTION", "false"),
    ("process-failure", "REPOMIN_PROCESS_FAILURE", "false"),
    ("holdout-runs", "REPOMIN_HOLDOUT_RUNS", ""),
    ("min-holdout-rate", "REPOMIN_MIN_HOLDOUT_RATE", ""),
    ("holdout-confidence", "REPOMIN_HOLDOUT_CONFIDENCE", ""),
    ("ignore", "REPOMIN_IGNORE", ""),
    ("ignore-path", "REPOMIN_IGNORE_PATH", ""),
    ("keep", "REPOMIN_KEEP", ""),
    ("text-file", "REPOMIN_TEXT_FILE", ""),
    ("gitignore", "REPOMIN_GITIGNORE", "false"),
    ("gitignore-recursive", "REPOMIN_GITIGNORE_RECURSIVE", "false"),
    ("adapter", "REPOMIN_ADAPTER", "auto"),
    ("source-reducer", "REPOMIN_SOURCE_REDUCER", "auto"),
    ("backend", "REPOMIN_BACKEND", "host"),
    ("docker-image", "REPOMIN_DOCKER_IMAGE", ""),
    ("docker-network", "REPOMIN_DOCKER_NETWORK", "none"),
    ("timeout", "REPOMIN_TIMEOUT", "120"),
    ("max-attempts", "REPOMIN_MAX_ATTEMPTS", ""),
    ("max-duration", "REPOMIN_MAX_DURATION", ""),
    ("jobs", "REPOMIN_JOBS", "1"),
)
_SIGNATURE_INPUTS = (
    ("java-exception", "REPOMIN_JAVA_EXCEPTION", "--java-exception"),
    ("python-exception", "REPOMIN_PYTHON_EXCEPTION", "--python-exception"),
    ("process-failure", "REPOMIN_PROCESS_FAILURE", "--process-failure"),
)
_REPEATED_OPTIONS = (
    ("--ignore", "REPOMIN_IGNORE"),
    ("--ignore-path", "REPOMIN_IGNORE_PATH"),
    ("--keep", "REPOMIN_KEEP"),
    ("--text-file", "REPOMIN_TEXT_FILE"),
)
_OPTIONAL_VALUE_OPTIONS = (
    ("--holdout-runs", "REPOMIN_HOLDOUT_RUNS"),
    ("--min-holdout-rate", "REPOMIN_MIN_HOLDOUT_RATE"),
    ("--holdout-confidence", "REPOMIN_HOLDOUT_CONFIDENCE"),
)
_SUMMARY_OUTPUTS = (
    ("report-schema-version", "schema_version", True),
    ("source-files", "source_files", True),
    ("source-bytes", "source_bytes", True),
    ("output-files", "output_files", True),
    ("output-bytes", "output_bytes", True),
    ("attempts", "attempts", True),
    ("accepted-mutations", "accepted_mutations", True),
    ("holdout-status", "holdout_status", True),
    ("oracle-mode", "oracle_mode", False),
    ("file-retention-ratio", "file_retention_ratio", False),
    ("byte-retention-ratio", "byte_retention_ratio", False),
    ("payload-fingerprint-mode", "payload_fingerprint_mode", False),
    ("payload-fingerprint-verified", "payload_fingerprint_verified", False),
)
_UNSAFE_PATH_COMPONENT = re.compile(r'[<>:"/\\|?*\[]|[\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *("com%d" % number for number in range(1, 10)),
    *("lpt%d" % number for number in range(1, 10)),
}
_SERVER_URL = re.compile(r"https://[A-Za-z0-9.-]+")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_RUN_ID = re.compile(r"[0-9]+")


class ActionInputError(ValueError):
    """One GitHub Action input or runner path is invalid."""


class ActionRuntimeError(RuntimeError):
    """The reducer completed without producing a usable Action result."""


@dataclass(frozen=True)
class ActionPlan:
    arguments: Tuple[str, ...]
    payload_path: Path
    report_path: Path
    step_summary: bool


def _require_environment(environment: Mapping[str, str]) -> None:
    missing = [
        name
        for name in (*_ACTION_INPUTS, *_REQUIRED_RUNNER_VARIABLES)
        if name not in environment or not isinstance(environment[name], str)
    ]
    if missing:
        raise ActionInputError(
            "missing action environment variable(s): %s" % ", ".join(missing)
        )
    for name in _REQUIRED_RUNNER_VARIABLES:
        if not environment[name]:
            raise ActionInputError("%s must not be empty" % name)


def _resolve_workspace_path(workspace: Path, raw: str, label: str) -> Path:
    components = [] if raw == "." and label == "source" else raw.split("/")
    invalid = (
        not raw
        or "\\" in raw
        or PurePosixPath(raw).is_absolute()
        or bool(PureWindowsPath(raw).drive)
        or any(part in {"", ".", ".."} for part in components)
        or any(
            _UNSAFE_PATH_COMPONENT.search(part)
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
            for part in components
        )
    )
    if invalid:
        raise ActionInputError(
            "%s must be a portable repository-relative path and must not "
            "escape the workspace" % label
        )

    candidate = workspace
    for part in components:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ActionInputError("%s path must not contain symbolic links" % label)
    try:
        resolved = candidate.resolve(strict=label in {"source", "config"})
        resolved.relative_to(workspace)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ActionInputError(
            "%s must resolve inside GITHUB_WORKSPACE" % label
        ) from exc
    if label == "source" and not resolved.is_dir():
        raise ActionInputError(
            "source must resolve to a directory inside GITHUB_WORKSPACE"
        )
    if label == "config":
        if not resolved.is_file():
            raise ActionInputError(
                "config must resolve to a readable regular file inside "
                "GITHUB_WORKSPACE"
            )
        try:
            with resolved.open("rb"):
                pass
        except OSError as exc:
            raise ActionInputError(
                "config must resolve to a readable regular file inside "
                "GITHUB_WORKSPACE"
            ) from exc
    return resolved


def _boolean_input(name: str, value: str) -> bool:
    if value not in {"true", "false"}:
        raise ActionInputError("%s must be true or false" % name)
    return value == "true"


def _append_repeated(arguments: list[str], option: str, raw: str) -> None:
    for value in raw.split("\n"):
        if value.endswith("\r"):
            value = value[:-1]
        if value:
            arguments.extend((option, value))


def _default_output_path(environment: Mapping[str, str], pid: int) -> Path:
    runner_temp = environment.get("RUNNER_TEMP", "")
    if not runner_temp:
        raise ActionInputError("RUNNER_TEMP is required when output is omitted")
    run_id = environment.get("GITHUB_RUN_ID") or "0"
    attempt = environment.get("GITHUB_RUN_ATTEMPT") or "0"
    return Path(runner_temp) / (
        "repomin-result-%s-%s-%d" % (run_id, attempt, pid)
    )


def _prepare_action(
    environment: Mapping[str, str], *, pid: Optional[int] = None
) -> ActionPlan:
    _require_environment(environment)
    workspace = Path(environment["GITHUB_WORKSPACE"]).resolve()
    source_path = _resolve_workspace_path(
        workspace, environment["REPOMIN_SOURCE"], "source"
    )
    raw_output = environment["REPOMIN_OUTPUT"]
    output_path = (
        _resolve_workspace_path(workspace, raw_output, "output")
        if raw_output
        else _default_output_path(environment, os.getpid() if pid is None else pid)
    )
    step_summary = _boolean_input(
        "step-summary", environment["REPOMIN_STEP_SUMMARY"]
    )
    arguments = [str(source_path), "--output", str(output_path)]

    config = environment["REPOMIN_CONFIG"]
    if config:
        conflicts = [
            name
            for name, variable, default in _CONFIG_SEMANTIC_DEFAULTS
            if environment[variable] != default
        ]
        if conflicts:
            raise ActionInputError(
                "config cannot be combined with non-default semantic inputs: %s"
                % " ".join(conflicts)
            )
        config_path = _resolve_workspace_path(workspace, config, "config")
        arguments.extend(("--config", str(config_path)))
    else:
        command = environment["REPOMIN_COMMAND"]
        if not command.strip():
            raise ActionInputError("command is required when config is omitted")
        if (
            environment["REPOMIN_BACKEND"] == "docker"
            and not environment["REPOMIN_DOCKER_IMAGE"]
        ):
            raise ActionInputError(
                "docker-image is required when backend is docker"
            )

        signatures = {
            name: _boolean_input(name, environment[variable])
            for name, variable, _option in _SIGNATURE_INPUTS
        }
        gitignore = _boolean_input("gitignore", environment["REPOMIN_GITIGNORE"])
        gitignore_recursive = _boolean_input(
            "gitignore-recursive", environment["REPOMIN_GITIGNORE_RECURSIVE"]
        )
        if sum(signatures.values()) > 1:
            raise ActionInputError(
                "only one of java-exception, python-exception, and "
                "process-failure may be true"
            )
        if (
            not environment["REPOMIN_MATCH"]
            and not environment["REPOMIN_EXIT_CODE"]
            and not signatures["process-failure"]
        ):
            raise ActionInputError(
                "set match, exit-code, or process-failure so the failure "
                "oracle is explicit"
            )
        if environment["REPOMIN_EXIT_CODE"] and signatures["process-failure"]:
            raise ActionInputError(
                "exit-code cannot be combined with process-failure"
            )

        arguments.extend(
            (
                "--command",
                command,
                "--adapter",
                environment["REPOMIN_ADAPTER"],
                "--source-reducer",
                environment["REPOMIN_SOURCE_REDUCER"],
                "--backend",
                environment["REPOMIN_BACKEND"],
                "--docker-network",
                environment["REPOMIN_DOCKER_NETWORK"],
                "--timeout",
                environment["REPOMIN_TIMEOUT"],
                "--jobs",
                environment["REPOMIN_JOBS"],
            )
        )
        for option, variable in (
            ("--match", "REPOMIN_MATCH"),
            ("--exit-code", "REPOMIN_EXIT_CODE"),
        ):
            if environment[variable]:
                arguments.extend((option, environment[variable]))
        for name, _variable, option in _SIGNATURE_INPUTS:
            if signatures[name]:
                arguments.append(option)
        for option, variable in _OPTIONAL_VALUE_OPTIONS:
            if environment[variable]:
                arguments.extend((option, environment[variable]))
        for option, variable in _REPEATED_OPTIONS:
            _append_repeated(arguments, option, environment[variable])
        for option, variable in (
            ("--max-attempts", "REPOMIN_MAX_ATTEMPTS"),
            ("--max-duration", "REPOMIN_MAX_DURATION"),
        ):
            if environment[variable]:
                arguments.extend((option, environment[variable]))
        if gitignore:
            arguments.append("--gitignore")
        if gitignore_recursive:
            arguments.append("--gitignore-recursive")
        if environment["REPOMIN_BACKEND"] == "docker":
            arguments.extend(
                ("--docker-image", environment["REPOMIN_DOCKER_IMAGE"])
            )

    return ActionPlan(
        arguments=tuple(arguments),
        payload_path=output_path,
        report_path=Path(str(output_path) + ".repomin") / "report.json",
        step_summary=step_summary,
    )


def _render_summary_outputs(summary: Mapping[str, object]) -> Sequence[Tuple[str, str]]:
    outputs = []
    for output_name, summary_name, required in _SUMMARY_OUTPUTS:
        value = summary.get(summary_name)
        if required and value is None:
            raise ActionRuntimeError(
                "generated report is missing an action output field"
            )
        if isinstance(value, (dict, list)):
            raise ActionRuntimeError(
                "generated report action output is not scalar: %s" % output_name
            )
        if isinstance(value, bool):
            rendered = str(value).lower()
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)
        outputs.append((output_name, rendered))
    return outputs


def _append_utf8(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _write_step_summary(
    environment: Mapping[str, str], summary: Mapping[str, object]
) -> str:
    summary_path = environment.get("GITHUB_STEP_SUMMARY", "")
    if not summary_path:
        print(
            "step-summary requested but GITHUB_STEP_SUMMARY is unavailable; "
            "skipping",
            file=sys.stderr,
        )
        return ""

    server_url = environment.get("GITHUB_SERVER_URL", "")
    repository = environment.get("GITHUB_REPOSITORY", "")
    run_id = environment.get("GITHUB_RUN_ID", "")
    if (
        _SERVER_URL.fullmatch(server_url)
        and _REPOSITORY.fullmatch(repository)
        and _RUN_ID.fullmatch(run_id)
    ):
        artifact_note = (
            "[Open the workflow run to download the artifact]"
            "(%s/%s/actions/runs/%s).\n" % (server_url, repository, run_id)
        )
    else:
        artifact_note = (
            "The minimized payload and report are prepared for upload as the "
            "configured artifact.\n"
        )
    _append_utf8(
        Path(summary_path),
        format_validation_markdown(dict(summary))
        + "\n## ReproMin artifact\n\n"
        + artifact_note,
    )
    return summary_path


def _write_action_outputs(
    environment: Mapping[str, str],
    plan: ActionPlan,
    summary_path: str,
    summary: Mapping[str, object],
) -> None:
    outputs = [
        ("payload-path", str(plan.payload_path)),
        ("report-path", str(plan.report_path)),
        ("metadata-path", str(plan.report_path.parent)),
        ("step-summary-path", summary_path),
        *_render_summary_outputs(summary),
    ]
    _append_utf8(
        Path(environment["GITHUB_OUTPUT"]),
        "".join("%s=%s\n" % item for item in outputs),
    )


def run_action(environment: Mapping[str, str]) -> int:
    try:
        plan = _prepare_action(environment)
    except ActionInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "repomin", *plan.arguments],
            check=False,
            cwd=str(Path(__file__).resolve().parent),
        )
    except OSError as exc:
        print("repomin action: could not start ReproMin: %s" % exc, file=sys.stderr)
        return 1
    if completed.returncode != 0:
        return (
            128 - completed.returncode
            if completed.returncode < 0
            else completed.returncode
        )
    if not plan.payload_path.is_dir() or not plan.report_path.is_file():
        print(
            "ReproMin did not produce the expected payload and report paths",
            file=sys.stderr,
        )
        return 1

    try:
        report = validate_report_file(plan.report_path, plan.payload_path)
    except (ReportValidationError, ValueError, OSError) as exc:
        print("repomin report: %s" % exc, file=sys.stderr)
        return 2

    try:
        summary = _validation_summary(
            report, plan.report_path, plan.payload_path
        )
        summary_path = (
            _write_step_summary(environment, summary) if plan.step_summary else ""
        )
        _write_action_outputs(environment, plan, summary_path, summary)
    except (ActionRuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return run_action(os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
