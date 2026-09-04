"""Read-only preflight checks for a prospective ReproMin reduction."""

from __future__ import annotations

import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from repomin.execution import CommandRunner, DockerRunner, RunnerError
from repomin.gitignore import load_gitignore
from repomin.input_paths import (
    normalize_ignore_path,
    normalize_text_file_path,
    validate_keep_paths,
    validate_text_file_paths,
)
from repomin.model import FailureSpec
from repomin.oracle import FailureOracle, OracleError
from repomin.sampling import sample_threshold, validate_rate_attainable
from repomin.session import (
    DEFAULT_IGNORES,
    IgnoreSet,
    _copy_repository,
    _validate_repository_entries,
)


_ADAPTER_NAMES = (
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
)
_JAVA_VERSION = re.compile(r"(?:openjdk|java|javac)[^0-9]*(\d+)(?:\.\d+)?")


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _positive_integer(value: object, minimum: int = 1) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= minimum
    )


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _valid_directory_basename(value: str) -> bool:
    parsed = Path(value)
    return bool(
        value
        and value not in {".", ".."}
        and "\x00" not in value
        and not parsed.is_absolute()
        and len(parsed.parts) == 1
        and parsed.name == value
    )


def _raise_walk_error(error: OSError) -> None:
    raise error


def _repository_files(root: Path, ignores: Optional[IgnoreSet] = None) -> List[Path]:
    """Return regular files while pruning the same exclusions as a session."""
    active_ignores = ignores or IgnoreSet(DEFAULT_IGNORES)
    files: List[Path] = []
    root = root.resolve()
    for directory, dirnames, filenames in os.walk(
        root,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        directory_path = Path(directory)
        kept_directories = []
        for name in sorted(dirnames):
            relative = (directory_path / name).relative_to(root)
            if not active_ignores.matches(relative, is_directory=True):
                kept_directories.append(name)
        dirnames[:] = kept_directories
        for name in sorted(filenames):
            path = directory_path / name
            relative = path.relative_to(root)
            if active_ignores.matches(relative, is_directory=False):
                continue
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode):
                files.append(path)
    return sorted(files, key=lambda value: value.relative_to(root).as_posix())


def _matching_files(
    root: Path,
    kind: str,
    files: Optional[Sequence[Path]] = None,
) -> List[str]:
    files = list(files) if files is not None else _repository_files(root)
    matches: List[str] = []
    for path in files:
        name = path.name
        suffix = path.suffix
        matched = False
        if kind == "maven":
            matched = name == "pom.xml"
        elif kind == "gradle":
            matched = name == "gradle.properties" or name.endswith(
                (".gradle", ".gradle.kts")
            )
        elif kind == "python":
            matched = name == "pyproject.toml" or (
                name.startswith("requirements") and suffix == ".txt"
            )
            if not matched and suffix == ".txt":
                relative = path.relative_to(root)
                matched = "requirements" in relative.parts[:-1]
        elif kind == "pipenv":
            matched = name == "Pipfile"
        elif kind == "node":
            matched = name == "package.json"
        elif kind == "composer":
            matched = name == "composer.json"
        elif kind == "dotnet":
            matched = name == "Directory.Build.props" or suffix in {
                ".csproj",
                ".fsproj",
                ".vbproj",
            }
        elif kind == "ruby":
            matched = name in {"Gemfile", "gems.rb"} or (
                name.startswith("Gemfile.") and name != "Gemfile.lock"
            )
        elif kind == "cargo":
            matched = name == "Cargo.toml"
        elif kind == "go":
            matched = name in {"go.mod", "go.work"}
        if matched:
            matches.append(path.relative_to(root).as_posix())
    return matches


def _java_toolchain() -> Dict[str, object]:
    java = shutil.which("java")
    javac = shutil.which("javac")
    if java is None or javac is None:
        return {
            "available": False,
            "version": None,
            "message": "JDK 11 or newer (java and javac) was not found",
        }
    try:
        completed = subprocess.run(
            [javac, "-version"],
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "available": False,
            "version": None,
            "message": "javac could not be queried",
        }
    version_text = (completed.stdout or "") + " " + (completed.stderr or "")
    match = _JAVA_VERSION.search(version_text)
    version = int(match.group(1)) if match is not None else None
    if version == 1:
        legacy = re.search(r"(?:java|javac)[^0-9]+1\.(\d+)", version_text)
        version = int(legacy.group(1)) if legacy is not None else version
    available = completed.returncode == 0 and version is not None and version >= 11
    return {
        "available": available,
        "version": version,
        "message": (
            "JDK %s is available" % version
            if available
            else "JDK 11 or newer is required for the Java source reducer"
        ),
    }


def _check(
    checks: List[Dict[str, object]],
    name: str,
    status: str,
    message: str,
) -> None:
    checks.append({"name": name, "status": status, "message": message})


def _runner(
    command: str,
    timeout: float,
    backend: str,
    docker_image: Optional[str],
    docker_network: str,
    docker_cpus: Optional[float],
    docker_memory: Optional[int],
    docker_pids_limit: int,
    docker_tmpfs_size: int,
    docker_workspace_limit: Optional[int],
    environment: Mapping[str, str],
    java_exception: bool,
) -> object:
    if backend == "host":
        return CommandRunner(
            command,
            timeout,
            environment=environment,
            collect_java_diagnostics=java_exception,
        )
    if not docker_image:
        raise RunnerError("--backend docker requires --docker-image")
    runner = DockerRunner(
        command,
        timeout,
        image=docker_image,
        network=docker_network,
        environment=environment,
        collect_java_diagnostics=java_exception,
        cpus=docker_cpus,
        memory_bytes=docker_memory,
        pids_limit=docker_pids_limit,
        tmpfs_bytes=docker_tmpfs_size,
        workspace_limit_bytes=docker_workspace_limit,
    )
    runner.validate()
    return runner


def run_doctor(
    source: Path,
    *,
    command: Optional[str] = None,
    match: Optional[str] = None,
    exit_code: Optional[int] = None,
    java_exception: bool = False,
    python_exception: bool = False,
    process_failure: bool = False,
    adapter: str = "auto",
    source_reducer: str = "auto",
    backend: str = "host",
    docker_image: Optional[str] = None,
    docker_network: str = "none",
    docker_cpus: Optional[float] = None,
    docker_memory: Optional[int] = None,
    docker_pids_limit: int = 512,
    docker_tmpfs_size: int = 1024 * 1024 * 1024,
    docker_workspace_limit: Optional[int] = None,
    timeout: float = 120.0,
    baseline_runs: int = 2,
    min_baseline_passes: Optional[int] = None,
    min_baseline_rate: Optional[float] = None,
    confidence: float = 0.95,
    environment: Optional[Mapping[str, str]] = None,
    output: Optional[str] = None,
    ignore_names: Sequence[str] = (),
    ignore_paths: Sequence[str] = (),
    keep_paths: Sequence[str] = (),
    text_files: Sequence[str] = (),
    gitignore: bool = False,
    gitignore_files: Sequence[str] = (),
    gitignore_recursive: bool = False,
) -> Tuple[bool, Dict[str, object]]:
    """Run static checks and, when requested, a fresh-copy baseline check."""
    source = source.expanduser().resolve()
    keep_path_error: Optional[ValueError] = None
    text_file_error: Optional[ValueError] = None
    try:
        normalized_keep_paths = sorted(
            {normalize_ignore_path(value) for value in keep_paths}
        )
    except ValueError as exc:
        normalized_keep_paths = []
        keep_path_error = exc
    try:
        normalized_text_files = sorted(
            {normalize_text_file_path(value) for value in text_files}
        )
    except ValueError as exc:
        normalized_text_files = []
        text_file_error = exc
    checks: List[Dict[str, object]] = []
    result: Dict[str, object] = {
        "schema_version": 1,
        "source": str(source),
        "checks": checks,
        "adapters": {},
        "source_reducers": {},
        "baseline": {"status": "not_run"},
        "gitignore_files": [],
        "gitignore_sha256": None,
        "gitignore_recursive": bool(gitignore_recursive),
        "keep_paths": normalized_keep_paths,
        "text_files": normalized_text_files,
    }
    if adapter not in ("auto", "none") + _ADAPTER_NAMES:
        _check(checks, "adapter", "fail", "unsupported adapter: %s" % adapter)
    if source_reducer not in {"auto", "none", "java", "python"}:
        _check(
            checks,
            "source-reducer",
            "fail",
            "unsupported source reducer: %s" % source_reducer,
        )
    if backend not in {"host", "docker"}:
        _check(checks, "backend", "fail", "unsupported backend: %s" % backend)
    selection_valid = keep_path_error is None and text_file_error is None
    if keep_path_error is not None:
        _check(checks, "keep-paths", "fail", str(keep_path_error))
    if text_file_error is not None:
        _check(checks, "text-targets", "fail", str(text_file_error))
    elif not normalized_text_files:
        _check(checks, "text-targets", "skip", "no text files requested")
    if not source.is_dir():
        _check(checks, "source", "fail", "source is not a directory: %s" % source)
        result["ok"] = False
        return False, result
    try:
        (
            gitignore_matcher,
            loaded_gitignore_files,
            gitignore_sha256,
            loaded_gitignore_recursive,
        ) = load_gitignore(
            source,
            gitignore,
            gitignore_files,
            recursive=gitignore_recursive,
            ignore_names=ignore_names,
            ignore_paths=ignore_paths,
            default_ignores=DEFAULT_IGNORES,
        )
    except (OSError, ValueError) as exc:
        _check(checks, "gitignore", "fail", str(exc))
        result["ok"] = False
        return False, result
    result["gitignore_files"] = list(loaded_gitignore_files)
    result["gitignore_sha256"] = gitignore_sha256
    result["gitignore_recursive"] = loaded_gitignore_recursive
    ignores = IgnoreSet(
        DEFAULT_IGNORES,
        sorted(set(ignore_paths)),
        gitignore_matcher,
        normalized_keep_paths,
    )
    ignores.update(ignore_names)
    try:
        _validate_repository_entries(source, ignores)
    except (OSError, RuntimeError, ValueError) as exc:
        _check(checks, "source", "fail", str(exc))
        if normalized_keep_paths and keep_path_error is None:
            _check(
                checks,
                "keep-paths",
                "fail",
                "keep paths were not validated because the source repository is unsafe",
            )
        if normalized_text_files and text_file_error is None:
            _check(
                checks,
                "text-targets",
                "fail",
                "text files were not validated because the source repository is unsafe",
            )
        result["ok"] = False
        return False, result
    if normalized_keep_paths:
        try:
            validate_keep_paths(source, normalized_keep_paths)
        except ValueError as exc:
            _check(checks, "keep-paths", "fail", str(exc))
            selection_valid = False
        else:
            _check(
                checks,
                "keep-paths",
                "pass",
                "%d keep path(s) are valid" % len(normalized_keep_paths),
            )
    if normalized_text_files:
        try:
            validate_text_file_paths(
                source,
                normalized_text_files,
                ignore_names=ignore_names,
                ignore_paths=ignore_paths,
                gitignore_matcher=gitignore_matcher,
                keep_paths=normalized_keep_paths,
            )
        except ValueError as exc:
            _check(checks, "text-targets", "fail", str(exc))
            selection_valid = False
        else:
            _check(
                checks,
                "text-targets",
                "pass",
                "%d text file(s) are valid" % len(normalized_text_files),
            )

    try:
        repository_files = _repository_files(source, ignores)
        source_files = len(repository_files)
        source_bytes = sum(path.stat().st_size for path in repository_files)
    except OSError as exc:
        _check(checks, "source", "fail", "source changed while being scanned: %s" % exc)
        result["ok"] = False
        return False, result
    result["source_files"] = source_files
    result["source_bytes"] = source_bytes
    result["ignored_names"] = sorted(ignores)
    result["ignored_paths"] = list(ignores.paths)
    _check(
        checks,
        "source",
        "pass",
        "%d files, %d bytes" % (source_files, source_bytes),
    )

    detected_adapters: List[str] = []
    adapters: Dict[str, object] = {}
    for name in _ADAPTER_NAMES:
        files = _matching_files(source, name, repository_files)
        if files:
            detected_adapters.append(name)
        adapters[name] = {
            "detected": bool(files),
            "file_count": len(files),
            "files": files[:20],
        }
    result["adapters"] = adapters
    if adapter not in ("auto", "none") + _ADAPTER_NAMES:
        pass
    elif adapter != "auto" and adapter != "none" and adapter not in detected_adapters:
        _check(
            checks,
            "adapter",
            "fail",
            "requested %s adapter but no matching manifest was found" % adapter,
        )
    elif adapter == "auto" and len(detected_adapters) > 1:
        _check(
            checks,
            "adapter",
            "warn",
            "auto detected multiple adapters: %s" % ", ".join(detected_adapters),
        )
    elif adapter == "auto":
        _check(
            checks,
            "adapter",
            "pass" if detected_adapters else "warn",
            (
                "detected %s" % detected_adapters[0]
                if detected_adapters
                else "no structured manifest detected; use --adapter none if intentional"
            ),
        )
    else:
        _check(checks, "adapter", "pass", "requested adapter: %s" % adapter)

    java_files = [
        path.relative_to(source).as_posix()
        for path in repository_files
        if path.suffix == ".java"
    ]
    python_files = [
        path.relative_to(source).as_posix()
        for path in repository_files
        if path.suffix == ".py"
    ]
    java_toolchain = _java_toolchain()
    reducers: Dict[str, object] = {
        "java": {
            "detected": bool(java_files),
            "available": bool(java_files) and bool(java_toolchain["available"]),
            "file_count": len(java_files),
            "files": java_files[:20],
            "toolchain": java_toolchain,
        },
        "python": {
            "detected": bool(python_files),
            "available": bool(python_files),
            "file_count": len(python_files),
            "files": python_files[:20],
            "toolchain": {"available": True, "version": sys.version.split()[0]},
        },
    }
    result["source_reducers"] = reducers
    if source_reducer not in {"auto", "none", "java", "python"}:
        pass
    elif source_reducer == "java":
        if not java_files:
            _check(checks, "source-reducer", "fail", "no .java files were found")
        elif not java_toolchain["available"]:
            _check(checks, "source-reducer", "fail", str(java_toolchain["message"]))
        else:
            _check(checks, "source-reducer", "pass", "Java source reducer is available")
    elif source_reducer == "python":
        _check(
            checks,
            "source-reducer",
            "pass" if python_files else "fail",
            "Python source reducer is available"
            if python_files
            else "no .py files were found",
        )
    elif source_reducer == "auto":
        available = []
        if java_files and java_toolchain["available"]:
            available.append("java")
        if python_files:
            available.append("python")
        _check(
            checks,
            "source-reducer",
            "pass" if available else "warn",
            "available: %s" % ", ".join(available)
            if available
            else "no native source reducer is available",
        )
    else:
        _check(checks, "source-reducer", "pass", "source reducer disabled")

    configured_output = (
        Path(output).expanduser()
        if output is not None
        else source.with_name(source.name + "-minimal")
    )
    if not configured_output.is_absolute():
        configured_output = Path.cwd() / configured_output
    working_directory_valid = backend != "host" or _valid_directory_basename(
        configured_output.name
    )
    if not working_directory_valid:
        _check(
            checks,
            "output",
            "fail",
            "host output path must end in a single ordinary directory name",
        )
        output_path = configured_output
    elif configured_output.is_symlink():
        _check(checks, "output", "fail", "output must not be a symbolic link")
        output_path = configured_output
    else:
        output_path = configured_output.parent.resolve() / configured_output.name
        metadata_path = output_path.with_name(output_path.name + ".repomin")
        result["output"] = str(output_path)
        result["metadata"] = str(metadata_path)
        if output_path.is_symlink():
            _check(checks, "output", "fail", "output must not be a symbolic link")
        elif metadata_path.is_symlink():
            _check(
                checks,
                "output",
                "fail",
                "metadata output must not be a symbolic link",
            )
        elif _inside(output_path, source):
            _check(
                checks,
                "output",
                "fail",
                "output must not be inside the source repository",
            )
        elif output_path.exists():
            _check(
                checks,
                "output",
                "fail",
                "output already exists: %s" % output_path,
            )
        elif metadata_path.exists():
            _check(
                checks,
                "output",
                "fail",
                "metadata output already exists: %s" % metadata_path,
            )
        else:
            label = "configured" if output is not None else "default"
            _check(
                checks,
                "output",
                "pass",
                "%s output is available outside the source repository" % label,
            )

    mode_count = sum((java_exception, python_exception, process_failure))
    oracle_requested = command is not None
    oracle_valid = mode_count <= 1
    if mode_count > 1:
        _check(checks, "oracle", "fail", "only one learned failure mode may be enabled")
    if oracle_requested and (
        not isinstance(command, str) or not command.strip()
    ):
        _check(checks, "oracle", "fail", "command must not be empty or whitespace")
        oracle_valid = False
    if match is not None:
        try:
            re.compile(match)
        except re.error as exc:
            _check(checks, "oracle", "fail", "invalid --match regular expression: %s" % exc)
            oracle_valid = False
    if not oracle_requested:
        if any(value is not None for value in (match, exit_code)) or any(
            (java_exception, python_exception, process_failure)
        ):
            _check(checks, "oracle", "fail", "failure options require --command")
            oracle_valid = False
        else:
            _check(checks, "oracle", "skip", "no command supplied; baseline not run")
    else:
        if match is None and exit_code is None and not process_failure:
            _check(checks, "oracle", "fail", "set --match, --exit-code, or --process-failure")
            oracle_valid = False
        if process_failure and exit_code is not None:
            _check(checks, "oracle", "fail", "--process-failure cannot be combined with --exit-code")
            oracle_valid = False
        if oracle_valid:
            _check(checks, "oracle", "pass", "failure contract is configured")

    backend_valid = backend in {"host", "docker"}
    if backend == "host":
        incompatible = []
        if docker_image:
            incompatible.append("--docker-image")
        if docker_network != "none":
            incompatible.append("--docker-network")
        if docker_cpus is not None:
            incompatible.append("--docker-cpus")
        if docker_memory is not None:
            incompatible.append("--docker-memory")
        if docker_pids_limit != 512:
            incompatible.append("--docker-pids-limit")
        if docker_tmpfs_size != 1024 * 1024 * 1024:
            incompatible.append("--docker-tmpfs-size")
        if docker_workspace_limit is not None:
            incompatible.append("--docker-workspace-limit")
        if incompatible:
            _check(
                checks,
                "backend",
                "fail",
                "%s requires --backend docker" % ", ".join(incompatible),
            )
            backend_valid = False
        else:
            _check(checks, "backend", "pass", "host backend selected")
    elif backend == "docker":
        if docker_network not in {"none", "bridge", "host"}:
            _check(
                checks,
                "backend",
                "fail",
                "unsupported Docker network policy: %s" % docker_network,
            )
            backend_valid = False
        elif not docker_image:
            _check(
                checks,
                "backend",
                "fail",
                "--backend docker requires --docker-image",
            )
            backend_valid = False

    docker_limits = (
        (
            docker_cpus is None
            or (_finite_number(docker_cpus) and docker_cpus > 0),
            "Docker CPU limit must be a finite number greater than zero",
        ),
        (
            docker_memory is None
            or _positive_integer(docker_memory, 6 * 1024 * 1024),
            "Docker memory limit must be an integer of at least 6 MiB",
        ),
        (
            _positive_integer(docker_pids_limit),
            "Docker PID limit must be a positive integer",
        ),
        (
            _positive_integer(docker_tmpfs_size),
            "Docker tmpfs size must be a positive integer",
        ),
        (
            docker_workspace_limit is None
            or _positive_integer(docker_workspace_limit),
            "Docker workspace limit must be a positive integer",
        ),
    )
    for valid, message in docker_limits:
        if not valid:
            _check(checks, "backend", "fail", message)
            backend_valid = False
    if backend == "docker" and backend_valid:
        _check(checks, "backend", "pass", "docker backend selected")

    baseline_configuration_valid = True
    baseline_minimum: Optional[int] = None
    if isinstance(baseline_runs, bool) or not isinstance(baseline_runs, int):
        _check(checks, "baseline", "fail", "baseline runs must be an integer")
        baseline_configuration_valid = False
    elif baseline_runs < 1:
        _check(checks, "baseline", "fail", "baseline runs must be at least 1")
        baseline_configuration_valid = False
    if min_baseline_passes is not None and (
        isinstance(min_baseline_passes, bool)
        or not isinstance(min_baseline_passes, int)
    ):
        _check(
            checks,
            "baseline",
            "fail",
            "minimum baseline passes must be an integer",
        )
        baseline_configuration_valid = False
    elif (
        min_baseline_passes is not None
        and isinstance(baseline_runs, int)
        and not isinstance(baseline_runs, bool)
        and (min_baseline_passes < 1 or min_baseline_passes > baseline_runs)
    ):
        _check(
            checks,
            "baseline",
            "fail",
            "minimum baseline passes must be between 1 and baseline runs",
        )
        baseline_configuration_valid = False
    rate_valid = min_baseline_rate is None or (
        _finite_number(min_baseline_rate) and 0.0 < min_baseline_rate < 1.0
    )
    if not rate_valid:
        _check(
            checks,
            "baseline",
            "fail",
            "minimum baseline rate must be a finite number in (0, 1)",
        )
        baseline_configuration_valid = False
    confidence_valid = _finite_number(confidence) and 0.0 < confidence < 1.0
    if not confidence_valid:
        _check(
            checks,
            "baseline",
            "fail",
            "confidence must be a finite number in (0, 1)",
        )
        baseline_configuration_valid = False
    if baseline_configuration_valid:
        try:
            baseline_minimum = sample_threshold(
                baseline_runs,
                min_baseline_passes,
                "baseline",
                min_baseline_rate,
            )
            validate_rate_attainable(
                baseline_runs,
                min_baseline_rate,
                confidence,
                "baseline",
                signature_discovery=bool(
                    java_exception or python_exception or process_failure
                ),
            )
        except ValueError as exc:
            _check(checks, "baseline", "fail", str(exc))
            baseline_configuration_valid = False
    if (
        not _finite_number(timeout)
        or timeout <= 0.0
    ):
        _check(
            checks,
            "baseline",
            "fail",
            "timeout must be a finite number greater than zero",
        )
        baseline_configuration_valid = False

    if (
        oracle_requested
        and oracle_valid
        and backend_valid
        and working_directory_valid
        and baseline_configuration_valid
        and selection_valid
    ):
        try:
            runner = _runner(
                command or "",
                timeout,
                backend,
                docker_image,
                docker_network,
                docker_cpus,
                docker_memory,
                docker_pids_limit,
                docker_tmpfs_size,
                docker_workspace_limit,
                environment or {},
                java_exception,
            )
            oracle = FailureOracle(
                runner,
                FailureSpec(
                    match,
                    exit_code,
                    java_exception=java_exception,
                    python_exception=python_exception,
                    process_failure=process_failure,
                ),
                min_baseline_rate=min_baseline_rate,
                confidence=confidence,
            )
            temp_kwargs = {}
            if backend == "docker":
                temp_kwargs["dir"] = str(source.parent)
            with tempfile.TemporaryDirectory(
                prefix=".repomin-doctor-", **temp_kwargs
            ) as temporary:
                temporary_root = Path(temporary)
                attempt = 0

                def prepare() -> Path:
                    nonlocal attempt
                    attempt += 1
                    if backend == "host":
                        execution_root = temporary_root / (
                            "execution-%04d" % attempt
                        )
                        execution_root.mkdir()
                        destination = execution_root / output_path.name
                    else:
                        destination = temporary_root / (
                            "repository-%04d" % attempt
                        )
                    _copy_repository(source, destination, ignores)
                    return destination

                representative = oracle.verify_baseline(
                    temporary_root,
                    baseline_runs,
                    prepare=prepare,
                    minimum_passes=baseline_minimum,
                    minimum_rate=min_baseline_rate,
                )
            baseline = {
                "status": "pass",
                "runs": oracle.baseline_runs,
                "passes": oracle.baseline_passes,
                "minimum_passes": baseline_minimum,
                "rate": oracle.baseline_rate,
                "minimum_rate": min_baseline_rate,
                "confidence": confidence,
                "rate_evidence_runs": oracle.baseline_rate_evidence_runs,
                "rate_evidence_passes": oracle.baseline_rate_evidence_passes,
                "exact_lower_bound": oracle.baseline_exact_lower_bound,
                "exact_rate_gate_passed": oracle.baseline_exact_rate_gate_passed,
                "exit_code": representative.returncode,
                "duration_seconds": round(representative.duration_seconds, 3),
            }
            result["baseline"] = baseline
            _check(
                checks,
                "baseline",
                "pass",
                "%d/%d fresh runs reproduced the failure"
                % (oracle.baseline_passes, oracle.baseline_runs),
            )
        except (OracleError, RunnerError, OSError, ValueError) as exc:
            result["baseline"] = {"status": "fail", "message": str(exc)}
            _check(checks, "baseline", "fail", str(exc))

    ok = not any(check["status"] == "fail" for check in checks)
    result["ok"] = ok
    result["detected_adapters"] = detected_adapters
    return ok, result


def format_doctor(result: Mapping[str, object]) -> str:
    """Render a compact human-readable diagnostic without command output."""
    lines = [
        "ReproMin doctor: %s" % ("ready" if result.get("ok") else "needs attention"),
        "Source: %s" % result.get("source", ""),
    ]
    for check in result.get("checks", []):
        if not isinstance(check, dict):
            continue
        lines.append(
            "[%s] %s: %s"
            % (
                str(check.get("status", "?")).upper(),
                check.get("name", "check"),
                check.get("message", ""),
            )
        )
    baseline = result.get("baseline")
    if isinstance(baseline, dict) and baseline.get("status") == "pass":
        lines.append(
            "Baseline: %(passes)s/%(runs)s passes, exit code %(exit_code)s"
            % baseline
        )
    return "\n".join(lines) + "\n"
