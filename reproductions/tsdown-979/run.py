#!/usr/bin/env python3
"""Re-run an explicitly prepared tsdown case using released ReproMin dev14.

This executes the reviewed fixture on the host. It is not a sandbox, a
historical-snapshot recovery, or an independent-user trial.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import venv
from datetime import datetime, timezone


KIT = Path(__file__).resolve().parent
WHEEL = "repomin-0.1.0.dev14-py3-none-any.whl"
WHEEL_SHA256 = "763be17e31d0398e204c7fc5b911097ea77a7aab599da4512d53da2f55bd5267"
COMMAND = ('pnpm install --offline --frozen-lockfile --ignore-scripts '
           '--store-dir "$REPOMIN_CASE_STORE" && node reproduce.mjs')
MARKER = "DUPLICATED_CSS_MODULE_COMPOSITION"
VERIFIED_NODES = {"v24.19.0", "v26.5.0"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def preflight(workspace: Path = None) -> dict:
    """Check local requirements and fixture bytes, without creating or downloading."""
    if os.name != "posix":
        raise RuntimeError("This recipe is for macOS/Linux POSIX shells only.")
    if sys.version_info < (3, 9):
        raise RuntimeError("Python 3.9+ is required.")
    if importlib.util.find_spec("ensurepip") is None:
        raise RuntimeError("This Python lacks ensurepip. Use a Python installation with venv/ensurepip.")
    for executable in ("node", "npm"):
        if not shutil.which(executable):
            raise RuntimeError(executable + " must already be installed.")
    provenance = json.loads((KIT / "provenance.json").read_text())
    fixture = KIT / "prepared-input"
    fixture_entries = list(fixture.rglob("*"))
    if any(entry.is_symlink() for entry in fixture_entries):
        raise RuntimeError("The prepared fixture must not contain symlinks.")
    actual_paths = {entry.relative_to(fixture).as_posix()
                    for entry in fixture_entries if entry.is_file()}
    if actual_paths != {entry["path"] for entry in provenance["prepared_files"]}:
        raise RuntimeError("The prepared fixture file set changed.")
    for entry in provenance["prepared_files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Invalid fixture path")
        source_file = KIT / "prepared-input" / relative
        if source_file.is_symlink() or digest(source_file) != entry["sha256"]:
            raise RuntimeError("Fixture bytes changed: " + str(relative))
    if not (KIT / WHEEL).is_file():
        raise RuntimeError("The dev14 wheel is missing. Download the existing release asset as shown in README.md.")
    if digest(KIT / WHEEL) != WHEEL_SHA256:
        raise RuntimeError("The bundled published wheel checksum does not match.")
    versions = {}
    for executable in ("node", "npm"):
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, timeout=15, check=False)
        if result.returncode:
            raise RuntimeError(executable + " --version failed. Check your local runtime installation.")
        versions[executable] = result.stdout.strip()
    if versions["node"] not in VERIFIED_NODES:
        raise RuntimeError("Use a verified Node version: 24.19.0 or 26.5.0. "
                           "Detected " + versions["node"] + ". No workspace was created.")
    if workspace is not None:
        if workspace.exists() or workspace.is_symlink():
            raise RuntimeError("The workspace already exists. Choose a new directory; existing work is kept.")
        if not workspace.parent.is_dir():
            raise RuntimeError("The workspace parent does not exist. Choose an existing parent directory.")
    return {"status": "ready", "python": platform.python_version(), **versions,
            "fixture_files_verified": len(provenance["prepared_files"]),
            "wheel_sha256_verified": True, "workspace_created": False,
            "dependencies_downloaded": False}


def run_case(workspace: Path) -> None:
    preflight(workspace)
    provenance = json.loads((KIT / "provenance.json").read_text())

    # mkdir without exist_ok refuses reuse rather than deleting prior work.
    workspace.mkdir(mode=0o700)
    workspace = workspace.resolve()
    logs = workspace / "private-logs"
    logs.mkdir()
    source = workspace / "input"
    payload = workspace / "reduced"
    shutil.copytree(KIT / "prepared-input", source)
    runtime = workspace / "runtime"
    runtime.mkdir()
    store = workspace / "pnpm-store"
    store.mkdir()
    user_config = runtime / "empty-user.npmrc"
    global_config = runtime / "empty-global.npmrc"
    user_config.write_text("")
    global_config.write_text("")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "en_US.UTF-8",
        "TERM": "dumb",
        "REPOMIN_CASE_STORE": str(store),
        "NPM_CONFIG_USERCONFIG": str(user_config),
        "NPM_CONFIG_GLOBALCONFIG": str(global_config),
        "NPM_CONFIG_CACHE": str(runtime / "npm-cache"),
        "PIP_CONFIG_FILE": os.devnull,
        "NO_COLOR": "1",
    }
    # Preserve the user's existing network route and CA configuration only.
    # No registry tokens, cloud credentials or private service keys are copied.
    for key in ("TMPDIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "all_proxy", "no_proxy",
                "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
                "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    execution = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
                 "status": "running", "platform": platform.system(),
                 "architecture": platform.machine(), "python": platform.python_version(),
                 "source_identity": provenance["identity"], "steps": [],
                 "independent_user_trial": False, "external_publication": False}

    def execute(label: str, args: list[str], *, cwd: Path = workspace,
                limit: int = 120, expected: tuple[int, ...] = (0,)) -> tuple[str, int]:
        stdout_path = logs / (label + ".stdout")
        stderr_path = logs / (label + ".stderr")
        begin = time.monotonic()
        print("Running " + label, flush=True)
        with stdout_path.open("x") as out, stderr_path.open("x") as err:
            proc = subprocess.Popen(args, cwd=cwd, env=environment, stdout=out,
                                    stderr=err, text=True, start_new_session=True)
            try:
                code = proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                execution["steps"].append({"name": label, "timeout": True,
                    "elapsed_seconds": round(time.monotonic() - begin, 3)})
                raise RuntimeError(label + " exceeded its time limit")
        execution["steps"].append({"name": label, "returncode": code,
            "elapsed_seconds": round(time.monotonic() - begin, 3)})
        text = stdout_path.read_text(errors="replace")
        if code not in expected:
            raise RuntimeError(label + " failed; inspect private-logs locally")
        return text, code

    try:
        node, _ = execute("node-version", ["node", "--version"])
        execution["node"] = node.strip()
        if node.strip() not in VERIFIED_NODES:
            raise RuntimeError("The Node version changed after preflight. Use Node24.19.0 or26.5.0.")
        npm, _ = execute("npm-version", ["npm", "--version"])
        execution["npm"] = npm.strip()
        execute("install-pnpm", ["npm", "install", "--prefix", str(runtime),
            "--package-lock=false", "--ignore-scripts", "--no-audit", "--no-fund",
            "--registry=https://registry.npmjs.org", "pnpm@11.8.0"], limit=180)
        environment["PATH"] = str(runtime / "node_modules/.bin") + os.pathsep + environment["PATH"]
        pnpm, _ = execute("pnpm-version-in-fixture", ["pnpm", "--version"], cwd=source)
        if pnpm.strip() != "11.8.0":
            raise RuntimeError("Unexpected pnpm version inside fixture")
        execution["pnpm"] = pnpm.strip()
        execute("warm-dependencies", ["pnpm", "install", "--frozen-lockfile",
            "--ignore-scripts", "--store-dir", str(store)], cwd=source, limit=240)

        tool_environment = runtime / "repomin-env"
        venv.EnvBuilder(with_pip=True).create(tool_environment)
        python = tool_environment / "bin/python"
        tool = tool_environment / "bin/repomin"
        execute("install-released-wheel", [str(python), "-m", "pip", "install",
            "--no-index", "--no-deps", str(KIT / WHEEL)])
        version, _ = execute("repomin-version", [str(tool), "--version"])
        if version.strip() != "repomin 0.1.0.dev14":
            raise RuntimeError("Unexpected ReproMin version")
        execution["repomin"] = "0.1.0.dev14"

        # Only the input fixture is used as input. No reduced source file is seeded.
        common = [str(source), "--command", COMMAND, "--match-text", MARKER,
            "--exit-code", "23", "--adapter", "none", "--source-reducer", "none",
            "--text-file", "src/index.ts", "--timeout", "30",
            "--ignore", "node_modules", "--ignore", "dist",
            "--keep", "reproduce.mjs", "--keep", "LICENSE",
            "--keep", "package.json", "--keep", "pnpm-lock.yaml",
            "--output", str(payload)]
        execute("doctor", [str(tool), "doctor", *common, "--json"], limit=120)
        execute("reduce", [str(tool), "reduce", *common,
            "--semantic-reducer", "none", "--jobs", "2",
            "--max-attempts", "80", "--max-duration", "300"], limit=360)
        report = workspace / "reduced.repomin/report.json"
        text, _ = execute("validate", [str(tool), "report", "validate", str(report),
            "--payload", str(payload), "--json"])
        validation = json.loads(text)
        if not (validation.get("valid") and validation.get("payload_fingerprint_mode") == "exact"
                and validation.get("payload_fingerprint_verified") is True
                and validation.get("source_files") == 14 and validation.get("output_files", 14) < 14):
            raise RuntimeError("Expected a real reduction and exact payload validation")
        text, _ = execute("replay", [str(tool), "report", "replay", str(report),
            "--payload", str(payload), "--runs", "3", "--yes", "--json"], limit=150)
        replay = json.loads(text)
        if not (replay.get("reproduced") and replay.get("passes") == 3
                and replay.get("fresh_repository_copy_per_run") and not replay.get("cache_used")):
            raise RuntimeError("Three fresh-copy replays did not succeed")

        controls = []
        for label in ("no-target", "one-export"):
            control = workspace / ("control-" + label)
            shutil.copytree(KIT / "prepared-input", control)
            if label == "no-target":
                css = control / "src/styles/common.module.css"
                original = css.read_text()
                changed = re.sub(r"cursor\s*:\s*pointer\s*;?", "", original)
                if changed == original:
                    raise RuntimeError("No target declaration found for negative control")
                css.write_text(changed)
            else:
                index = control / "src/index.ts"
                index.write_text(index.read_text().splitlines(True)[0])
            expected_code = 0 if label == "no-target" else 2
            text, code = execute("control-" + label, ["/bin/sh", "-c", COMMAND],
                                 cwd=control, limit=45, expected=(expected_code,))
            error = (logs / ("control-" + label + ".stderr")).read_text(errors="replace")
            if MARKER in text + error:
                raise RuntimeError("A negative control emitted the target marker")
            diagnostic = ("CSS composition count: 0" if label == "no-target"
                          else "Expected at least two style exports, got 1")
            if diagnostic not in text + error:
                raise RuntimeError("A negative control did not reach its intended oracle check")
            controls.append({"name": label, "returncode": code, "target_rejected": True,
                             "intended_oracle_check_reached": True})
        source_unchanged = all(digest(source / e["path"]) == e["sha256"]
                               for e in provenance["prepared_files"])
        if not source_unchanged:
            raise RuntimeError("Original prepared fixture bytes changed")
        execute("validate-after-controls", [str(tool), "report", "validate", str(report),
            "--payload", str(payload), "--json"])
        # Use the existing allow-listed Markdown summary for sharing, not private logs/report.
        markdown, _ = execute("shareable-summary", [str(tool), "report", "validate", str(report),
            "--payload", str(payload), "--format", "markdown"])
        (workspace / "validation-summary.md").write_text(markdown)
        summary = {key: value for key, value in validation.items() if key not in {"report", "payload"}}
        execution.update(status="passed", source_unchanged=True,
                         validation=summary, replay=replay, negative_controls=controls)
    except Exception as exc:
        execution.update(status="failed", error=str(exc))
        raise
    finally:
        execution["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save(workspace / "run-record.json", execution)
        print(json.dumps({"status": execution["status"],
                          "independent_user_trial": False}), flush=True)
        if execution["status"] == "passed":
            print("Read " + str(workspace / "validation-summary.md"), flush=True)
            print("Record usefulness or friction in FEEDBACK.md; nothing is sent automatically.", flush=True)
        else:
            print("Keep this workspace. Check run-record.json for the stopped stage and "
                  "private-logs for details; use a new directory to retry.", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, nargs="?", help="new directory; never overwritten")
    parser.add_argument("--check", action="store_true",
                        help="verify local requirements and kit; no workspace or download")
    args = parser.parse_args()
    if not args.check and args.workspace is None:
        parser.error("provide a new workspace directory, or use --check first")
    try:
        if args.check:
            print(json.dumps(preflight(args.workspace), indent=2))
        else:
            run_case(args.workspace)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        sys.exit(1)
