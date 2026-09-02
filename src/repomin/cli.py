from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence, Tuple

from repomin import __version__
from repomin.execution import CommandRunner, DockerRunner, Runner, RunnerError
from repomin.cargo_manifest import CargoManifestReducer
from repomin.composer_manifest import ComposerManifestReducer
from repomin.completion import SUPPORTED_SHELLS, completion_script
from repomin.dotnet_manifest import DotnetManifestReducer
from repomin.doctor import format_doctor, run_doctor
from repomin.go_manifest import GoManifestReducer
from repomin.gradle import GradleReducer
from repomin.gitignore import GitignoreMatcher, load_gitignore
from repomin.java import (
    JavaAnalysisClasspathEntry,
    JavaReducer,
    JavaReducerError,
    prepare_java_analysis_classpath,
)
from repomin.maven import MavenReducer
from repomin.model import (
    CANDIDATE_FAMILY_CONTROL_POLICY,
    CANDIDATE_SAMPLING_POLICY,
    FailureSpec,
    HOLDOUT_CERTIFICATION_POLICY,
    REDUCTION_STRATEGY,
    ReductionResult,
    ReductionStats,
)
from repomin.node_manifest import NodeManifestReducer
from repomin.oracle import (
    FailureOracle,
    OracleError,
    candidate_family_confidence,
    clopper_pearson_lower_bound,
    exact_binomial_rate_gate,
)
from repomin.pipenv_manifest import PipenvManifestReducer
from repomin.python_manifest import PythonManifestReducer
from repomin.python_source import PythonSourceReducer
from repomin.reducer import FileReducer
from repomin.ruby_manifest import RubyManifestReducer
from repomin.report import (
    ReportValidationError,
    _payload_fingerprint_evidence,
    measure_tree,
    validate_report_file,
    verify_existing_report,
    write_report,
)
from repomin.report_compare import (
    ReportComparisonError,
    compare_reports,
    render_comparison_markdown,
    render_comparison_text,
)
from repomin.replay import ReplayError, format_replay, replay_report
from repomin.semantic import (
    HttpSemanticBackend,
    NoopSemanticBackend,
    SemanticReducer,
)
from repomin.signature import format_process_failure_signature
from repomin.text_reducer import TextReducer
from repomin.session import (
    DEFAULT_IGNORES,
    HoldoutCertificationError,
    ReductionSession,
    SessionError,
    _validate_repository_entries,
)


DEFAULT_DOCKER_PIDS_LIMIT = 512
DEFAULT_DOCKER_TMPFS_BYTES = 1024 * 1024 * 1024
HOST_WORKING_DIRECTORY_POLICY = "host-output-basename-v1"
DOCKER_WORKING_DIRECTORY_POLICY = "docker-workspace-v1"
_BYTE_SIZE = re.compile(r"^(?P<number>[1-9][0-9]*)(?P<suffix>[kmgt]i?b?|b)?$")
_BYTE_MULTIPLIERS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "ki": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "ti": 1024**4,
    "tib": 1024**4,
}
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_byte_size(value: str) -> int:
    normalized = value.strip().lower()
    match = _BYTE_SIZE.fullmatch(normalized)
    if match is None:
        raise argparse.ArgumentTypeError(
            "size must be a positive integer with an optional KiB/MiB/GiB suffix"
        )
    suffix = match.group("suffix") or ""
    return int(match.group("number")) * _BYTE_MULTIPLIERS[suffix]


def _parse_rate(value: str) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("rate must be a number in (0, 1)") from exc
    if not math.isfinite(rate) or rate <= 0.0 or rate >= 1.0:
        raise argparse.ArgumentTypeError(
            "rate must be in (0, 1); use an all-runs pass threshold for strict mode"
        )
    return rate


def _parse_confidence(value: str) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("confidence must be a number in (0, 1)") from exc
    if not math.isfinite(confidence) or confidence <= 0.0 or confidence >= 1.0:
        raise argparse.ArgumentTypeError("confidence must be in (0, 1)")
    return confidence


def _parse_ignore_name(value: str) -> str:
    """Validate one recursively ignored repository basename."""
    if not isinstance(value, str):
        raise argparse.ArgumentTypeError("ignore name must be text")
    name = value.strip()
    parsed = Path(name)
    if (
        not name
        or name in {".", ".."}
        or "\x00" in name
        or "/" in name
        or "\\" in name
        or parsed.is_absolute()
        or len(parsed.parts) != 1
        or parsed.name != name
    ):
        raise argparse.ArgumentTypeError(
            "ignore name must be one ordinary file or directory basename"
        )
    return name


def _parse_ignore_path(value: str) -> str:
    """Validate one exact repository-relative path exclusion."""
    if not isinstance(value, str):
        raise argparse.ArgumentTypeError("ignore path must be text")
    path = value.strip()
    if (
        not path
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(char in path for char in "*?[")
    ):
        raise argparse.ArgumentTypeError(
            "ignore path must be an exact relative path without glob syntax"
        )
    normalized = PurePosixPath(path).as_posix()
    if normalized in {"", "."}:
        raise argparse.ArgumentTypeError("ignore path must not be the repository root")
    return normalized


def _parse_text_file_path(value: str) -> str:
    """Validate one exact repository-relative text file for the text reducer."""
    if not isinstance(value, str):
        raise argparse.ArgumentTypeError("text file path must be text")
    path = value.strip()
    if (
        not path
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(char in path for char in "*?[")
    ):
        raise argparse.ArgumentTypeError(
            "text file path must be an exact relative path without glob syntax"
        )
    return PurePosixPath(path).as_posix()


def _load_gitignore(
    source: Path,
    enabled: bool,
    files: Sequence[str],
    recursive: bool = False,
    ignore_names: Sequence[str] = (),
    ignore_paths: Sequence[str] = (),
) -> Tuple[Optional[GitignoreMatcher], Sequence[str], Optional[str], bool]:
    """Compatibility wrapper for the shared gitignore loader."""
    return load_gitignore(
        source,
        enabled,
        files,
        recursive=recursive,
        ignore_names=ignore_names,
        ignore_paths=ignore_paths,
        default_ignores=DEFAULT_IGNORES,
    )


def _parse_environment(value: str) -> Tuple[str, str]:
    """Parse one explicit NAME=VALUE environment override."""
    if not isinstance(value, str) or "=" not in value:
        raise argparse.ArgumentTypeError("environment must use NAME=VALUE")
    name, environment_value = value.split("=", 1)
    if _ENVIRONMENT_NAME.fullmatch(name) is None:
        raise argparse.ArgumentTypeError(
            "environment name must match [A-Za-z_][A-Za-z0-9_]*"
        )
    if name == "REPOMIN":
        raise argparse.ArgumentTypeError("REPOMIN is reserved by ReproMin")
    if "\x00" in environment_value:
        raise argparse.ArgumentTypeError("environment value must not contain NUL")
    return name, environment_value


def _environment_mapping(entries: Sequence[Tuple[str, str]]) -> dict:
    values = {}
    normalized_names = set()
    for name, value in entries:
        if name in values:
            raise ValueError("environment variable specified more than once: %s" % name)
        normalized = name.casefold() if os.name == "nt" else name
        if normalized in normalized_names:
            raise ValueError(
                "environment variable names must be unambiguous on this platform: %s"
                % name
            )
        normalized_names.add(normalized)
        values[name] = value
    return values


def _environment_digest(environment: dict) -> str:
    encoded = "".join(
        "%s=%s\0" % (name, environment[name])
        for name in sorted(environment)
    ).encode("utf-8", errors="surrogateescape")
    return hashlib.sha256(encoded).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repomin",
        description="Reduce a repository while preserving a command failure.",
        epilog=(
            "Preflight with `repomin doctor --help`; inspect evidence with "
            "`repomin report --help`. Generate shell completion with "
            "`repomin completion bash`, "
            "`repomin completion zsh`, `repomin completion fish`, or "
            "`repomin completion powershell`."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="repomin %s" % __version__,
    )
    parser.add_argument("source", nargs="?", default=".", help="repository to reduce")
    parser.add_argument("--command", required=True, help="failure reproduction command")
    parser.add_argument(
        "--match",
        help=(
            "regular expression that must remain present in command output "
            "(required unless --process-failure or --exit-code is enabled)"
        ),
    )
    parser.add_argument(
        "--exit-code",
        type=int,
        help="required exit code; by default any non-zero exit is accepted",
    )
    parser.add_argument("--output", help="output directory (default: SOURCE-minimal)")
    parser.add_argument(
        "--session",
        help="persistent reduction session directory for checkpointing",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume an existing --session instead of starting a new reduction",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds per run")
    parser.add_argument(
        "--backend",
        choices=("host", "docker"),
        default="host",
        help="command execution backend (default: host)",
    )
    parser.add_argument(
        "--docker-image",
        help="local image used by the Docker backend",
    )
    parser.add_argument(
        "--docker-network",
        choices=("none", "bridge", "host"),
        default="none",
        help="Docker network policy (default: none)",
    )
    parser.add_argument(
        "--docker-cpus",
        type=float,
        help="Docker CPU quota in cores",
    )
    parser.add_argument(
        "--docker-memory",
        type=_parse_byte_size,
        metavar="SIZE",
        help="Docker memory and swap limit (for example: 2GiB)",
    )
    parser.add_argument(
        "--docker-pids-limit",
        type=int,
        default=DEFAULT_DOCKER_PIDS_LIMIT,
        help="maximum container processes (default: 512)",
    )
    parser.add_argument(
        "--docker-tmpfs-size",
        type=_parse_byte_size,
        default=DEFAULT_DOCKER_TMPFS_BYTES,
        metavar="SIZE",
        help="size of the container /tmp filesystem (default: 1GiB)",
    )
    parser.add_argument(
        "--docker-workspace-limit",
        type=_parse_byte_size,
        metavar="SIZE",
        help="maximum total size of the writable candidate workspace",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="candidate commands to run concurrently (default: 1)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="disable session-local content-addressed result caching",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        metavar="N",
        help="stop the reduction after N logical candidate attempts",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        metavar="SECONDS",
        help="stop the reduction after this many wall-clock seconds",
    )
    parser.add_argument(
        "--ignore",
        dest="ignore_names",
        action="append",
        type=_parse_ignore_name,
        default=[],
        metavar="NAME",
        help=(
            "recursively ignore an additional exact file or directory basename; "
            "repeat for multiple names"
        ),
    )
    parser.add_argument(
        "--ignore-path",
        dest="ignore_paths",
        action="append",
        type=_parse_ignore_path,
        default=[],
        metavar="RELATIVE_PATH",
        help=(
            "recursively ignore one exact repository-relative path; repeat for "
            "multiple paths (glob syntax is not accepted)"
        ),
    )
    parser.add_argument(
        "--gitignore",
        action="store_true",
        help="apply the repository .gitignore as additional exclusions",
    )
    parser.add_argument(
        "--gitignore-file",
        dest="gitignore_files",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "apply one explicit gitignore-style file; repeat for multiple files "
            "(relative paths are resolved against the repository)"
        ),
    )
    parser.add_argument(
        "--gitignore-recursive",
        action="store_true",
        help=(
            "apply the repository .gitignore and nested .gitignore files in "
            "their respective directories"
        ),
    )
    parser.add_argument(
        "--keep",
        dest="keep_paths",
        action="append",
        type=_parse_ignore_path,
        default=[],
        metavar="RELATIVE_PATH",
        help=(
            "protect one exact repository-relative file or directory from the "
            "file reducer; repeat for multiple paths (the path itself is kept)"
        ),
    )
    parser.add_argument(
        "--env",
        dest="environment_entries",
        action="append",
        type=_parse_environment,
        default=[],
        metavar="NAME=VALUE",
        help=(
            "set an explicit reproduction environment variable; repeat for "
            "multiple variables (values are omitted from reports)"
        ),
    )
    signature_group = parser.add_mutually_exclusive_group()
    signature_group.add_argument(
        "--java-exception",
        action="store_true",
        help="learn and preserve a normalized Java exception signature",
    )
    signature_group.add_argument(
        "--python-exception",
        action="store_true",
        help="learn and preserve a normalized Python exception signature",
    )
    signature_group.add_argument(
        "--process-failure",
        action="store_true",
        help="learn and preserve the exact process termination signature",
    )
    parser.add_argument(
        "--baseline-runs",
        type=int,
        default=2,
        help="baseline samples collected before reduction (default: 2)",
    )
    parser.add_argument(
        "--min-baseline-passes",
        type=int,
        help="minimum passing baseline samples (default: all baseline runs)",
    )
    parser.add_argument(
        "--candidate-runs",
        type=int,
        default=1,
        help="independent oracle runs for each candidate (default: 1)",
    )
    parser.add_argument(
        "--min-candidate-passes",
        type=int,
        help="minimum passing candidate samples (default: all candidate runs)",
    )
    parser.add_argument(
        "--min-baseline-rate",
        type=_parse_rate,
        metavar="RATE",
        help=(
            "minimum exact one-sided baseline failure rate in (0, 1); "
            "when set without --min-baseline-passes, the count minimum is 1"
        ),
    )
    parser.add_argument(
        "--min-candidate-rate",
        type=_parse_rate,
        metavar="RATE",
        help=(
            "minimum exact one-sided candidate failure rate in (0, 1); "
            "early acceptance also requires an anytime-valid bound"
        ),
    )
    parser.add_argument(
        "--confidence",
        type=_parse_confidence,
        default=0.95,
        metavar="LEVEL",
        help=(
            "confidence level for exact rate gates, anytime-valid bounds, and "
            "descriptive Wilson bounds (default: 0.95)"
        ),
    )
    parser.add_argument(
        "--run-confidence",
        type=_parse_confidence,
        metavar="LEVEL",
        help=(
            "run-wide confidence across adaptively selected candidate families; "
            "requires --min-candidate-rate"
        ),
    )
    parser.add_argument(
        "--holdout-runs",
        type=int,
        metavar="N",
        help="fresh fixed-size samples used only for final certification",
    )
    parser.add_argument(
        "--min-holdout-rate",
        type=_parse_rate,
        metavar="RATE",
        help="minimum exact one-sided lower bound certified by the holdout",
    )
    parser.add_argument(
        "--holdout-confidence",
        type=_parse_confidence,
        metavar="LEVEL",
        help="holdout confidence level (default when enabled: 0.95)",
    )
    parser.add_argument(
        "--adapter",
        choices=(
            "auto",
            "none",
            "maven",
            "gradle",
            "python",
            "pipenv",
            "node",
            "composer",
            "dotnet",
            "ruby",
            "cargo",
            "go",
        ),
        default="auto",
        help="structured manifest reducer",
    )
    parser.add_argument(
        "--source-reducer",
        choices=("auto", "none", "java", "python"),
        default="auto",
        help="source-level reducer",
    )
    parser.add_argument(
        "--text-file",
        dest="text_files",
        action="append",
        type=_parse_text_file_path,
        default=[],
        metavar="RELATIVE_PATH",
        help=(
            "line-reduce one exact repository-relative UTF-8 text file; "
            "repeat for multiple files"
        ),
    )
    parser.add_argument(
        "--semantic-reducer",
        choices=("none", "http"),
        default=os.environ.get("REPOMIN_SEMANTIC_REDUCER", "none"),
        help=(
            "opt-in semantic reducer backend (default: none); http uses an "
            "OpenAI-compatible chat-completions endpoint"
        ),
    )
    parser.add_argument(
        "--semantic-endpoint",
        default=os.environ.get("REPOMIN_SEMANTIC_ENDPOINT"),
        metavar="URL",
        help=(
            "OpenAI-compatible /v1/chat/completions endpoint for "
            "--semantic-reducer http (or REPOMIN_SEMANTIC_ENDPOINT)"
        ),
    )
    parser.add_argument(
        "--semantic-model",
        default=os.environ.get("REPOMIN_SEMANTIC_MODEL"),
        metavar="NAME",
        help=(
            "model name for --semantic-reducer http "
            "(or REPOMIN_SEMANTIC_MODEL)"
        ),
    )
    parser.add_argument(
        "--semantic-timeout",
        type=float,
        default=os.environ.get("REPOMIN_SEMANTIC_TIMEOUT", "60"),
        metavar="SECONDS",
        help=(
            "HTTP timeout for --semantic-reducer http "
            "(or REPOMIN_SEMANTIC_TIMEOUT; default: 60)"
        ),
    )
    parser.add_argument(
        "--java-classpath",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "host path for Java AST attribution; repeat for multiple entries "
            "(does not change the reproduction command or Docker mounts)"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "doctor":
        return _doctor_command(raw_argv[1:])
    if raw_argv and raw_argv[0] == "report":
        return _report_command(raw_argv[1:])
    if raw_argv and raw_argv[0] == "completion":
        if len(raw_argv) == 2 and raw_argv[1] in SUPPORTED_SHELLS:
            print(completion_script(raw_argv[1]), end="")
            return 0
        if raw_argv[1:] in ([], ["--help"], ["-h"]):
            print("usage: repomin completion {%s}" % ",".join(SUPPORTED_SHELLS))
            print("print a completion script for the selected shell")
            return 0
        if len(raw_argv) == 2:
            print(
                "unsupported shell %r (choose one of: %s)"
                % (raw_argv[1], ", ".join(SUPPORTED_SHELLS)),
                file=sys.stderr,
            )
        else:
            print(
                "usage: repomin completion {%s}" % ",".join(SUPPORTED_SHELLS),
                file=sys.stderr,
            )
        return 2
    args = build_parser().parse_args(raw_argv)
    session_path: Optional[Path] = None
    session: Optional[ReductionSession] = None
    try:
        environment = _environment_mapping(args.environment_entries)
        if (
            args.match is None
            and args.exit_code is None
            and not args.process_failure
        ):
            raise ValueError(
                "--match is required unless --process-failure or --exit-code "
                "is enabled"
            )
        if args.process_failure and args.exit_code is not None:
            raise ValueError("--process-failure cannot be combined with --exit-code")
        source, output = _resolve_paths(
            args.source,
            args.output,
            allow_existing_output=args.resume,
        )
        (
            working_directory_policy,
            working_directory_basename,
            execution_working_directory_basename,
        ) = _working_directory_configuration(output, args.backend)
        _validate_repository_entries(source, DEFAULT_IGNORES)
        (
            gitignore_matcher,
            gitignore_files,
            gitignore_sha256,
            gitignore_recursive,
        ) = _load_gitignore(
            source,
            args.gitignore,
            args.gitignore_files,
            recursive=args.gitignore_recursive,
            ignore_names=args.ignore_names,
            ignore_paths=args.ignore_paths,
        )
        _validate_keep_paths(source, args.keep_paths)
        metadata_output = _metadata_output_path(output)
        _reject_symbolic_link(metadata_output, "metadata output")
        if metadata_output.exists() and not args.resume:
            raise FileExistsError(
                "metadata output already exists: %s" % metadata_output
            )
        if args.resume and metadata_output.exists() and not output.exists():
            raise FileExistsError(
                "metadata output exists without its payload: %s" % metadata_output
            )
        java_analysis_classpath = prepare_java_analysis_classpath(
            source, args.java_classpath
        )
        session_path = _resolve_session_path(
            source,
            output,
            metadata_output,
            args.session,
            args.resume,
        )
        baseline_min_passes = _sample_threshold(
            args.baseline_runs,
            args.min_baseline_passes,
            "baseline",
            args.min_baseline_rate,
        )
        candidate_min_passes = _sample_threshold(
            args.candidate_runs,
            args.min_candidate_passes,
            "candidate",
            args.min_candidate_rate,
        )
        holdout_confidence = _holdout_configuration(args)
        semantic_reducer = args.semantic_reducer
        semantic_endpoint = args.semantic_endpoint
        semantic_model = args.semantic_model
        if semantic_reducer == "http":
            if not semantic_endpoint:
                raise ValueError(
                    "--semantic-reducer http requires --semantic-endpoint"
                )
            if not semantic_model:
                raise ValueError(
                    "--semantic-reducer http requires --semantic-model"
                )
            if (
                isinstance(args.semantic_timeout, bool)
                or not math.isfinite(float(args.semantic_timeout))
                or float(args.semantic_timeout) <= 0.0
            ):
                raise ValueError(
                    "--semantic-timeout must be a positive number of seconds"
                )
        if args.run_confidence is not None and args.min_candidate_rate is None:
            raise ValueError("--run-confidence requires --min-candidate-rate")
        _validate_rate_attainable(
            args.baseline_runs,
            args.min_baseline_rate,
            args.confidence,
            "baseline",
            signature_discovery=(
                args.java_exception or args.python_exception or args.process_failure
            ),
        )
        if args.run_confidence is not None:
            first_candidate_confidence, _alpha = candidate_family_confidence(
                args.confidence,
                args.run_confidence,
                1,
            )
            _validate_rate_attainable(
                args.candidate_runs,
                args.min_candidate_rate,
                first_candidate_confidence,
                "candidate family 1",
            )
        _validate_rate_attainable(
            args.candidate_runs,
            args.min_candidate_rate,
            args.confidence,
            "candidate",
        )
        if args.holdout_runs is not None:
            assert args.min_holdout_rate is not None
            _validate_holdout_rate_attainable(
                args.holdout_runs,
                args.min_holdout_rate,
                holdout_confidence,
            )
        runner = _build_runner(args)
        docker_image_id = (
            getattr(runner, "resolved_image_id", None)
            if args.backend == "docker"
            else None
        )
        stats = ReductionStats(
            source_files=0,
            source_bytes=0,
            jobs=args.jobs,
            cache_enabled=not args.no_cache,
            backend=args.backend,
            working_directory_policy=working_directory_policy,
            working_directory_basename=working_directory_basename,
            container_image=args.docker_image if args.backend == "docker" else None,
            container_image_id=docker_image_id,
            container_network=(
                args.docker_network if args.backend == "docker" else None
            ),
            container_cpus=args.docker_cpus if args.backend == "docker" else None,
            container_memory_bytes=(
                args.docker_memory if args.backend == "docker" else None
            ),
            container_pids_limit=(
                args.docker_pids_limit if args.backend == "docker" else None
            ),
            container_tmpfs_bytes=(
                args.docker_tmpfs_size if args.backend == "docker" else None
            ),
            container_workspace_limit_bytes=(
                args.docker_workspace_limit if args.backend == "docker" else None
            ),
            min_baseline_rate=args.min_baseline_rate,
            min_candidate_rate=args.min_candidate_rate,
            confidence=args.confidence,
            run_confidence=args.run_confidence,
            candidate_family_control_policy=(
                CANDIDATE_FAMILY_CONTROL_POLICY
                if args.run_confidence is not None
                else None
            ),
            reduction_strategy=REDUCTION_STRATEGY,
            environment_names=sorted(environment),
            environment_sha256=_environment_digest(environment),
            ignored_paths=sorted(set(args.ignore_paths)),
            gitignore_files=list(gitignore_files),
            gitignore_sha256=gitignore_sha256,
            gitignore_recursive=gitignore_recursive,
            keep_paths=sorted(set(args.keep_paths)),
            text_files=sorted(set(args.text_files)),
            max_attempts=args.max_attempts,
            max_duration_seconds=args.max_duration,
            semantic_reducer=semantic_reducer,
            semantic_model=semantic_model if semantic_reducer == "http" else None,
            semantic_endpoint=(
                semantic_endpoint if semantic_reducer == "http" else None
            ),
        )
        oracle = FailureOracle(
            runner,
            FailureSpec(
                args.match,
                args.exit_code,
                java_exception=args.java_exception,
                python_exception=args.python_exception,
                process_failure=args.process_failure,
            ),
            min_baseline_rate=args.min_baseline_rate,
            min_candidate_rate=args.min_candidate_rate,
            confidence=args.confidence,
        )
        progress = (lambda message: print(message, file=sys.stderr)) if args.verbose else None
        session = ReductionSession(
            source,
            oracle,
            stats,
            progress=progress,
            ignores=args.ignore_names,
            ignore_paths=args.ignore_paths,
            gitignore_matcher=gitignore_matcher,
            gitignore_files=gitignore_files,
            gitignore_recursive=gitignore_recursive,
            keep_paths=args.keep_paths,
            max_attempts=args.max_attempts,
            max_duration_seconds=args.max_duration,
            jobs=args.jobs,
            cache_enabled=not args.no_cache,
            temporary_parent=source.parent if args.backend == "docker" else None,
            session_path=session_path,
            resume=args.resume,
            identity=_session_identity(
                args,
                baseline_min_passes,
                candidate_min_passes,
                java_analysis_classpath,
                holdout_confidence,
                docker_image_id,
                working_directory_policy,
                working_directory_basename,
                environment,
                args.ignore_paths,
                gitignore_files,
                gitignore_sha256,
                gitignore_recursive,
                args.keep_paths,
                args.max_attempts,
                args.max_duration,
                semantic_reducer,
                semantic_endpoint,
                semantic_model,
                args.text_files,
            ),
            execution_working_directory_basename=(
                execution_working_directory_basename
            ),
            candidate_runs=args.candidate_runs,
            candidate_min_passes=candidate_min_passes,
            candidate_min_rate=args.min_candidate_rate,
            run_confidence=args.run_confidence,
            holdout_runs=args.holdout_runs,
            holdout_minimum_rate=args.min_holdout_rate,
            holdout_confidence=holdout_confidence,
        )
        try:
            if (
                output.exists()
                and session.holdout_certification.status != "certified"
            ):
                raise FileExistsError("output already exists: %s" % output)
            stats = session.stats
            if not session.resumed:
                stats.source_files, stats.source_bytes = measure_tree(session.current)
            if session.baseline is None:
                print("Verifying baseline failure...", file=sys.stderr)
                baseline = session.verify_baseline(
                    args.baseline_runs,
                    minimum_passes=baseline_min_passes,
                    minimum_rate=args.min_baseline_rate,
                )
            else:
                baseline = session.baseline
                print("Resuming from saved baseline failure...", file=sys.stderr)
            if oracle.java_exception_signature is not None:
                signature = oracle.java_exception_signature
                print(
                    "Preserving Java exception: %s: %s"
                    % (signature.class_name, signature.message),
                    file=sys.stderr,
                )
            if oracle.python_exception_signature is not None:
                signature = oracle.python_exception_signature
                print(
                    "Preserving Python exception: %s: %s"
                    % (signature.class_name, signature.message),
                    file=sys.stderr,
                )
            if oracle.process_failure_signature is not None:
                print(
                    "Preserving process failure: %s"
                    % format_process_failure_signature(
                        oracle.process_failure_signature
                    ),
                    file=sys.stderr,
                )

            if not session.phase_completed("reduction-fixed-point"):
                maven = MavenReducer(session)
                cargo = CargoManifestReducer(session)
                composer = ComposerManifestReducer(session)
                dotnet = DotnetManifestReducer(session)
                ruby = RubyManifestReducer(session)
                go_manifest = GoManifestReducer(session)
                gradle = GradleReducer(session)
                python_manifest = PythonManifestReducer(session)
                pipenv = PipenvManifestReducer(session)
                node_manifest = NodeManifestReducer(session)
                java_reducer = JavaReducer(session, java_analysis_classpath)
                python_source_reducer = PythonSourceReducer(session)

                maven_applicable = maven.is_applicable()
                cargo_applicable = cargo.is_applicable()
                composer_applicable = composer.is_applicable()
                dotnet_applicable = dotnet.is_applicable()
                ruby_applicable = ruby.is_applicable()
                go_manifest_applicable = go_manifest.is_applicable()
                gradle_applicable = gradle.is_applicable()
                python_manifest_applicable = python_manifest.is_applicable()
                pipenv_applicable = pipenv.is_applicable()
                node_manifest_applicable = node_manifest.is_applicable()
                java_applicable = java_reducer.has_java_sources()
                python_source_applicable = python_source_reducer.has_python_sources()
                if args.adapter == "maven" and not maven_applicable:
                    raise ValueError("--adapter maven requires at least one pom.xml")
                if args.adapter == "gradle" and not gradle_applicable:
                    raise ValueError(
                        "--adapter gradle requires a Gradle build or properties file"
                    )
                if args.adapter == "python" and not python_manifest_applicable:
                    raise ValueError(
                        "--adapter python requires pyproject.toml or requirements files"
                    )
                if args.adapter == "pipenv" and not pipenv_applicable:
                    raise ValueError("--adapter pipenv requires at least one Pipfile")
                if args.adapter == "node" and not node_manifest_applicable:
                    raise ValueError(
                        "--adapter node requires at least one package.json"
                    )
                if args.adapter == "composer" and not composer_applicable:
                    raise ValueError(
                        "--adapter composer requires at least one composer.json"
                    )
                if args.adapter == "dotnet" and not dotnet_applicable:
                    raise ValueError(
                        "--adapter dotnet requires at least one .csproj, .fsproj, "
                        ".vbproj, or Directory.Build.props"
                    )
                if args.adapter == "ruby" and not ruby_applicable:
                    raise ValueError(
                        "--adapter ruby requires a Gemfile, gems.rb, or Gemfile.*"
                    )
                if args.adapter == "cargo" and not cargo_applicable:
                    raise ValueError("--adapter cargo requires at least one Cargo.toml")
                if args.adapter == "go" and not go_manifest_applicable:
                    raise ValueError(
                        "--adapter go requires at least one go.mod or go.work"
                    )
                if args.source_reducer == "java" and not java_applicable:
                    raise ValueError("--source-reducer java requires at least one .java file")
                if (
                    args.source_reducer == "java"
                    and not java_reducer.toolchain_available()
                ):
                    raise JavaReducerError(
                        "--source-reducer java requires JDK 11 or newer"
                    )
                if args.source_reducer == "python" and not python_source_applicable:
                    raise ValueError(
                        "--source-reducer python requires at least one .py file"
                    )

                use_maven = args.adapter == "maven" or (
                    args.adapter == "auto" and maven_applicable
                )
                use_gradle = args.adapter == "gradle" or (
                    args.adapter == "auto" and gradle_applicable
                )
                use_python_manifest = args.adapter == "python" or (
                    args.adapter == "auto" and python_manifest_applicable
                )
                use_pipenv = args.adapter == "pipenv" or (
                    args.adapter == "auto" and pipenv_applicable
                )
                use_node_manifest = args.adapter == "node" or (
                    args.adapter == "auto" and node_manifest_applicable
                )
                use_composer = args.adapter == "composer" or (
                    args.adapter == "auto" and composer_applicable
                )
                use_dotnet = args.adapter == "dotnet" or (
                    args.adapter == "auto" and dotnet_applicable
                )
                use_ruby = args.adapter == "ruby" or (
                    args.adapter == "auto" and ruby_applicable
                )
                use_cargo = args.adapter == "cargo" or (
                    args.adapter == "auto" and cargo_applicable
                )
                use_go_manifest = args.adapter == "go" or (
                    args.adapter == "auto" and go_manifest_applicable
                )
                use_java = args.source_reducer == "java" or (
                    args.source_reducer == "auto"
                    and java_applicable
                    and java_reducer.toolchain_available()
                )
                use_python_source = args.source_reducer == "python" or (
                    args.source_reducer == "auto" and python_source_applicable
                )

                print("Reducing to a global fixed point...", file=sys.stderr)
                session.begin_reduction()
                file_reducer = FileReducer(session)
                semantic_reducer_obj: Optional[SemanticReducer] = None
                if semantic_reducer == "http":
                    semantic_backend = HttpSemanticBackend(
                        semantic_endpoint,
                        semantic_model,
                        token=os.environ.get("REPOMIN_SEMANTIC_TOKEN"),
                        timeout=float(args.semantic_timeout),
                    )
                    semantic_reducer_obj = SemanticReducer(
                        session,
                        semantic_backend,
                    )
                else:
                    semantic_reducer_obj = SemanticReducer(
                        session,
                        NoopSemanticBackend(),
                    )
                components = []
                if use_maven:
                    components.append(("maven", maven.reduce))
                if use_gradle:
                    components.append(("gradle", gradle.reduce))
                if use_python_manifest:
                    components.append(("python-manifest", python_manifest.reduce))
                if use_pipenv:
                    components.append(("pipenv-manifest", pipenv.reduce))
                if use_node_manifest:
                    components.append(("node-manifest", node_manifest.reduce))
                if use_composer:
                    components.append(("composer-manifest", composer.reduce))
                if use_dotnet:
                    components.append(("dotnet-manifest", dotnet.reduce))
                if use_ruby:
                    components.append(("ruby-manifest", ruby.reduce))
                if use_cargo:
                    components.append(("cargo-manifest", cargo.reduce))
                if use_go_manifest:
                    components.append(("go-manifest", go_manifest.reduce))
                components.append(("files", file_reducer.reduce))
                if args.text_files:
                    text_reducer = TextReducer(session, args.text_files)
                    components.append(("text", text_reducer.reduce))
                if use_java:
                    components.append(
                        (
                            "java",
                            lambda: (
                                java_reducer.reduce()
                                if java_reducer.has_java_sources()
                                else None
                            ),
                        )
                    )
                if use_python_source:
                    components.append(
                        (
                            "python-source",
                            lambda: (
                                python_source_reducer.reduce()
                                if python_source_reducer.has_python_sources()
                                else None
                            ),
                        )
                    )
                components.append(("semantic", semantic_reducer_obj.reduce))

                _run_fixed_point(components, stats, session.progress)
                session.mark_phase_completed("reduction-fixed-point")

            if (
                args.holdout_runs is not None
                and session.final_validation_run is not None
            ):
                final_run = session.final_validation_run
                print(
                    "Resuming after saved final consistency validation...",
                    file=sys.stderr,
                )
            else:
                final_samples = session.run_current_repeated()
                final_accepted, final_passes = oracle.accepts_repeated(
                    final_samples,
                    candidate_min_passes,
                    minimum_rate=args.min_candidate_rate,
                    confidence=session.final_candidate_confidence,
                )
                stats.final_runs = len(final_samples)
                stats.final_passes = final_passes
                stats.final_rate = (
                    float(final_passes) / len(final_samples) if final_samples else 0.0
                )
                stats.final_lower_bound = oracle.candidate_lower_bound
                if not final_accepted:
                    raise OracleError(
                        "internal error: final candidate no longer reproduces failure"
                    )
                final_run = next(
                    sample for sample in final_samples if oracle.accepts(sample)
                )
                if args.holdout_runs is not None:
                    session.record_final_validation(final_run)

            holdout = session.run_holdout_certification()
            if holdout.status == "certified":
                print(
                    "Certified final holdout: %d/%d passes, exact lower bound %.4f."
                    % (
                        holdout.passes,
                        holdout.planned_runs,
                        holdout.exact_lower_bound,
                    ),
                    file=sys.stderr,
                )

            session.export(output)
            output_files, output_bytes = measure_tree(output)
            stats.output_files = output_files
            stats.output_bytes = output_bytes
            result = ReductionResult(
                output=output,
                stats=stats,
                baseline=baseline,
                final_run=final_run,
                java_exception_signature=oracle.java_exception_signature,
                python_exception_signature=oracle.python_exception_signature,
                holdout_certification=holdout,
                process_failure_signature=oracle.process_failure_signature,
            )
            if metadata_output.exists():
                verify_existing_report(
                    result,
                    args.command,
                    args.match,
                    metadata_output,
                    failure_spec=oracle.spec,
                    timeout_seconds=args.timeout,
                )
            else:
                write_report(
                    result,
                    args.command,
                    args.match,
                    metadata_output,
                    failure_spec=oracle.spec,
                    timeout_seconds=args.timeout,
                )
            session.mark_completed()
        finally:
            session.close()

        print(
            "Reduced %d files to %d files in %d attempts "
            "(%d accepted, %d cache hits)." % (
                stats.source_files,
                stats.output_files,
                stats.attempts,
                stats.accepted,
                stats.cache_hits,
            ),
            file=sys.stderr,
        )
        print(
            "Size: %d -> %d bytes." % (stats.source_bytes, stats.output_bytes),
            file=sys.stderr,
        )
        print(str(output))
        print("Metadata: %s" % metadata_output, file=sys.stderr)
        print("Report: %s" % (metadata_output / "report.json"), file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        if session_path is not None:
            print(
                "repomin: interrupted; checkpoint retained at %s (resume with --resume)"
                % session_path,
                file=sys.stderr,
            )
        else:
            print("repomin: interrupted", file=sys.stderr)
        return 130
    except HoldoutCertificationError as exc:
        print("repomin: %s" % exc, file=sys.stderr)
        return 3
    except (
        FileExistsError,
        JavaReducerError,
        NotADirectoryError,
        OSError,
        OracleError,
        RunnerError,
        SessionError,
        ValueError,
    ) as exc:
        print("repomin: %s" % exc, file=sys.stderr)
        return 2


def _doctor_command(argv: Sequence[str]) -> int:
    """Run read-only checks before starting a reduction."""
    parser = argparse.ArgumentParser(
        prog="repomin doctor",
        description=(
            "Check a repository, reducer/toolchain selection, and an optional "
            "failure baseline without exporting mutations."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="repomin %s" % __version__,
    )
    parser.add_argument("source", nargs="?", default=".", help="repository to inspect")
    parser.add_argument(
        "--command",
        help="optional failure reproduction command to run in fresh copies",
    )
    parser.add_argument("--match", help="regular expression required in command output")
    parser.add_argument(
        "--exit-code",
        type=int,
        help="exact process exit code required by the failure oracle",
    )
    signature_group = parser.add_mutually_exclusive_group()
    signature_group.add_argument(
        "--java-exception",
        action="store_true",
        help="learn and preserve a normalized Java exception signature",
    )
    signature_group.add_argument(
        "--python-exception",
        action="store_true",
        help="learn and preserve a normalized Python exception signature",
    )
    signature_group.add_argument(
        "--process-failure",
        action="store_true",
        help="learn and preserve the exact process termination signature",
    )
    parser.add_argument(
        "--adapter",
        choices=(
            "auto",
            "none",
            "maven",
            "gradle",
            "python",
            "pipenv",
            "node",
            "composer",
            "dotnet",
            "ruby",
            "cargo",
            "go",
        ),
        default="auto",
        help="structured manifest reducer to check",
    )
    parser.add_argument(
        "--source-reducer",
        choices=("auto", "none", "java", "python"),
        default="auto",
        help="source reducer to check",
    )
    parser.add_argument(
        "--backend",
        choices=("host", "docker"),
        default="host",
        help="backend used for the optional baseline (default: host)",
    )
    parser.add_argument("--docker-image", help="local Docker image for the baseline")
    parser.add_argument(
        "--docker-network",
        choices=("none", "bridge", "host"),
        default="none",
        help="Docker network policy (default: none)",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds per baseline run")
    parser.add_argument(
        "--baseline-runs",
        type=int,
        default=2,
        help="fresh baseline copies when --command is supplied (default: 2)",
    )
    parser.add_argument(
        "--output",
        help="output path to check (default: SOURCE-minimal); never created",
    )
    parser.add_argument(
        "--ignore",
        dest="ignore_names",
        action="append",
        type=_parse_ignore_name,
        default=[],
        metavar="NAME",
        help="exact basename excluded from baseline copies; repeatable",
    )
    parser.add_argument(
        "--ignore-path",
        dest="ignore_paths",
        action="append",
        type=_parse_ignore_path,
        default=[],
        metavar="RELATIVE_PATH",
        help="exact repository path excluded from baseline copies; repeatable",
    )
    parser.add_argument(
        "--gitignore",
        action="store_true",
        help="apply the repository .gitignore as additional exclusions",
    )
    parser.add_argument(
        "--gitignore-file",
        dest="gitignore_files",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "apply one explicit gitignore-style file; repeat for multiple files "
            "(relative paths are resolved against the repository)"
        ),
    )
    parser.add_argument(
        "--gitignore-recursive",
        action="store_true",
        help=(
            "apply the repository .gitignore and nested .gitignore files in "
            "their respective directories"
        ),
    )
    parser.add_argument(
        "--env",
        dest="environment_entries",
        action="append",
        type=_parse_environment,
        default=[],
        metavar="NAME=VALUE",
        help="explicit baseline environment override; repeatable",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable diagnostic result",
    )
    try:
        args = parser.parse_args(list(argv))
        environment = _environment_mapping(args.environment_entries)
        source = Path(args.source).expanduser().resolve()
        ok, result = run_doctor(
            source,
            command=args.command,
            match=args.match,
            exit_code=args.exit_code,
            java_exception=args.java_exception,
            python_exception=args.python_exception,
            process_failure=args.process_failure,
            adapter=args.adapter,
            source_reducer=args.source_reducer,
            backend=args.backend,
            docker_image=args.docker_image,
            docker_network=args.docker_network,
            timeout=args.timeout,
            baseline_runs=args.baseline_runs,
            environment=environment,
            output=args.output,
            ignore_names=args.ignore_names,
            ignore_paths=args.ignore_paths,
            gitignore=args.gitignore,
            gitignore_files=args.gitignore_files,
            gitignore_recursive=args.gitignore_recursive,
        )
    except (OSError, RunnerError, ValueError) as exc:
        print("repomin doctor: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(format_doctor(result), end="")
    return 0 if ok else 1


def _report_command(argv: Sequence[str]) -> int:
    """Handle report inspection commands without changing reduction parsing."""
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: repomin report {validate,replay,compare} ...")
        print(
            "inspect report evidence, replay its failure, or compare validated "
            "summaries"
        )
        print()
        print("commands:")
        print("  validate  validate report structure and optional payload evidence")
        print("  replay    run the recorded failure in fresh payload copies")
        print("  compare   compare privacy-safe evidence from two or more reports")
        print()
        print(
            "use `repomin report validate --help`, `repomin report replay --help`, "
            "or `repomin report compare --help`"
        )
        print("for command-specific options")
        return 0
    if argv[0] == "validate":
        return _report_validate_command(argv[1:])
    if argv[0] == "replay":
        return _report_replay_command(argv[1:])
    if argv[0] == "compare":
        return _report_compare_command(argv[1:])
    print("repomin report: unsupported command %r" % argv[0], file=sys.stderr)
    return 2


def _validation_ratio(numerator: int, denominator: int) -> Optional[float]:
    """Return a bounded, descriptive ratio for the validation summary."""
    if denominator <= 0:
        return None
    try:
        return round(float(numerator) / float(denominator), 6)
    except (OverflowError, ZeroDivisionError):
        # A structurally valid legacy report may contain integers too large for
        # a platform float. Keep validation successful and omit only the ratio.
        return None


def _failure_contract_mode(report: dict) -> str:
    """Classify the oracle without exposing its configured match expression."""
    spec = report.get("failure_spec")
    if isinstance(spec, dict):
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


VALIDATION_SUMMARY_SCHEMA_VERSION = 2
_SUMMARY_VERSION = re.compile(
    r"^[0-9]+(?:\.[0-9]+){2}[A-Za-z0-9._+-]*$"
)
_MAX_SUMMARY_VERSION_LENGTH = 128


def _safe_summary_version(value: object) -> Optional[str]:
    """Keep arbitrary report provenance out of shareable summaries."""
    if (
        not isinstance(value, str)
        or len(value) > _MAX_SUMMARY_VERSION_LENGTH
        or _SUMMARY_VERSION.fullmatch(value) is None
    ):
        return None
    return value


def _validation_summary(
    report: dict,
    report_path: Path,
    payload: Optional[Path],
) -> dict:
    """Build the privacy-safe scalar summary emitted by ``report validate``."""
    source = report["source"]
    output = report["output"]
    execution = report["execution"]
    holdout = report["holdout_certification"]
    source_files = source["files"]
    source_bytes = source["bytes"]
    output_files = output["files"]
    output_bytes = output["bytes"]
    result = {
        "valid": True,
        "summary_schema_version": VALIDATION_SUMMARY_SCHEMA_VERSION,
        "schema_version": report["schema_version"],
        "repomin_version": _safe_summary_version(report.get("repomin_version")),
        "holdout_status": holdout["status"],
        "holdout_planned_runs": holdout["planned_runs"],
        "holdout_completed_runs": holdout["completed_runs"],
        "holdout_passes": holdout["passes"],
        "holdout_exact_rate_gate_passed": holdout.get("exact_rate_gate_passed"),
        "backend": execution["backend"],
        "oracle_mode": _failure_contract_mode(report),
        "source_files": source_files,
        "source_bytes": source_bytes,
        "output_files": output_files,
        "output_bytes": output_bytes,
        "files_removed": source_files - output_files,
        "bytes_removed": source_bytes - output_bytes,
        "file_retention_ratio": _validation_ratio(output_files, source_files),
        "byte_retention_ratio": _validation_ratio(output_bytes, source_bytes),
        "attempts": report["attempts"],
        "accepted_mutations": report["accepted_mutations"],
        "cache_hits": report["cache_hits"],
        "budget_exhausted": execution.get("budget_exhausted", False),
        "report": str(report_path.resolve()),
    }
    if payload is not None:
        result["payload"] = str(payload.resolve())
        result["payload_checked"] = True
        fingerprint_mode, _actual_full, _actual_content = (
            _payload_fingerprint_evidence(report, payload)
        )
        result["payload_fingerprint_mode"] = fingerprint_mode
        result["payload_fingerprint_verified"] = fingerprint_mode != "unavailable"
    return result


_VALIDATION_MARKDOWN_FIELDS = (
    ("valid", "valid"),
    ("summary_schema_version", "summary_schema_version"),
    ("schema_version", "schema_version"),
    ("repomin_version", "repomin_version"),
    ("backend", "backend"),
    ("oracle_mode", "oracle_mode"),
    ("source_files", "source_files"),
    ("source_bytes", "source_bytes"),
    ("output_files", "output_files"),
    ("output_bytes", "output_bytes"),
    ("files_removed", "files_removed"),
    ("bytes_removed", "bytes_removed"),
    ("file_retention_ratio", "file_retention_ratio"),
    ("byte_retention_ratio", "byte_retention_ratio"),
    ("attempts", "attempts"),
    ("accepted_mutations", "accepted_mutations"),
    ("cache_hits", "cache_hits"),
    ("budget_exhausted", "budget_exhausted"),
    ("holdout_status", "holdout_status"),
    ("holdout_planned_runs", "holdout_planned_runs"),
    ("holdout_completed_runs", "holdout_completed_runs"),
    ("holdout_passes", "holdout_passes"),
    ("holdout_exact_rate_gate_passed", "holdout_exact_rate_gate_passed"),
    ("payload_fingerprint_mode", "payload_fingerprint_mode"),
    ("payload_fingerprint_verified", "payload_fingerprint_verified"),
)

def _markdown_cell(value: object) -> str:
    """Render one untrusted scalar as a safe Markdown table cell.

    The report validator constrains the value types, but legacy provenance
    strings can still contain arbitrary text. Escape both table syntax and
    inline Markdown, and make line/control separators visible instead of
    allowing a value to create additional rows or blocks.
    """
    if value is None:
        text = "n/a"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    escaped = []
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
    # Keep values in code spans so status names such as ``not_requested`` do
    # not become emphasis. Pick a fence longer than any input run of backticks
    # so even legacy provenance strings cannot terminate the cell early. A
    # single pass keeps this bounded for unusually large report fields.
    longest_run = 0
    current_run = 0
    for character in rendered:
        if character == "`":
            current_run += 1
            if current_run > longest_run:
                longest_run = current_run
        else:
            current_run = 0
    fence = "`" * (longest_run + 1)
    return "%s%s%s" % (fence, rendered, fence)


def format_validation_markdown(summary: dict) -> str:
    """Format a deterministic, privacy-safe validation summary.

    Only fields in ``_VALIDATION_MARKDOWN_FIELDS`` are rendered. In
    particular, report/payload paths, command text, match expressions, logs,
    and environment metadata are intentionally excluded even when present in
    the source summary dictionary.
    """
    lines = [
        "# ReproMin validation summary",
        "",
        (
            "This is privacy-safe evidence for one configured failure oracle in "
            "the recorded environment. It does not establish code correctness, "
            "production reliability, or sandbox security."
        ),
        "",
        "| Field | Value |",
        "| --- | --- |",
    ]
    for label, key in _VALIDATION_MARKDOWN_FIELDS:
        value = summary.get(key)
        lines.append("| `%s` | %s |" % (label, _markdown_cell(value)))
    return "\n".join(lines) + "\n"


def _report_validate_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="repomin report validate")
    parser.add_argument("report", type=Path, help="report.json to validate")
    parser.add_argument(
        "--payload",
        type=Path,
        help="exported payload directory whose holdout fingerprint should match",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--json",
        action="store_true",
        help="print a compact machine-readable validation result",
    )
    output_group.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        help="output format (default: text; markdown is privacy-safe)",
    )
    try:
        args = parser.parse_args(list(argv))
        report = validate_report_file(args.report, args.payload)
    except (ReportValidationError, ValueError, OSError) as exc:
        print("repomin report: %s" % exc, file=sys.stderr)
        return 2
    result = _validation_summary(report, args.report, args.payload)
    output_format = "json" if args.json else args.format
    if output_format == "json":
        print(json.dumps(result, sort_keys=True))
    elif output_format == "markdown":
        print(format_validation_markdown(result), end="")
    else:
        print(
            "Valid ReproMin report (schema %s, oracle %s, holdout %s)."
            % (
                result["schema_version"],
                result["oracle_mode"],
                result["holdout_status"],
            )
        )
        print(
            "Payload: %s files, %s bytes (retention %s files / %s bytes)."
            % (
                result["output_files"],
                result["output_bytes"],
                result["file_retention_ratio"]
                if result["file_retention_ratio"] is not None
                else "n/a",
                result["byte_retention_ratio"]
                if result["byte_retention_ratio"] is not None
                else "n/a",
            )
        )
    return 0


def _report_compare_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repomin report compare",
        description=(
            "Compare privacy-safe evidence from validated reports; labels are "
            "display-only and do not filter or group data."
        ),
    )
    parser.add_argument(
        "reports",
        nargs="+",
        type=Path,
        metavar="REPORT.json",
        help="two or more report.json files, compared in the supplied order",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "short ASCII display label for one report (does not affect the "
            "comparison); repeat once per report (defaults to run-1, run-2, ...)"
        ),
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        help="output format (default: text; markdown is privacy-safe)",
    )
    try:
        # Allow labels to be placed between report paths as users naturally
        # build a command incrementally.  This parser has no subparsers or
        # positional optionals, so argparse's intermixed mode is sufficient.
        parse_intermixed = getattr(parser, "parse_intermixed_args", None)
        if parse_intermixed is None:
            args = parser.parse_args(list(argv))
        else:
            args = parse_intermixed(list(argv))
        comparison = compare_reports(args.reports, labels=args.label)
    except (ReportComparisonError, OSError, ValueError) as exc:
        print("repomin report compare: %s" % exc, file=sys.stderr)
        return 2
    try:
        if args.format == "json":
            print(json.dumps(comparison, sort_keys=True, allow_nan=False))
        elif args.format == "markdown":
            print(render_comparison_markdown(comparison), end="")
        else:
            print(render_comparison_text(comparison), end="")
    except (TypeError, ValueError, OverflowError) as exc:
        print("repomin report compare: could not render output: %s" % exc, file=sys.stderr)
        return 2
    return 0


def _report_replay_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repomin report replay",
        description=(
            "Execute the command stored in a report against fresh payload copies. "
            "The command is untrusted; review the report before passing --yes."
        ),
    )
    parser.add_argument("report", type=Path, help="report.json containing the command")
    parser.add_argument(
        "--payload",
        type=Path,
        required=True,
        help="exported payload directory to copy for every replay run",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="fresh replay copies to execute; every run must pass (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="seconds per run (default: recorded value, otherwise 120)",
    )
    parser.add_argument(
        "--env",
        dest="environment_entries",
        action="append",
        type=_parse_environment,
        default=[],
        metavar="NAME=VALUE",
        help="recorded explicit environment override; repeatable",
    )
    parser.add_argument(
        "--backend",
        choices=("recorded", "host", "docker"),
        default="recorded",
        help="execution backend (default: backend recorded in report)",
    )
    parser.add_argument(
        "--docker-image",
        help="explicit local Docker image override; never pulled",
    )
    parser.add_argument(
        "--docker-network",
        choices=("none", "bridge", "host"),
        help="Docker network policy (default: none, even if report differs)",
    )
    parser.add_argument(
        "--exit-code",
        type=int,
        dest="legacy_exit_code",
        help="explicit contract for an ambiguous legacy report",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="acknowledge execution of the untrusted command stored in the report",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable replay evidence without raw command output",
    )
    args = parser.parse_args(list(argv))
    try:
        if not args.yes:
            raise ReplayError(
                "replay executes the command stored in the report; review it and pass --yes"
            )
        environment = _environment_mapping(args.environment_entries)
        reproduced, result = replay_report(
            args.report,
            args.payload,
            runs=args.runs,
            timeout_seconds=args.timeout,
            environment=environment,
            backend=args.backend,
            docker_image=args.docker_image,
            docker_network=args.docker_network,
            legacy_exit_code=args.legacy_exit_code,
        )
    except KeyboardInterrupt:
        print("repomin report replay: interrupted", file=sys.stderr)
        return 130
    except (
        OSError,
        OracleError,
        ReplayError,
        ReportValidationError,
        RunnerError,
        SessionError,
    ) as exc:
        if args.json:
            print(json.dumps({"reproduced": False, "error": str(exc)}, sort_keys=True))
        else:
            print("repomin report replay: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(format_replay(result), end="")
    return 0 if reproduced else 1


def _run_fixed_point(
    components: Sequence[Tuple[str, Callable[[], object]]],
    stats: ReductionStats,
    progress: Callable[[str], None],
) -> None:
    """Run locally stable reducers until no component is dirty."""
    pending = list(range(len(components)))
    queued = set(pending)
    while pending:
        component_index = pending.pop(0)
        queued.remove(component_index)
        name, reduce_component = components[component_index]
        accepted_before = stats.accepted
        progress("fixed-point component: %s" % name)
        reduce_component()
        if stats.accepted == accepted_before:
            continue
        for other_index in range(len(components)):
            if other_index == component_index or other_index in queued:
                continue
            pending.append(other_index)
            queued.add(other_index)


def _build_runner(args: argparse.Namespace) -> Runner:
    if args.backend == "host":
        if args.docker_image:
            raise ValueError("--docker-image requires --backend docker")
        docker_only = []
        if args.docker_network != "none":
            docker_only.append("--docker-network")
        if args.docker_cpus is not None:
            docker_only.append("--docker-cpus")
        if args.docker_memory is not None:
            docker_only.append("--docker-memory")
        if args.docker_pids_limit != DEFAULT_DOCKER_PIDS_LIMIT:
            docker_only.append("--docker-pids-limit")
        if args.docker_tmpfs_size != DEFAULT_DOCKER_TMPFS_BYTES:
            docker_only.append("--docker-tmpfs-size")
        if args.docker_workspace_limit is not None:
            docker_only.append("--docker-workspace-limit")
        if docker_only:
            raise ValueError(
                "%s requires --backend docker" % ", ".join(docker_only)
            )
        return CommandRunner(
            args.command,
            args.timeout,
            environment=_environment_mapping(args.environment_entries),
            collect_java_diagnostics=args.java_exception,
        )
    if not args.docker_image:
        raise ValueError("--backend docker requires --docker-image")
    runner = DockerRunner(
        args.command,
        args.timeout,
        image=args.docker_image,
        network=args.docker_network,
        environment=_environment_mapping(args.environment_entries),
        collect_java_diagnostics=args.java_exception,
        cpus=args.docker_cpus,
        memory_bytes=args.docker_memory,
        pids_limit=args.docker_pids_limit,
        tmpfs_bytes=args.docker_tmpfs_size,
        workspace_limit_bytes=args.docker_workspace_limit,
    )
    runner.validate()
    return runner


def _resolve_paths(
    source_value: str,
    output_value: Optional[str],
    allow_existing_output: bool = False,
) -> tuple:
    source = Path(source_value).expanduser().resolve()
    if not source.is_dir():
        raise NotADirectoryError(
            "source is not a directory: %s (check SOURCE or pass a repository path)"
            % source
        )
    output_path = (
        Path(output_value).expanduser()
        if output_value
        else source.with_name(source.name + "-minimal")
    )
    _reject_symbolic_link(output_path, "output")
    if output_path.name in {"", ".", ".."}:
        output = output_path.resolve()
    else:
        output = output_path.parent.resolve() / output_path.name
    _reject_symbolic_link(output, "output")
    if output.exists() and not allow_existing_output:
        raise FileExistsError(
            "output already exists: %s (choose another --output or use --resume)"
            % output
        )
    if _is_within(output, source):
        raise ValueError("output must not be inside the source repository")
    return source, output


def _validate_keep_paths(source: Path, keep_paths: Sequence[str]) -> None:
    """Reject explicit keep paths that cannot protect anything."""
    for value in sorted(set(keep_paths)):
        candidate = source.joinpath(*PurePosixPath(value).parts)
        try:
            status = candidate.lstat()
        except FileNotFoundError as exc:
            raise ValueError(
                "keep path does not exist in the source repository: %s "
                "(check --keep %s)" % (value, value)
            ) from exc
        except OSError as exc:
            raise ValueError(
                "keep path could not be inspected: %s (check --keep %s)"
                % (value, value)
            ) from exc
        if not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)):
            raise ValueError(
                "keep path must be a regular file or directory: %s "
                "(check --keep %s)" % (value, value)
            )


def _reject_symbolic_link(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise FileExistsError("%s must not be a symbolic link: %s" % (label, path))


def _resolve_session_path(
    source: Path,
    output: Path,
    metadata_output: Path,
    session_value: Optional[str],
    resume: bool,
) -> Optional[Path]:
    if resume and not session_value:
        raise ValueError("--resume requires --session PATH")
    if session_value is None:
        return None
    session = Path(session_value).expanduser().resolve()
    if session == source or _is_within(session, source):
        raise ValueError("session must not be inside the source repository")
    if (
        _is_within(session, output)
        or _is_within(output, session)
        or _is_within(session, metadata_output)
        or _is_within(metadata_output, session)
    ):
        raise ValueError(
            "session, output, and metadata directories must not overlap"
        )
    return session


def _metadata_output_path(output: Path) -> Path:
    return output.with_name(output.name + ".repomin")


def _working_directory_configuration(
    output: Path,
    backend: str,
) -> Tuple[str, str, Optional[str]]:
    if backend == "docker":
        return DOCKER_WORKING_DIRECTORY_POLICY, "workspace", None
    basename = output.name
    parsed = Path(basename)
    if (
        not basename
        or basename in {".", ".."}
        or "\x00" in basename
        or parsed.is_absolute()
        or len(parsed.parts) != 1
        or parsed.name != basename
    ):
        raise ValueError(
            "host output path must end in a single ordinary directory name"
        )
    return HOST_WORKING_DIRECTORY_POLICY, basename, basename


def _session_identity(
    args: argparse.Namespace,
    baseline_min_passes: Optional[int] = None,
    candidate_min_passes: Optional[int] = None,
    java_analysis_classpath: Sequence[JavaAnalysisClasspathEntry] = (),
    holdout_confidence: float = 0.95,
    docker_image_id: Optional[str] = None,
    working_directory_policy: Optional[str] = None,
    working_directory_basename: Optional[str] = None,
    environment: Optional[dict] = None,
    ignore_paths: Sequence[str] = (),
    gitignore_files: Sequence[str] = (),
    gitignore_sha256: Optional[str] = None,
    gitignore_recursive: bool = False,
    keep_paths: Sequence[str] = (),
    max_attempts: Optional[int] = None,
    max_duration: Optional[float] = None,
    semantic_reducer: Optional[str] = None,
    semantic_endpoint: Optional[str] = None,
    semantic_model: Optional[str] = None,
    text_files: Sequence[str] = (),
) -> dict:
    configured_environment = {} if environment is None else dict(environment)
    return {
        "command": args.command,
        "match": args.match,
        "exit_code": args.exit_code,
        "timeout": args.timeout,
        "backend": args.backend,
        "working_directory_policy": working_directory_policy,
        "working_directory_basename": working_directory_basename,
        "docker_image": args.docker_image,
        "docker_image_id": docker_image_id,
        "docker_network": args.docker_network,
        "docker_cpus": args.docker_cpus,
        "docker_memory": args.docker_memory,
        "docker_pids_limit": args.docker_pids_limit,
        "docker_tmpfs_size": args.docker_tmpfs_size,
        "docker_workspace_limit": args.docker_workspace_limit,
        "jobs": args.jobs,
        "cache_enabled": not args.no_cache,
        "environment_names": sorted(configured_environment),
        "environment_sha256": _environment_digest(configured_environment),
        "ignored_paths": sorted(set(ignore_paths)),
        # Keep the effective rule-file order: it is part of matching
        # semantics, not merely display metadata.
        "gitignore_files": list(dict.fromkeys(gitignore_files)),
        "gitignore_sha256": gitignore_sha256,
        "gitignore_recursive": bool(gitignore_recursive),
        "keep_paths": sorted(set(keep_paths)),
        "max_attempts": max_attempts,
        "max_duration_seconds": max_duration,
        "semantic_reducer": semantic_reducer,
        "semantic_endpoint": semantic_endpoint,
        "semantic_model": semantic_model,
        "text_files": sorted(set(text_files)),
        "java_exception": args.java_exception,
        "python_exception": args.python_exception,
        "process_failure": args.process_failure,
        "baseline_runs": args.baseline_runs,
        "min_baseline_passes": (
            args.baseline_runs if baseline_min_passes is None else baseline_min_passes
        ),
        "min_baseline_rate": args.min_baseline_rate,
        "candidate_runs": args.candidate_runs,
        "min_candidate_passes": (
            args.candidate_runs
            if candidate_min_passes is None
            else candidate_min_passes
        ),
        "min_candidate_rate": args.min_candidate_rate,
        "confidence": args.confidence,
        "run_confidence": args.run_confidence,
        "candidate_family_control_policy": (
            CANDIDATE_FAMILY_CONTROL_POLICY
            if args.run_confidence is not None
            else None
        ),
        "candidate_sampling_policy": CANDIDATE_SAMPLING_POLICY,
        "reduction_strategy": REDUCTION_STRATEGY,
        "holdout_runs": args.holdout_runs,
        "min_holdout_rate": args.min_holdout_rate,
        "holdout_confidence": holdout_confidence,
        "holdout_certification_policy": HOLDOUT_CERTIFICATION_POLICY,
        "adapter": args.adapter,
        "source_reducer": args.source_reducer,
        "ignored_names": sorted(set(DEFAULT_IGNORES).union(args.ignore_names)),
        "java_analysis_classpath": [
            {
                "path": str(entry.path),
                "kind": entry.kind,
                "fingerprint": entry.fingerprint,
            }
            for entry in java_analysis_classpath
        ],
    }


def _sample_threshold(
    runs: int,
    minimum: Optional[int],
    label: str,
    minimum_rate: Optional[float] = None,
) -> int:
    if runs < 1:
        raise ValueError("%s runs must be at least 1" % label)
    # A rate criterion supplies the statistical requirement.  Requiring every
    # sample as well would make a flaky-failure mode equivalent to strict mode.
    required = (1 if minimum_rate is not None else runs) if minimum is None else minimum
    if required < 1 or required > runs:
        raise ValueError(
            "minimum %s passes must be between 1 and %s runs" % (label, label)
        )
    return required


def _holdout_configuration(args: argparse.Namespace) -> float:
    runs = args.holdout_runs
    minimum_rate = args.min_holdout_rate
    confidence = args.holdout_confidence
    if (runs is None) != (minimum_rate is None):
        raise ValueError(
            "--holdout-runs and --min-holdout-rate must be provided together"
        )
    if runs is None:
        if confidence is not None:
            raise ValueError(
                "--holdout-confidence requires --holdout-runs and "
                "--min-holdout-rate"
            )
        return 0.95
    if runs < 1:
        raise ValueError("holdout runs must be at least 1")
    return 0.95 if confidence is None else confidence


def _validate_holdout_rate_attainable(
    runs: int,
    minimum_rate: float,
    confidence: float,
) -> None:
    if exact_binomial_rate_gate(runs, runs, minimum_rate, confidence):
        return
    best_possible = clopper_pearson_lower_bound(runs, runs, confidence)
    raise ValueError(
        "minimum holdout rate %.4g is unattainable with %d holdout runs at "
        "%.4g confidence (best possible exact lower bound: %.4g); increase "
        "--holdout-runs or lower the rate/confidence"
        % (minimum_rate, runs, confidence, best_possible)
    )


def _validate_rate_attainable(
    runs: int,
    minimum_rate: Optional[float],
    confidence: float,
    label: str,
    signature_discovery: bool = False,
) -> None:
    if minimum_rate is None:
        return
    evidence_runs = runs - int(signature_discovery)
    if exact_binomial_rate_gate(
        evidence_runs,
        evidence_runs,
        minimum_rate,
        confidence,
    ):
        return
    best_possible = clopper_pearson_lower_bound(
        evidence_runs,
        evidence_runs,
        confidence,
    )
    discovery_detail = (
        ", leaving %d post-discovery rate-evidence runs" % evidence_runs
        if signature_discovery
        else ""
    )
    raise ValueError(
        "minimum %s rate %.4g is unattainable with %d %s runs%s at %.4g "
        "confidence (best possible exact lower bound: %.4g); increase "
        "--%s-runs or lower the rate/confidence"
        % (
            label,
            minimum_rate,
            runs,
            label,
            discovery_detail,
            confidence,
            best_possible,
            label,
        )
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
