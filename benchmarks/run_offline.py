#!/usr/bin/env python3
"""Run every network-free benchmark and report one pass/fail summary."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from repomin import __version__  # noqa: E402
from repomin.report import validate_report_file  # noqa: E402


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["CARGO_NET_OFFLINE"] = "true"
    env["GOPROXY"] = "off"
    return env


@contextmanager
def _run_repomin(
    fixture: str,
    adapter: str,
    command: str,
    extra: list[str],
    match_pattern: str | None = "ORIGINAL_FAILURE",
) -> Iterator[Path]:
    source = (ROOT / "benchmarks" / fixture).resolve()
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "result"
        args = [
            sys.executable,
            "-m",
            "repomin",
            str(source),
            "--command",
            command,
        ]
        if match_pattern is not None:
            args.extend(["--match", match_pattern])
        args.extend(
            [
                "--adapter",
                adapter,
                "--source-reducer",
                "none",
                *extra,
                "--output",
                str(output),
            ]
        )
        result = subprocess.run(
            args,
            cwd=str(ROOT),
            env=_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        _validate_report(output)
        yield output


def _independent(
    command: list[str],
    output: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(output),
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _replay_cli(
    report: Path,
    payload: Path,
    *,
    runs: int = 1,
) -> subprocess.CompletedProcess[str]:
    """Run the public replay command without exposing command output."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "repomin",
            "report",
            "replay",
            str(report),
            "--payload",
            str(payload),
            "--runs",
            str(runs),
            "--yes",
            "--json",
        ],
        cwd=str(ROOT),
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _validate_report(output: Path) -> None:
    """Validate the sidecar and exported payload produced by one fixture."""
    metadata = output.with_name(output.name + ".repomin")
    validate_report_file(metadata / "report.json", output)


def _check_input_controls() -> None:
    extra = [
        "--exit-code",
        "7",
        "--gitignore-recursive",
        "--keep",
        "keep-me.txt",
        "--keep",
        "kept-dir",
    ]
    with _run_repomin(
        "input-controls",
        "none",
        "python3 reproduce.py",
        extra,
        match_pattern=None,
    ) as output:
        files = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
        _require(
            files
            == {
                "reproduce.py",
                "required.txt",
                "exit-sentinel.txt",
                "keep-me.txt",
                "kept-dir/keep-nested.txt",
            },
            "input-controls produced an unexpected file set: %s" % sorted(files),
        )
        run = _independent(["python3", "reproduce.py"], output)
        _require(run.returncode == 7, "input-controls independent exit was %s" % run.returncode)


def _check_input_controls_budget() -> None:
    extra = [
        "--exit-code",
        "7",
        "--gitignore-recursive",
        "--keep",
        "keep-me.txt",
        "--keep",
        "kept-dir",
        "--max-attempts",
        "2",
    ]
    with _run_repomin(
        "input-controls",
        "none",
        "python3 reproduce.py",
        extra,
        match_pattern=None,
    ) as output:
        metadata = output.with_name(output.name + ".repomin")
        report = json.loads((metadata / "report.json").read_text(encoding="utf-8"))
        _require(report["execution"]["budget_exhausted"] is True, "budget was not exhausted")
        for name in (
            "reproduce.py",
            "required.txt",
            "exit-sentinel.txt",
            "keep-me.txt",
            "kept-dir/keep-nested.txt",
        ):
            _require((output / name).is_file(), "missing %s" % name)
        run = _independent(["python3", "reproduce.py"], output)
        _require(run.returncode == 7, "budget independent exit was %s" % run.returncode)


def _check_semantic_stub() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "benchmarks" / "semantic-stub" / "run.py")],
        cwd=str(ROOT),
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _require(result.returncode == 0, result.stderr.strip())


def _check_manifest(
    name: str,
    adapter: str,
    command: list[str],
    manifest: str,
    required: tuple[str, ...],
    forbidden: tuple[str, ...] = (),
) -> None:
    with _run_repomin(name, adapter, " ".join(command), []) as output:
        content = (output / manifest).read_text(encoding="utf-8")
        for token in required:
            _require(token in content, "%s missing %s" % (name, token))
        for token in forbidden:
            _require(token not in content, "%s still contains %s" % (name, token))
        run = _independent(command, output)
        _require(run.returncode != 0, "%s independent exit was 0" % name)
        _require(
            "ORIGINAL_FAILURE" in run.stdout + run.stderr,
            "%s independent output lacked ORIGINAL_FAILURE" % name,
        )


def _check_text_lines() -> None:
    with _run_repomin(
        "text-lines",
        "none",
        "python3 reproduce.py",
        ["--text-file", "data.txt"],
    ) as output:
        _require(
            (output / "data.txt").read_text(encoding="utf-8") == "NEEDLE\n",
            "text-lines did not reduce data.txt to the needle",
        )
        run = _independent(["python3", "reproduce.py"], output)
        _require(run.returncode == 1, "text-lines independent exit was %s" % run.returncode)
        _require(
            "ORIGINAL_FAILURE" in run.stdout + run.stderr,
            "text-lines independent output lacked failure",
        )


def _check_report_replay() -> None:
    """Exercise reduce, validate, fresh-copy replay, and a mismatch path."""
    with _run_repomin(
        "report-replay",
        "none",
        "python3 reproduce.py",
        [
            "--text-file",
            "required.txt",
            "--ignore",
            "README.md",
            "--ignore",
            "noise.txt",
        ],
    ) as output:
        files = {
            path.relative_to(output).as_posix()
            for path in output.rglob("*")
            if path.is_file()
        }
        _require(
            files == {"reproduce.py", "required.txt"},
            "report-replay produced an unexpected file set: %s" % sorted(files),
        )
        _require(
            (output / "required.txt").read_text(encoding="utf-8")
            == "REPLAY_NEEDLE\n",
            "report-replay did not reduce required.txt to the needle",
        )
        metadata = output.with_name(output.name + ".repomin")
        report_path = metadata / "report.json"
        replay = _replay_cli(report_path, output, runs=2)
        _require(
            replay.returncode == 0,
            "report-replay replay failed with exit %d" % replay.returncode,
        )
        replay_evidence = json.loads(replay.stdout)
        _require(replay_evidence.get("reproduced") is True, "replay did not reproduce")
        _require(replay_evidence.get("passes") == 2, "replay did not run twice")
        _require(
            replay_evidence.get("fresh_repository_copy_per_run") is True,
            "replay did not report fresh copies",
        )
        tampered_root = Path(tempfile.mkdtemp(prefix="repomin-replay-contract-"))
        try:
            changed_command = json.loads(report_path.read_text(encoding="utf-8"))
            changed_command["command"] = "python3 reproduce.py --different"
            changed_command_path = tampered_root / "changed-command.json"
            changed_command_path.write_text(
                json.dumps(changed_command),
                encoding="utf-8",
            )
            mismatch = _replay_cli(changed_command_path, output)
            _require(
                mismatch.returncode == 1,
                "changed failure path was not reported as a mismatch",
            )
            mismatch_evidence = json.loads(mismatch.stdout)
            _require(
                mismatch_evidence.get("reproduced") is False,
                "changed failure contract was accepted",
            )
            _require(
                mismatch_evidence["samples"][0]["mismatch_reason"] == "match",
                "unexpected mismatch reason: %s"
                % mismatch_evidence["samples"][0].get("mismatch_reason"),
            )

            changed_contract = json.loads(report_path.read_text(encoding="utf-8"))
            changed_contract["failure_spec"]["match"] = "DIFFERENT_FAILURE"
            changed_contract["failure_match"] = "DIFFERENT_FAILURE"
            changed_contract_path = tampered_root / "changed-contract.json"
            changed_contract_path.write_text(
                json.dumps(changed_contract),
                encoding="utf-8",
            )
            contract_mismatch = _replay_cli(changed_contract_path, output)
            _require(
                contract_mismatch.returncode == 1,
                "changed failure contract was not reported as a mismatch",
            )
            contract_evidence = json.loads(contract_mismatch.stdout)
            _require(
                contract_evidence.get("reproduced") is False,
                "changed failure contract was accepted",
            )
            _require(
                contract_evidence["samples"][0]["mismatch_reason"] == "match",
                "unexpected changed-contract reason: %s"
                % contract_evidence["samples"][0].get("mismatch_reason"),
            )
        finally:
            shutil.rmtree(tampered_root, ignore_errors=True)
        _require(
            not (output / ".replay-marker").exists(),
            "replay marker leaked into the exported payload",
        )


def _check_python_requirements() -> None:
    """Exercise a nested requirements include/constraint chain end to end."""
    with _run_repomin(
        "python-requirements",
        "python",
        "python3 reproduce.py",
        [],
    ) as output:
        root = (output / "requirements.txt").read_text(encoding="utf-8")
        runtime = (output / "requirements" / "runtime.txt").read_text(
            encoding="utf-8"
        )
        ci = (output / "requirements" / "ci.txt").read_text(encoding="utf-8")
        constraints = (output / "constraints.txt").read_text(encoding="utf-8")

        _require(
            "-r requirements/runtime.txt" in root,
            "python-requirements lost the runtime include",
        )
        _require(
            "-c constraints.txt" in root,
            "python-requirements lost the constraints include",
        )
        _require(
            "repomin-runtime==1.2.3" in runtime,
            "python-requirements lost the required runtime dependency",
        )
        _require(
            "--hash=sha256:" in runtime,
            "python-requirements lost the runtime hash continuation",
        )
        _require(
            "-r ci.txt" in runtime,
            "python-requirements lost the CI include",
        )
        _require(
            "repomin-ci-runner==4.5.0" in ci,
            "python-requirements lost the required CI dependency",
        )
        _require(
            "repomin-runtime<2" in constraints
            and "repomin-ci-runner<5" in constraints,
            "python-requirements lost required constraints",
        )

        all_text = "\n".join((root, runtime, ci, constraints))
        for token in (
            "repomin-unused-runtime",
            "repomin-unused-ci",
            "repomin-unused-runtime<10",
            "packages.example.invalid",
            "mirror.example.invalid",
        ):
            _require(token not in all_text, "python-requirements retained %s" % token)

        run = _independent(["python3", "reproduce.py"], output)
        _require(run.returncode == 1, "python-requirements independent exit was %s" % run.returncode)
        _require(
            "ORIGINAL_FAILURE" in run.stdout + run.stderr,
            "python-requirements independent output lacked failure",
        )


def _check_cargo_workspace() -> None:
    if shutil.which("cargo") is None:
        raise _Skipped("cargo is not installed")
    with _run_repomin(
        "cargo-workspace",
        "cargo",
        "CARGO_NET_OFFLINE=true cargo run -q -p app",
        [],
    ) as output:
        root = (output / "Cargo.toml").read_text(encoding="utf-8")
        app = (output / "app" / "Cargo.toml").read_text(encoding="utf-8")
        _require("unused" not in root, "root workspace still references unused member")
        _require("required-lib" in app, "app lost its required dependency")
        _require("unused-lib" not in app, "app still references unused-lib")
        _require((output / "required-lib" / "src" / "lib.rs").is_file(), "required-lib source missing")
        _require(not (output / "unused-lib").exists(), "unused-lib still present")
        run = _independent(["cargo", "run", "-q", "-p", "app"], output)
        _require(run.returncode != 0, "cargo independent exit was 0")
        _require("ORIGINAL_FAILURE" in run.stdout + run.stderr, "cargo output lacked failure")


def _check_go_module() -> None:
    if shutil.which("go") is None:
        raise _Skipped("go is not installed")
    with _run_repomin("go-module", "go", "GOPROXY=off go run .", []) as output:
        content = (output / "go.mod").read_text(encoding="utf-8")
        _require("example.com/required" in content, "go.mod lost required module")
        _require("example.com/unused" not in content, "go.mod still references unused module")
        _require("exclude" not in content, "go.mod still has exclude")
        _require("retract" not in content, "go.mod still has retract")
        _require((output / "required").is_dir(), "required module directory missing")
        _require(not (output / "unused").exists(), "unused module directory still present")
        run = _independent(["go", "run", "."], output)
        _require(run.returncode != 0, "go independent exit was 0")
        _require("ORIGINAL_FAILURE" in run.stdout + run.stderr, "go output lacked failure")


def _check_native_process() -> None:
    if os.name == "nt":
        raise _Skipped("native-process requires POSIX")
    extra = [
        "--process-failure",
        "--baseline-runs",
        "3",
        "--candidate-runs",
        "2",
        "--jobs",
        "1",
    ]
    with _run_repomin(
        "native-process",
        "none",
        "exec python3 crash.py",
        extra,
        match_pattern=None,
    ) as output:
        metadata = output.with_name(output.name + ".repomin")
        report = json.loads((metadata / "report.json").read_text(encoding="utf-8"))
        signature = report["process_failure_signature"]
        _require(
            signature
            == {"kind": "posix_signal", "code": int(signal.SIGABRT), "name": "SIGABRT"},
            "unexpected process signature: %s" % signature,
        )
        _require((output / "crash.py").is_file(), "crash.py missing")
        _require((output / "required.txt").is_file(), "required.txt missing")
        _require(not (output / "unused.txt").exists(), "unused.txt still present")
        run = _independent(["python3", "crash.py"], output)
        _require(run.returncode == -int(signal.SIGABRT), "independent signal was %s" % run.returncode)


class _Skipped(Exception):
    pass


def _write_summary(
    path: Path,
    checks: list[dict[str, object]],
    *,
    only: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> None:
    counts = {
        "passed": sum(item["status"] == "passed" for item in checks),
        "skipped": sum(item["status"] == "skipped" for item in checks),
        "failed": sum(item["status"] == "failed" for item in checks),
    }
    summary = {
        "schema_version": 1,
        "repomin_version": __version__,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "platform": sys.platform,
        **counts,
        "checks": checks,
        "selection": {
            "only": list(only),
            "exclude": list(exclude),
            "selected": [str(item["name"]) for item in checks],
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list",
        action="store_true",
        help="list benchmark names without executing them",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        metavar="PATH",
        help="write a machine-readable result summary to PATH",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="run only the named benchmark (repeatable)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NAME",
        help="skip the named benchmark (repeatable)",
    )
    return parser.parse_args(argv)


def _checks() -> list[tuple[str, Callable[[], None]]]:
    return [
        ("input-controls", _check_input_controls),
        ("input-controls-budget", _check_input_controls_budget),
        ("semantic-stub", _check_semantic_stub),
        ("text-lines", _check_text_lines),
        ("report-replay", _check_report_replay),
        ("python-requirements", _check_python_requirements),
        (
            "python-pyproject",
            lambda: _check_manifest(
                "python-pyproject",
                "python",
                ["python3", "reproduce.py"],
                "pyproject.toml",
                ("repomin-pyproject-fixture", "repomin-required==1.0"),
                ("repomin-unused-",),
            ),
        ),
        (
            "node-package",
            lambda: _check_manifest(
                "node-package",
                "node",
                ["python3", "reproduce.py"],
                "package.json",
                ("required-sdk", "packages/required"),
                ("unused-sdk", "unused-test-tool"),
            ),
        ),
        (
            "pipenv-package",
            lambda: _check_manifest(
                "pipenv-package",
                "pipenv",
                ["python3", "reproduce.py"],
                "Pipfile",
                ("required-package",),
                ("unused-package", "unused-test"),
            ),
        ),
        (
            "composer-package",
            lambda: _check_manifest(
                "composer-package",
                "composer",
                ["python3", "reproduce.py"],
                "composer.json",
                ("repomin/required", "autoload", "psr-4"),
            ),
        ),
        (
            "dotnet-project",
            lambda: _check_manifest(
                "dotnet-project",
                "dotnet",
                ["python3", "reproduce.py"],
                "fixture.csproj",
                ("PackageReference", "ProjectReference", "TargetFramework"),
            ),
        ),
        (
            "dotnet-directory-build-props",
            lambda: _check_manifest(
                "dotnet-directory-build-props",
                "dotnet",
                ["python3", "reproduce.py"],
                "Directory.Build.props",
                ("PackageReference", "ProjectReference", "TargetFramework"),
            ),
        ),
        (
            "ruby-gemfile",
            lambda: _check_manifest(
                "ruby-gemfile",
                "ruby",
                ["ruby", "reproduce.rb"],
                "Gemfile",
                ("repomin-required",),
            ),
        ),
        ("cargo-workspace", _check_cargo_workspace),
        ("go-module", _check_go_module),
        ("native-process", _check_native_process),
    ]


def _select_checks(
    checks: list[tuple[str, Callable[[], None]]],
    only: Sequence[str],
    exclude: Sequence[str],
) -> list[tuple[str, Callable[[], None]]]:
    available = {name for name, _ in checks}
    unknown = sorted((set(only) | set(exclude)) - available)
    if unknown:
        raise SystemExit("unknown benchmark name(s): %s" % ", ".join(unknown))
    overlap = sorted(set(only) & set(exclude))
    if overlap:
        raise SystemExit(
            "benchmark name(s) cannot be both --only and --exclude: %s"
            % ", ".join(overlap)
        )
    excluded = set(exclude)
    selected = set(only) if only else available
    filtered = [
        (name, check)
        for name, check in checks
        if name in selected and name not in excluded
    ]
    if not filtered:
        raise SystemExit("benchmark selection is empty")
    return filtered


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    checks = _checks()
    if args.list:
        if args.json_output is not None:
            raise SystemExit("--list cannot be combined with --json-output")
        for name, _ in checks:
            print(name)
        return 0
    checks = _select_checks(checks, args.only, args.exclude)
    passed = 0
    skipped = 0
    failed = 0
    results: list[dict[str, object]] = []
    for name, check in checks:
        started = time.monotonic()
        try:
            check()
            passed += 1
            results.append(
                {
                    "name": name,
                    "status": "passed",
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
            print("PASS %s" % name)
        except _Skipped as skip:
            skipped += 1
            results.append(
                {
                    "name": name,
                    "status": "skipped",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "detail": str(skip),
                }
            )
            print("SKIP %s (%s)" % (name, skip))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error": str(exc)[:2000],
                }
            )
            print("FAIL %s: %s" % (name, exc))
    if args.json_output is not None:
        try:
            _write_summary(
                args.json_output,
                results,
                only=args.only,
                exclude=args.exclude,
            )
        except OSError as exc:
            print("could not write benchmark JSON: %s" % exc, file=sys.stderr)
            failed += 1
    print(
        "offline benchmarks: %d passed, %d skipped, %d failed"
        % (passed, skipped, failed)
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
