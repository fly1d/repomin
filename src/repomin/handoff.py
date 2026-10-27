"""Build a local, copyable handoff from a verified Doctor invocation."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence


def reduction_arguments(
    argv: Sequence[str], parser: argparse.ArgumentParser
) -> Optional[list[str]]:
    """Preserve parsed Doctor options without explicit environment values.

    Call only after the invocation has passed argument parsing and its baseline.
    Walking actions rather than deleting token strings preserves option-looking
    values, abbreviations, repeated options, and the positional-only delimiter.
    """
    options: list[str] = []
    known_options = {
        option: action
        for action in parser._actions
        for option in action.option_strings
    }
    configured = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            options.extend(argv[index:])
            break
        option, separator, explicit_value = token.partition("=")
        action = known_options.get(option)
        if action is None and option.startswith("--") and parser.allow_abbrev:
            matches = [name for name in known_options if name.startswith(option)]
            if len(matches) == 1:
                action = known_options[matches[0]]
        if action is None and not token.startswith("-"):
            options.append(token)
            index += 1
            continue
        if action is None or action.nargs not in (None, 0):
            # A future parser shape must be handled explicitly before advice
            # can promise an equivalent command.
            return None
        if action.dest == "environment_entries":
            return None
        configured = configured or action.dest == "config"
        canonical = next(
            (value for value in action.option_strings if value.startswith("--")),
            None,
        )
        if canonical is None:
            return None
        if action.nargs == 0:
            if action.dest not in {"json", "help", "version"}:
                options.append(canonical)
            index += 1
            continue
        if not separator:
            if index + 1 >= len(argv):
                return None
            value = argv[index + 1]
            index += 2
        else:
            value = explicit_value
            index += 1
        if action.dest != "format":
            # An equals token also preserves values beginning with a dash.
            options.append(canonical + "=" + value)

    limits = [] if configured else [
        "--max-attempts=25", "--max-duration=300", "--semantic-reducer=none"
    ]
    return ["repomin", "reduce", *limits, *options]


def format_reduction_command(
    arguments: Sequence[str], *, windows: Optional[bool] = None
) -> str:
    """Quote arguments for Bash/Zsh, or Windows PowerShell and PowerShell 7."""
    if windows is None:
        windows = os.name == "nt"
    # Keep the package Doctor actually used. A fresh ``-m repomin`` lookup
    # could import repository code or another installation from cwd/PYTHONPATH.
    entrypoint = Path(__file__).resolve().with_name("__main__.py")
    invocation = [sys.executable, "-I", str(entrypoint), *arguments[1:]]
    if not windows:
        return shlex.join(invocation)

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    # Start-Process receives a literal Windows command line. Unlike native
    # PowerShell invocation this retains embedded quotes on Windows PowerShell
    # 5.1 as well as PowerShell 7. Retain the cwd in which Doctor verified
    # relative paths while the isolated launcher fixes the package identity.
    native = subprocess.list2cmdline(invocation[1:])
    return (
        "(Start-Process -FilePath %s -ArgumentList %s "
        "-WorkingDirectory (Get-Location).Path "
        "-NoNewWindow -Wait -PassThru).ExitCode"
    ) % (literal(sys.executable), literal(native))
