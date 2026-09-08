#!/usr/bin/env python3
"""Deterministic local-stub benchmark for the opt-in semantic reducer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent


class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        inner = json.dumps(
            {"edits": [{"path": "data.txt", "replace": "NEEDLE\n"}]}
        )
        payload = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": inner,
                        }
                    }
                ],
                "model": body.get("model"),
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _metadata_output(output: Path) -> Path:
    return output.with_name(output.name + ".repomin")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the local-stub semantic reducer benchmark.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="output directory (default: a temporary directory)",
    )
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = "http://127.0.0.1:%d/v1/chat/completions" % server.server_port
        with tempfile.TemporaryDirectory() as temporary:
            output = args.output or (Path(temporary) / "result")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(REPO_ROOT / "src")
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "repomin",
                    str(FIXTURE),
                    "--command",
                    "python3 reproduce.py",
                    "--match",
                    "ORIGINAL_FAILURE",
                    "--source-reducer",
                    "none",
                    "--adapter",
                    "none",
                    "--semantic-reducer",
                    "http",
                    "--semantic-endpoint",
                    endpoint,
                    "--semantic-model",
                    "benchmark-model",
                    "--semantic-timeout",
                    "12.5",
                    "--output",
                    str(output),
                ],
                cwd=str(REPO_ROOT),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if run.returncode != 0:
                print(run.stderr, file=sys.stderr)
                return run.returncode

            report = json.loads(
                (_metadata_output(output) / "report.json").read_text(encoding="utf-8")
            )
            assert report["execution"]["semantic_reducer"] == "http"
            assert report["execution"]["semantic_model"] == "benchmark-model"
            assert report["execution"]["semantic_endpoint"] == endpoint
            assert report["execution"]["semantic_timeout"] == 12.5
            assert report["execution"]["semantic_calls"] >= 1
            assert report["execution"]["semantic_accepted"] == 1
            assert (output / "data.txt").read_text(encoding="utf-8") == "NEEDLE\n"
            assert (output / "reproduce.py").is_file()
            print("semantic-stub benchmark accepted")
            return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    raise SystemExit(main())
