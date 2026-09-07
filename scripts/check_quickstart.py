#!/usr/bin/env python3
"""Exercise the release-installed first-run workflow without network access."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="repomin-quickstart-") as directory:
        root = Path(directory)
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

print(\"ORIGINAL_FAILURE\", file=sys.stderr)
raise SystemExit(1)
""",
            encoding="utf-8",
        )
        (source / "input.txt").write_text(
            "keep-me\nremove-me\n", encoding="utf-8"
        )
        (source / "unused.txt").write_text("unrelated file\n", encoding="utf-8")
        reproduce = _command(sys.executable, "reproduce.py")

        doctor = _run(
            "doctor",
            str(source),
            "--command",
            reproduce,
            "--match",
            "ORIGINAL_FAILURE",
            "--exit-code",
            "1",
            "--text-file",
            "input.txt",
            "--output",
            str(output),
            "--format",
            "markdown",
        )
        if not doctor.stdout.startswith("# ReproMin Doctor summary\n"):
            raise RuntimeError("Doctor did not emit the documented Markdown summary")

        _run(
            str(source),
            "--command",
            reproduce,
            "--match",
            "ORIGINAL_FAILURE",
            "--exit-code",
            "1",
            "--adapter",
            "none",
            "--source-reducer",
            "none",
            "--text-file",
            "input.txt",
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
            "--format",
            "markdown",
        )
        if not validation.stdout.startswith("# ReproMin validation summary\n"):
            raise RuntimeError("validation did not emit the documented Markdown summary")
        if "| `payload_fingerprint_verified` | `true` |" not in validation.stdout:
            raise RuntimeError("validation did not verify the payload fingerprint")

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
        if '"passes": 2' not in replay.stdout or '"runs": 2' not in replay.stdout:
            raise RuntimeError("replay did not preserve the failure in two fresh copies")

    print("Quick-start smoke passed: Doctor, reduce, validate, and 2/2 replay.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print("Quick-start smoke failed: %s" % exc, file=sys.stderr)
        raise SystemExit(1)
