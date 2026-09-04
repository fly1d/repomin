"""Strict reduction-spec validation and command-line expansion."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path, PureWindowsPath
from typing import List, Mapping, Sequence, Tuple

from repomin.oracle import candidate_family_confidence
from repomin.sampling import validate_rate_attainable


class ConfigError(ValueError):
    """Raised when a reduction spec or its command-line use is invalid."""


class _JSONObject(dict):
    def __init__(self, pairs: Sequence[Tuple[str, object]]) -> None:
        super().__init__()
        duplicates: List[str] = []
        for key, value in pairs:
            if key in self:
                duplicates.append(key)
            self[key] = value
        self.duplicates = duplicates


class _NonFinite:
    def __init__(self, token: str) -> None:
        self.token = token


_TOP_LEVEL_KEYS = {
    "schema_version",
    "failure",
    "execution",
    "sampling",
    "reduction",
    "inputs",
}
_FAILURE_KEYS = {"command", "match", "exit_code", "signature"}
_EXECUTION_KEYS = {"timeout_seconds", "backend", "jobs", "cache", "docker"}
_DOCKER_KEYS = {
    "image",
    "network",
    "cpus",
    "memory",
    "pids_limit",
    "tmpfs_size",
    "workspace_limit",
}
_SAMPLING_KEYS = {
    "baseline_runs",
    "min_baseline_passes",
    "candidate_runs",
    "min_candidate_passes",
    "min_baseline_rate",
    "min_candidate_rate",
    "confidence",
    "run_confidence",
    "holdout",
}
_HOLDOUT_KEYS = {"runs", "min_rate", "confidence"}
_REDUCTION_KEYS = {
    "adapter",
    "source_reducer",
    "max_attempts",
    "max_duration_seconds",
}
_INPUT_KEYS = {
    "ignore_names",
    "ignore_paths",
    "keep_paths",
    "text_files",
    "gitignore",
    "gitignore_files",
    "gitignore_recursive",
}
_SIGNATURES = {"java_exception", "python_exception", "process_failure"}
_ADAPTERS = {
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
}
_SOURCE_REDUCERS = {"auto", "none", "java", "python"}
_BYTE_SIZE = re.compile(r"^[1-9][0-9]*(?:[kmgt](?:i?b?)?|b)?$", re.IGNORECASE)
_UNSAFE_PORTABLE_COMPONENT = re.compile(r'[<>:"/\\|?*\[]|[\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *("com%d" % number for number in range(1, 10)),
    *("lpt%d" % number for number in range(1, 10)),
}


def expand_config_args(argv: Sequence[str], *, command: str) -> list[str]:
    """Replace one ``--config`` argument with validated semantic CLI options."""
    if command not in {"reduce", "doctor"}:
        raise ConfigError("command: expected 'reduce' or 'doctor'")

    arguments = list(argv)
    occurrences = _config_occurrences(arguments)
    if not occurrences:
        return arguments
    if len(occurrences) != 1:
        raise ConfigError("--config may be specified exactly once")

    config_index = occurrences[0]
    config_argument = arguments[config_index]
    if config_argument == "--config":
        if config_index + 1 >= len(arguments) or arguments[config_index + 1].startswith(
            "-"
        ):
            raise ConfigError("--config requires a path")
        config_path = arguments[config_index + 1]
        config_end = config_index + 2
    else:
        config_path = config_argument[len("--config=") :]
        config_end = config_index + 1
        if not config_path:
            raise ConfigError("--config requires a path")

    without_config = arguments[:config_index] + arguments[config_end:]
    help_limit = (
        without_config.index("--") if "--" in without_config else len(without_config)
    )
    if any(
        argument in {"-h", "--help", "--version"}
        for argument in without_config[:help_limit]
    ):
        return without_config

    _reject_mixed_options(without_config, command=command)
    document = _load_document(config_path)
    generated = _validate_and_expand(document, command=command)
    return arguments[:config_index] + generated + arguments[config_end:]


def config_option_present(argv: Sequence[str]) -> bool:
    """Return whether option parsing will treat one token as ``--config``."""
    return bool(_config_occurrences(argv))


def _config_occurrences(argv: Sequence[str]) -> List[int]:
    arguments = list(argv)
    option_limit = arguments.index("--") if "--" in arguments else len(arguments)
    return [
        index
        for index, argument in enumerate(arguments[:option_limit])
        if argument == "--config" or argument.startswith("--config=")
    ]


def _reject_mixed_options(argv: Sequence[str], *, command: str) -> None:
    value_options = {"--output"}
    flag_options = {"-h", "--help", "--version"}
    if command == "doctor":
        flag_options.add("--json")
    else:
        value_options.add("--session")
        flag_options.update({"--resume", "--verbose"})

    index = 0
    positional_only = False
    while index < len(argv):
        argument = argv[index]
        if positional_only:
            index += 1
            continue
        if argument == "--":
            positional_only = True
            index += 1
            continue
        option, separator, _value = argument.partition("=")
        if option in value_options:
            if separator:
                index += 1
                continue
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise ConfigError("%s requires a value" % option)
            index += 2
            continue
        if option in flag_options and not separator:
            index += 1
            continue
        if argument.startswith("-") and argument != "-":
            raise ConfigError("%s cannot be combined with --config" % option)
        index += 1


def _load_document(value: str) -> _JSONObject:
    try:
        path = Path(value).expanduser()
        source = path.read_text(encoding="utf-8")
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise ConfigError("--config %s: cannot read file: %s" % (value, exc)) from exc

    try:
        document = json.loads(
            source,
            object_pairs_hook=_JSONObject,
            parse_constant=_NonFinite,
        )
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "--config %s: invalid JSON at line %d column %d"
            % (value, exc.lineno, exc.colno)
        ) from exc
    except ValueError as exc:
        raise ConfigError("--config %s: invalid JSON: %s" % (value, exc)) from exc
    except RecursionError as exc:
        raise ConfigError("--config %s: JSON nesting is too deep" % value) from exc
    try:
        _reject_json_extensions(document, "config")
    except RecursionError as exc:
        raise ConfigError("--config %s: JSON nesting is too deep" % value) from exc
    return _object(document, "config")


def _reject_json_extensions(value: object, path: str) -> None:
    if isinstance(value, _NonFinite):
        raise ConfigError("%s: number must be finite (got %s)" % (path, value.token))
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError("%s: number must be finite" % path)
    if isinstance(value, _JSONObject):
        if value.duplicates:
            raise ConfigError("%s: duplicate key %r" % (path, value.duplicates[0]))
        for key, child in value.items():
            _reject_json_extensions(child, _child_path(path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_json_extensions(child, "%s[%d]" % (path, index))


def _validate_and_expand(document: Mapping[str, object], *, command: str) -> List[str]:
    _exact_keys(document, _TOP_LEVEL_KEYS, "config")
    _required(document, "schema_version", "config")
    _required(document, "failure", "config")
    schema_version = document["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise ConfigError("schema_version: expected integer 1")

    failure = _validate_failure(document["failure"])
    execution = (
        _validate_execution(document["execution"]) if "execution" in document else {}
    )
    sampling = (
        _validate_sampling(
            document["sampling"],
            signature_discovery="signature" in failure,
        )
        if "sampling" in document
        else {}
    )
    reduction = (
        _validate_reduction(document["reduction"]) if "reduction" in document else {}
    )
    inputs = _validate_inputs(document["inputs"]) if "inputs" in document else {}

    result: List[str] = []
    _expand_failure(result, failure)
    _expand_execution(result, execution, command=command)
    _expand_sampling(result, sampling, command=command)
    _expand_reduction(result, reduction, command=command)
    _expand_inputs(result, inputs)
    return result


def _validate_failure(value: object) -> Mapping[str, object]:
    failure = _object(value, "failure")
    _exact_keys(failure, _FAILURE_KEYS, "failure")
    _required(failure, "command", "failure")
    _nonempty_text(failure["command"], "failure.command")
    if "match" in failure:
        _nonempty_text(failure["match"], "failure.match")
    if "exit_code" in failure:
        _integer(failure["exit_code"], "failure.exit_code")
    if "signature" in failure:
        _choice(failure["signature"], _SIGNATURES, "failure.signature")

    process_failure = failure.get("signature") == "process_failure"
    if "match" not in failure and "exit_code" not in failure and not process_failure:
        raise ConfigError(
            "failure: one of match, exit_code, or signature=process_failure is required"
        )
    if process_failure and "exit_code" in failure:
        raise ConfigError("failure: process_failure is incompatible with exit_code")
    return failure


def _validate_execution(value: object) -> Mapping[str, object]:
    execution = _object(value, "execution")
    _exact_keys(execution, _EXECUTION_KEYS, "execution")
    if "timeout_seconds" in execution:
        _positive_number(execution["timeout_seconds"], "execution.timeout_seconds")
    if "backend" in execution:
        _choice(execution["backend"], {"host", "docker"}, "execution.backend")
    if "jobs" in execution:
        _positive_integer(execution["jobs"], "execution.jobs")
    if "cache" in execution:
        _boolean(execution["cache"], "execution.cache")

    docker_value = execution.get("docker")
    if "docker" in execution:
        docker = _object(docker_value, "execution.docker")
        _exact_keys(docker, _DOCKER_KEYS, "execution.docker")
        if "image" in docker:
            _nonempty_text(docker["image"], "execution.docker.image")
        if "network" in docker:
            _choice(
                docker["network"],
                {"none", "bridge", "host"},
                "execution.docker.network",
            )
        if "cpus" in docker:
            _positive_number(docker["cpus"], "execution.docker.cpus")
        for key in ("memory", "tmpfs_size", "workspace_limit"):
            if key in docker:
                _byte_size(docker[key], "execution.docker.%s" % key)
        if "pids_limit" in docker:
            _positive_integer(docker["pids_limit"], "execution.docker.pids_limit")

    backend = execution.get("backend", "host")
    if backend != "docker" and "docker" in execution:
        raise ConfigError("execution.docker requires execution.backend=docker")
    if backend == "docker":
        if "docker" not in execution or "image" not in docker_value:
            raise ConfigError(
                "execution.backend=docker requires execution.docker.image"
            )
    return execution


def _validate_sampling(
    value: object,
    *,
    signature_discovery: bool,
) -> Mapping[str, object]:
    sampling = _object(value, "sampling")
    _exact_keys(sampling, _SAMPLING_KEYS, "sampling")
    for key in (
        "baseline_runs",
        "min_baseline_passes",
        "candidate_runs",
        "min_candidate_passes",
    ):
        if key in sampling:
            _positive_integer(sampling[key], "sampling.%s" % key)
    for key in (
        "min_baseline_rate",
        "min_candidate_rate",
        "confidence",
        "run_confidence",
    ):
        if key in sampling:
            _rate(sampling[key], "sampling.%s" % key)

    baseline_runs = sampling.get("baseline_runs", 2)
    baseline_passes = sampling.get("min_baseline_passes")
    if baseline_passes is not None and baseline_passes > baseline_runs:
        raise ConfigError(
            "sampling.min_baseline_passes must not exceed sampling.baseline_runs"
        )
    candidate_runs = sampling.get("candidate_runs", 1)
    candidate_passes = sampling.get("min_candidate_passes")
    if candidate_passes is not None and candidate_passes > candidate_runs:
        raise ConfigError(
            "sampling.min_candidate_passes must not exceed sampling.candidate_runs"
        )
    if "run_confidence" in sampling and "min_candidate_rate" not in sampling:
        raise ConfigError(
            "sampling.run_confidence requires sampling.min_candidate_rate"
        )

    if "holdout" in sampling:
        holdout = _object(sampling["holdout"], "sampling.holdout")
        _exact_keys(holdout, _HOLDOUT_KEYS, "sampling.holdout")
        for key in ("runs", "min_rate"):
            _required(holdout, key, "sampling.holdout")
        _positive_integer(holdout["runs"], "sampling.holdout.runs")
        _rate(holdout["min_rate"], "sampling.holdout.min_rate")
        if "confidence" in holdout:
            _rate(holdout["confidence"], "sampling.holdout.confidence")
    confidence = sampling.get("confidence", 0.95)
    try:
        validate_rate_attainable(
            baseline_runs,
            sampling.get("min_baseline_rate"),
            confidence,
            "baseline",
            signature_discovery=signature_discovery,
        )
        if "run_confidence" in sampling:
            family_confidence, _alpha = candidate_family_confidence(
                confidence,
                sampling["run_confidence"],
                1,
            )
            validate_rate_attainable(
                candidate_runs,
                sampling.get("min_candidate_rate"),
                family_confidence,
                "candidate",
            )
        validate_rate_attainable(
            candidate_runs,
            sampling.get("min_candidate_rate"),
            confidence,
            "candidate",
        )
        if "holdout" in sampling:
            holdout = sampling["holdout"]
            assert isinstance(holdout, Mapping)
            validate_rate_attainable(
                holdout["runs"],
                holdout["min_rate"],
                holdout.get("confidence", 0.95),
                "holdout",
            )
    except ValueError as exc:
        raise ConfigError("sampling: %s" % exc) from exc
    return sampling


def _validate_reduction(value: object) -> Mapping[str, object]:
    reduction = _object(value, "reduction")
    _exact_keys(reduction, _REDUCTION_KEYS, "reduction")
    if "adapter" in reduction:
        _choice(reduction["adapter"], _ADAPTERS, "reduction.adapter")
    if "source_reducer" in reduction:
        _choice(
            reduction["source_reducer"],
            _SOURCE_REDUCERS,
            "reduction.source_reducer",
        )
    if "max_attempts" in reduction:
        _positive_integer(reduction["max_attempts"], "reduction.max_attempts")
    if "max_duration_seconds" in reduction:
        _positive_number(
            reduction["max_duration_seconds"],
            "reduction.max_duration_seconds",
        )
    return reduction


def _validate_inputs(value: object) -> Mapping[str, object]:
    inputs = _object(value, "inputs")
    _exact_keys(inputs, _INPUT_KEYS, "inputs")
    if "ignore_names" in inputs:
        values = _string_list(inputs["ignore_names"], "inputs.ignore_names")
        for index, item in enumerate(values):
            _portable_basename(item, "inputs.ignore_names[%d]" % index)
    for key in ("ignore_paths", "keep_paths", "text_files", "gitignore_files"):
        if key in inputs:
            values = _string_list(inputs[key], "inputs.%s" % key)
            for index, item in enumerate(values):
                _portable_relative_path(item, "inputs.%s[%d]" % (key, index))
    for key in ("gitignore", "gitignore_recursive"):
        if key in inputs:
            _boolean(inputs[key], "inputs.%s" % key)
    return inputs


def _expand_failure(result: List[str], failure: Mapping[str, object]) -> None:
    result.append("--command=%s" % failure["command"])
    if "match" in failure:
        result.append("--match=%s" % failure["match"])
    if "exit_code" in failure:
        result.append("--exit-code=%s" % failure["exit_code"])
    signature = failure.get("signature")
    if signature is not None:
        result.append("--%s" % str(signature).replace("_", "-"))


def _expand_execution(
    result: List[str], execution: Mapping[str, object], *, command: str
) -> None:
    if "timeout_seconds" in execution:
        result.append("--timeout=%s" % execution["timeout_seconds"])
    if "backend" in execution:
        result.append("--backend=%s" % execution["backend"])
    docker = execution.get("docker")
    if isinstance(docker, Mapping):
        if "image" in docker:
            result.append("--docker-image=%s" % docker["image"])
        if "network" in docker:
            result.append("--docker-network=%s" % docker["network"])
        docker_options = (
            ("cpus", "--docker-cpus"),
            ("memory", "--docker-memory"),
            ("pids_limit", "--docker-pids-limit"),
            ("tmpfs_size", "--docker-tmpfs-size"),
            ("workspace_limit", "--docker-workspace-limit"),
        )
        for key, option in docker_options:
            if key in docker:
                result.append("%s=%s" % (option, docker[key]))
    if command == "reduce":
        if "jobs" in execution:
            result.append("--jobs=%s" % execution["jobs"])
        if execution.get("cache") is False:
            result.append("--no-cache")


def _expand_sampling(
    result: List[str], sampling: Mapping[str, object], *, command: str
) -> None:
    if "baseline_runs" in sampling:
        result.append("--baseline-runs=%s" % sampling["baseline_runs"])
    for key, option in (
        ("min_baseline_passes", "--min-baseline-passes"),
        ("min_baseline_rate", "--min-baseline-rate"),
        ("confidence", "--confidence"),
    ):
        if key in sampling:
            result.append("%s=%s" % (option, sampling[key]))
    if command == "doctor":
        return
    options = (
        ("candidate_runs", "--candidate-runs"),
        ("min_candidate_passes", "--min-candidate-passes"),
        ("min_candidate_rate", "--min-candidate-rate"),
        ("run_confidence", "--run-confidence"),
    )
    for key, option in options:
        if key in sampling:
            result.append("%s=%s" % (option, sampling[key]))
    holdout = sampling.get("holdout")
    if isinstance(holdout, Mapping):
        result.append("--holdout-runs=%s" % holdout["runs"])
        result.append("--min-holdout-rate=%s" % holdout["min_rate"])
        if "confidence" in holdout:
            result.append("--holdout-confidence=%s" % holdout["confidence"])


def _expand_reduction(
    result: List[str], reduction: Mapping[str, object], *, command: str
) -> None:
    for key, option in (
        ("adapter", "--adapter"),
        ("source_reducer", "--source-reducer"),
    ):
        if key in reduction:
            result.append("%s=%s" % (option, reduction[key]))
    if command == "reduce":
        for key, option in (
            ("max_attempts", "--max-attempts"),
            ("max_duration_seconds", "--max-duration"),
        ):
            if key in reduction:
                result.append("%s=%s" % (option, reduction[key]))


def _expand_inputs(result: List[str], inputs: Mapping[str, object]) -> None:
    list_options = (
        ("ignore_names", "--ignore"),
        ("ignore_paths", "--ignore-path"),
        ("keep_paths", "--keep"),
        ("text_files", "--text-file"),
        ("gitignore_files", "--gitignore-file"),
    )
    for key, option in list_options:
        values = inputs.get(key, [])
        if isinstance(values, list):
            result.extend("%s=%s" % (option, value) for value in values)
    if inputs.get("gitignore") is True:
        result.append("--gitignore")
    if inputs.get("gitignore_recursive") is True:
        result.append("--gitignore-recursive")


def _object(value: object, path: str) -> _JSONObject:
    if not isinstance(value, _JSONObject):
        raise ConfigError("%s: expected an object" % path)
    return value


def _exact_keys(value: Mapping[str, object], allowed: set, path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError("%s: unknown key %r" % (path, unknown[0]))


def _required(value: Mapping[str, object], key: str, path: str) -> None:
    if key not in value:
        raise ConfigError("%s.%s: required" % (path, key))


def _nonempty_text(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ConfigError("%s: expected a string" % path)
    if not value.strip():
        raise ConfigError("%s: must not be empty" % path)
    if "\x00" in value:
        raise ConfigError("%s: must not contain NUL" % path)
    return value


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("%s: expected an integer" % path)
    return value


def _positive_integer(value: object, path: str) -> int:
    integer = _integer(value, path)
    if integer <= 0:
        raise ConfigError("%s: must be a positive integer" % path)
    return integer


def _positive_number(value: object, path: str) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s: expected a number" % path)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value <= 0:
        raise ConfigError("%s: must be a positive finite number" % path)
    return value


def _rate(value: object, path: str) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s: expected a number" % path)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value <= 0 or value >= 1:
        raise ConfigError("%s: must be a finite number in (0, 1)" % path)
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("%s: expected a boolean" % path)
    return value


def _choice(value: object, choices: set, path: str) -> str:
    text = _nonempty_text(value, path)
    if text not in choices:
        raise ConfigError("%s: expected one of %s" % (path, ", ".join(sorted(choices))))
    return text


def _byte_size(value: object, path: str) -> str:
    text = _nonempty_text(value, path)
    if _BYTE_SIZE.fullmatch(text.strip()) is None:
        raise ConfigError("%s: expected a positive byte-size string" % path)
    return text


def _string_list(value: object, path: str) -> List[str]:
    if not isinstance(value, list):
        raise ConfigError("%s: expected a list" % path)
    result: List[str] = []
    for index, item in enumerate(value):
        result.append(_nonempty_text(item, "%s[%d]" % (path, index)))
    return result


def _portable_basename(value: str, path: str) -> None:
    if value != value.strip() or value in {".", ".."}:
        raise ConfigError("%s: expected a portable basename" % path)
    _portable_component(value, path)


def _portable_relative_path(value: str, path: str) -> None:
    if value != value.strip() or value.startswith("/") or "\\" in value:
        raise ConfigError("%s: expected a portable repository-relative path" % path)
    if PureWindowsPath(value).drive:
        raise ConfigError("%s: expected a portable repository-relative path" % path)
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ConfigError("%s: expected a portable repository-relative path" % path)
    for component in components:
        _portable_component(component, path)


def _portable_component(value: str, path: str) -> None:
    stem = value.split(".", 1)[0].casefold()
    if (
        _UNSAFE_PORTABLE_COMPONENT.search(value)
        or value.endswith((" ", "."))
        or stem in _WINDOWS_RESERVED_NAMES
    ):
        raise ConfigError("%s: expected a portable repository-relative path" % path)


def _child_path(path: str, key: str) -> str:
    return key if path == "config" else "%s.%s" % (path, key)
