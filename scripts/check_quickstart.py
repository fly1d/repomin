#!/usr/bin/env python3
"""Check first-run documentation and exercise the installed CLI journey."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TARGET_FAILURE = "AssertionError: checkout total mismatch"
_SHELL_FENCE = re.compile(
    r"^[ \t]*```(?:bash|console|powershell|pwsh|sh|shell|zsh)[ \t]*\n"
    r"(.*?)^[ \t]*```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def _shell_code(document: str) -> str:
    """Return shell code only, with POSIX and PowerShell continuations joined."""
    blocks = _SHELL_FENCE.findall(document)
    return "\n".join(re.sub(r"(?:\\|`)[ \t]*\n[ \t]*", " ", block) for block in blocks)


def _find_command(
    label: str,
    code: str,
    parts: tuple[str, ...],
    start: int = 0,
) -> tuple[int, str]:
    cursor = start
    for line in code[start:].splitlines(keepends=True):
        try:
            tokens = _repomin_tokens(label, line)
        except RuntimeError:
            cursor += len(line)
            continue
        if tokens[: len(parts)] == list(parts):
            return cursor, line.rstrip("\r\n")
        cursor += len(line)
    raise RuntimeError(
        "%s is missing a fenced-code command: %s" % (label, " ".join(parts))
    )


def _ordered_commands(
    label: str, code: str, commands: tuple[tuple[str, ...], ...]
) -> list[str]:
    result = []
    cursor = 0
    for command in commands:
        position, line = _find_command(label, code, command, cursor)
        result.append(line)
        cursor = position + len(line)
    return result


def _repomin_tokens(label: str, command: str) -> list[str]:
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError as exc:
        raise RuntimeError("%s contains an invalid shell command" % label) from exc
    try:
        start = tokens.index("repomin")
    except ValueError as exc:
        raise RuntimeError("%s does not invoke repomin" % label) from exc
    return tokens[start:]


def _option_value(label: str, command: str, option: str) -> str:
    tokens = _repomin_tokens(label, command)
    values = []
    for index, token in enumerate(tokens):
        if token == option:
            if index + 1 >= len(tokens):
                raise RuntimeError("%s has no value for %s" % (label, option))
            values.append(tokens[index + 1])
        elif token.startswith(option + "="):
            values.append(token.split("=", 1)[1])
    if len(values) != 1:
        raise RuntimeError("%s must set %s exactly once" % (label, option))
    return values[0]


def _shell_assignments(code: str) -> dict[str, str]:
    assignments = {}
    for line in code.splitlines():
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue
        if len(tokens) != 1 or "=" not in tokens[0]:
            continue
        name, value = tokens[0].split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            assignments[name] = value
    return assignments


def _resolve_shell_value(value: str, assignments: dict[str, str]) -> str:
    match = re.fullmatch(
        r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))",
        value,
    )
    if match is None:
        return value
    return assignments.get(match.group(1) or match.group(2), value)


def _check_failure_contract(
    label: str,
    doctor: str,
    reduction: str,
    assignments: dict[str, str],
    *,
    target_failure: str = TARGET_FAILURE,
    shared_options: tuple[str, ...] = (
        "--command",
        "--match",
        "--exit-code",
        "--output",
    ),
) -> None:
    values = {}
    for option in shared_options:
        doctor_value = _resolve_shell_value(
            _option_value(label + " Doctor", doctor, option), assignments
        )
        reduction_value = _resolve_shell_value(
            _option_value(label + " reduction", reduction, option), assignments
        )
        if doctor_value != reduction_value:
            raise RuntimeError(
                "%s Doctor and reduction differ for %s" % (label, option)
            )
        values[option] = doctor_value
    if values["--match"] != target_failure or values["--exit-code"] != "1":
        raise RuntimeError(
            "%s does not use the strict documented failure contract" % label
        )
    for option in ("--max-attempts", "--max-duration"):
        value = _resolve_shell_value(
            _option_value(label + " reduction", reduction, option), assignments
        )
        try:
            positive = float(value) > 0
        except ValueError:
            positive = False
        if not positive:
            raise RuntimeError("%s reduction has an invalid %s" % (label, option))


def _require_release_install(label: str, document: str) -> None:
    if not any(
        "pip install" in block
        and "github.com/fly1d/repomin/releases/download/v" in block
        for block in _SHELL_FENCE.findall(document)
    ):
        raise RuntimeError("%s is missing the versioned release installation" % label)


def _check_real_failure_guide(label: str, document: str) -> None:
    """Check one translated real-failure guide's executable journey."""
    _require_release_install(label, document)
    code = _shell_code(document)
    doctor, reduction, _, _ = _ordered_commands(
        label,
        code,
        (
            ("repomin", "doctor"),
            ("repomin", "reduce"),
            ("repomin", "report", "validate"),
            ("repomin", "report", "replay"),
        ),
    )
    _check_failure_contract(label, doctor, reduction, _shell_assignments(code))


def _check_documented_journey() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quickstart = (ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    windows = (ROOT / "docs" / "QUICKSTART.windows.md").read_text(encoding="utf-8")
    chinese = (ROOT / "docs" / "QUICKSTART.zh-CN.md").read_text(encoding="utf-8")

    readme_code = _shell_code(readme)
    _, readme_doctor, readme_reduce, _ = _ordered_commands(
        "README",
        readme_code,
        (
            ("repomin", "demo"),
            ("repomin", "doctor"),
            ("repomin", "reduce"),
            ("repomin", "report", "validate"),
        ),
    )
    _check_failure_contract(
        "README", readme_doctor, readme_reduce, _shell_assignments(readme_code)
    )

    _check_real_failure_guide("quick start", quickstart)
    _check_real_failure_guide("Chinese quick start", chinese)

    if (
        "docs/QUICKSTART.windows.md" not in readme
        or "docs/QUICKSTART.zh-CN.md" not in readme
    ):
        raise RuntimeError("README must route readers to the Windows and Chinese tours")
    if "QUICKSTART.windows.md" not in quickstart:
        raise RuntimeError(
            "quick start must route Windows readers to the PowerShell tour"
        )
    if "QUICKSTART.md" not in windows:
        raise RuntimeError(
            "Windows walkthrough must route real failures to the quick start"
        )

    _require_release_install("Windows walkthrough", windows)
    windows_code = _shell_code(windows)
    windows_doctor, windows_reduce, _, _ = _ordered_commands(
        "Windows walkthrough",
        windows_code,
        (
            ("repomin", "doctor"),
            ("repomin", "reduce"),
            ("repomin", "report", "validate"),
            ("repomin", "report", "replay"),
        ),
    )
    _check_failure_contract(
        "Windows walkthrough",
        windows_doctor,
        windows_reduce,
        {},
        target_failure="ORIGINAL_FAILURE",
        shared_options=("--command", "--match", "--exit-code"),
    )
    _option_value("Windows Doctor", windows_doctor, "--output")
    _option_value("Windows reduction", windows_reduce, "--output")


def _command(interpreter: str, script: str) -> str:
    values = [interpreter, script]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "repomin", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "repomin %s failed with exit code %d\nstdout:\n%s\nstderr:\n%s"
            % (
                " ".join(arguments),
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
        )
    return completed


def _json_object(
    label: str, completed: subprocess.CompletedProcess[str]
) -> dict[str, object]:
    try:
        value = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("%s did not emit valid JSON" % label) from exc
    if not isinstance(value, dict):
        raise RuntimeError("%s did not emit a JSON object" % label)
    return value


def _check_replay_result(
    label: str, completed: subprocess.CompletedProcess[str]
) -> None:
    result = _json_object(label, completed)
    if result.get("runs") != 2 or result.get("passes") != 2:
        raise RuntimeError("%s did not preserve the failure in 2/2 fresh runs" % label)


def main() -> int:
    _check_documented_journey()
    with tempfile.TemporaryDirectory(prefix="repomin-quickstart-") as directory:
        root = Path(directory)
        demo_workspace = root / "demo & (portable)"
        demo = _run("demo", str(demo_workspace))
        if not demo.stdout.startswith("ReproMin demo completed.\n"):
            raise RuntimeError("demo did not emit its completion receipt")
        if (demo_workspace / "reduced" / "input.txt").read_text(
            encoding="utf-8"
        ) != "NEEDLE\n":
            raise RuntimeError("demo did not export the expected minimized input")
        demo_report = demo_workspace / "reduced.repomin" / "report.json"
        demo_replay = _run(
            "report",
            "replay",
            str(demo_report),
            "--payload",
            str(demo_workspace / "reduced"),
            "--runs",
            "2",
            "--yes",
            "--json",
        )
        _check_replay_result("demo replay", demo_replay)

        source = root / "case"
        output = root / "reduced"
        source.mkdir()
        (source / "reproduce.py").write_text(
            """from pathlib import Path
import sys

text = Path(\"input.txt\").read_text(encoding=\"utf-8\")
if \"keep-me\" not in text:
    print(\"DIFFERENT_FAILURE\", file=sys.stderr)
    raise SystemExit(2)

print(\"AssertionError: checkout total mismatch\", file=sys.stderr)
raise SystemExit(1)
""",
            encoding="utf-8",
        )
        (source / "input.txt").write_text("keep-me\nremove-me\n", encoding="utf-8")
        (source / "unused.txt").write_text("unrelated file\n", encoding="utf-8")
        reproduce = _command(sys.executable, "reproduce.py")

        doctor = _run(
            "doctor",
            str(source),
            "--command",
            reproduce,
            "--match",
            TARGET_FAILURE,
            "--exit-code",
            "1",
            "--text-file",
            "input.txt",
            "--output",
            str(output),
            "--json",
        )
        doctor_result = _json_object("Doctor", doctor)
        baseline = doctor_result.get("baseline")
        if (
            doctor_result.get("ok") is not True
            or not isinstance(baseline, dict)
            or baseline.get("status") != "pass"
            or baseline.get("runs") != 2
            or baseline.get("passes") != 2
        ):
            raise RuntimeError("Doctor did not verify a 2/2 fresh-copy baseline")

        _run(
            "reduce",
            str(source),
            "--command",
            reproduce,
            "--match",
            TARGET_FAILURE,
            "--exit-code",
            "1",
            "--adapter",
            "none",
            "--source-reducer",
            "none",
            "--text-file",
            "input.txt",
            "--max-attempts",
            "25",
            "--max-duration",
            "300",
            "--output",
            str(output),
        )

        payload_files = sorted(
            path.relative_to(output).as_posix()
            for path in output.rglob("*")
            if path.is_file()
        )
        if payload_files != ["input.txt", "reproduce.py"]:
            raise RuntimeError("unexpected reduced payload: %r" % payload_files)
        if (output / "input.txt").read_text(encoding="utf-8") != "keep-me\n":
            raise RuntimeError("text reduction did not remove the unrelated line")

        report = root / "reduced.repomin" / "report.json"
        validation = _run(
            "report",
            "validate",
            str(report),
            "--payload",
            str(output),
            "--json",
        )
        validation_result = _json_object("report validation", validation)
        if (
            validation_result.get("valid") is not True
            or validation_result.get("payload_fingerprint_verified") is not True
            or validation_result.get("payload_fingerprint_mode") != "exact"
        ):
            raise RuntimeError(
                "validation did not verify a valid payload with an exact fingerprint"
            )

        replay = _run(
            "report",
            "replay",
            str(report),
            "--payload",
            str(output),
            "--runs",
            "2",
            "--yes",
            "--json",
        )
        _check_replay_result("report replay", replay)

    print("Quick-start smoke passed: demo, Doctor, reduce, validate, and 2/2 replay.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print("Quick-start smoke failed: %s" % exc, file=sys.stderr)
        raise SystemExit(1)
