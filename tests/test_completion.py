from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from repomin.cli import (
    _build_demo_parser,
    _build_doctor_parser,
    _build_report_compare_parser,
    _build_report_replay_parser,
    _build_report_validate_parser,
    build_parser,
)
from repomin.completion import (
    SUPPORTED_SHELLS,
    completion_command_specs,
    completion_script,
)


def _long_option_contract(parser: argparse.ArgumentParser) -> dict:
    contract = {}
    for action in parser._actions:
        for option in action.option_strings:
            if option.startswith("--"):
                contract[option] = (
                    action.nargs != 0,
                    tuple(str(choice) for choice in (action.choices or ())),
                    type(action).__name__ == "_AppendAction",
                )
    return contract


class CompletionSpecTests(unittest.TestCase):
    def test_specs_match_every_real_parser(self) -> None:
        parsers = {
            "reduce": build_parser(semantic_environment_defaults=False),
            "doctor": _build_doctor_parser(),
            "demo": _build_demo_parser(),
            "report_validate": _build_report_validate_parser(),
            "report_replay": _build_report_replay_parser(),
            "report_compare": _build_report_compare_parser(),
        }
        specs = {spec.key: spec for spec in completion_command_specs()}
        for key, parser in parsers.items():
            with self.subTest(command=key):
                expected = _long_option_contract(parser)
                actual = {
                    option.name: (
                        option.takes_value,
                        option.choices,
                        option.repeatable,
                    )
                    for option in specs[key].options
                }
                self.assertEqual(expected, actual)

    def test_command_scopes_do_not_leak_options(self) -> None:
        specs = {spec.key: spec for spec in completion_command_specs()}

        def names(key: str) -> set:
            return {option.name for option in specs[key].options}

        self.assertNotIn("--json", names("reduce"))
        self.assertNotIn("--quiet", names("doctor"))
        self.assertNotIn("--candidate-runs", names("doctor"))
        self.assertNotIn("--runs", names("report_validate"))
        self.assertNotIn("--format", names("report_replay"))
        self.assertNotIn("--payload", names("report_compare"))
        self.assertEqual(
            {"bash", "zsh", "fish", "powershell"},
            set(specs["completion"].positionals[0].choices),
        )

    def test_path_and_repeatability_metadata_is_preserved(self) -> None:
        specs = {spec.key: spec for spec in completion_command_specs()}
        reduce_options = {option.name: option for option in specs["reduce"].options}
        doctor_options = {option.name: option for option in specs["doctor"].options}

        self.assertEqual("directory", specs["reduce"].positionals[0].value_kind)
        self.assertEqual("directory", reduce_options["--output"].value_kind)
        self.assertEqual("file", reduce_options["--config"].value_kind)
        self.assertEqual("directory", specs["demo"].positionals[0].value_kind)
        self.assertTrue(reduce_options["--env"].repeatable)
        self.assertTrue(reduce_options["--java-classpath"].repeatable)
        self.assertTrue(doctor_options["--gitignore-file"].repeatable)
        replay_options = {
            option.name: option for option in specs["report_replay"].options
        }
        self.assertEqual(
            ("recorded", "host", "docker"),
            replay_options["--backend"].choices,
        )

    def test_scripts_are_deterministic_and_end_with_one_newline(self) -> None:
        markers = {
            "bash": "complete -F _repomin repomin",
            "zsh": "#compdef repomin",
            "fish": "complete -c repomin",
            "powershell": (
                "Register-ArgumentCompleter -Native -CommandName repomin"
            ),
        }
        for shell in SUPPORTED_SHELLS:
            with self.subTest(shell=shell):
                first = completion_script(shell)
                self.assertEqual(first, completion_script(shell))
                self.assertTrue(first.endswith("\n"))
                self.assertFalse(first.endswith("\n\n"))
                self.assertIn(markers[shell], first)

    def test_renderers_preserve_choices_paths_and_repeatable_options(self) -> None:
        bash = completion_script("bash")
        self.assertIn(
            '"report_replay:--backend") COMPREPLY=('
            ' $(compgen -W "recorded host docker"',
            bash,
        )
        self.assertIn('directory_options="--output --session"', bash)

        zsh = completion_script("zsh")
        self.assertIn("*--env[", zsh)
        self.assertRegex(zsh, r"--adapter\[[^\n]+\]:adapter:\(auto none")
        self.assertRegex(zsh, r"--payload\[[^\n]+\]:payload:_directories")
        self.assertIn('words=("${words[@]:2}")', zsh)

        fish = completion_script("fish")
        self.assertIn(
            "-n '__repomin_using_command doctor' -l format -r "
            "-a 'text json markdown'",
            fish,
        )
        self.assertNotIn("not __repomin_demo_mode", fish)

        powershell = completion_script("powershell")
        self.assertIn("'reduce' = @('--help', '--version'", powershell)
        reduce_line = next(
            line
            for line in powershell.splitlines()
            if line.strip().startswith("'reduce' = @('--help'")
        )
        self.assertNotIn("--json", reduce_line)
        self.assertIn(
            "$reportCommand = if ($elements.Count -gt 2)",
            powershell,
        )
        self.assertIn("$subcommand -ne 'reduce'", powershell)
        self.assertIn("Get-ChildItem -LiteralPath $parentPath", powershell)
        self.assertNotIn("Get-ChildItem -Path $pathPattern", powershell)

    def test_renderers_route_the_explicit_reduce_command(self) -> None:
        bash = completion_script("bash")
        self.assertIn(
            'demo|doctor|reduce|completion) context="${COMP_WORDS[1]}"',
            bash,
        )

        zsh = completion_script("zsh")
        self.assertIn('if [[ "$words[2]" == "reduce" ]]; then', zsh)

        fish = completion_script("fish")
        self.assertIn("if test $tokens[2] = reduce", fish)
        self.assertIn(
            "-n '__repomin_using_command reduce' -l adapter -r",
            fish,
        )

        powershell = completion_script("powershell")
        self.assertIn("'reduce' { 'reduce'; break }", powershell)
        self.assertIn(
            "$context -eq 'reduce' -and $subcommand -ne 'reduce'",
            powershell,
        )

    def test_unknown_shell_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported shell"):
            completion_script("tcsh")


class NativeCompletionTests(unittest.TestCase):
    bash = shutil.which("bash")
    zsh = shutil.which("zsh")
    fish = shutil.which("fish")
    powershell = shutil.which("pwsh") or shutil.which("powershell")

    def _bash_complete(self, words: list, cwd: str) -> list:
        assert self.bash is not None
        word_array = " ".join(shlex.quote(word) for word in words)
        program = completion_script("bash") + (
            "\nCOMP_WORDS=(%s)\n"
            "COMP_CWORD=%d\n"
            "COMPREPLY=()\n"
            "_repomin\n"
            "printf '%%s\\n' \"${COMPREPLY[@]}\"\n"
            % (word_array, len(words) - 1)
        )
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc"],
            input=program,
            text=True,
            cwd=cwd,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return [line for line in result.stdout.splitlines() if line]

    @unittest.skipUnless(bash, "Bash is unavailable")
    def test_bash_syntax(self) -> None:
        result = subprocess.run(
            [self.bash, "-n"],
            input=completion_script("bash"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    @unittest.skipUnless(zsh, "Zsh is unavailable")
    def test_zsh_syntax(self) -> None:
        result = subprocess.run(
            [self.zsh, "-n"],
            input=completion_script("zsh"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    @unittest.skipUnless(fish, "Fish is unavailable")
    def test_fish_syntax(self) -> None:
        result = subprocess.run(
            [self.fish, "-n"],
            input=completion_script("fish"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    @unittest.skipUnless(bash, "Bash is unavailable")
    def test_bash_root_options_and_subcommands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "source dir").mkdir()
            options = self._bash_complete(["repomin", "--"], directory)
            self.assertIn("--quiet", options)
            self.assertNotIn("--json", options)

            targets = self._bash_complete(["repomin", ""], directory)
            self.assertTrue(
                {"demo", "doctor", "reduce", "report", "completion"}.issubset(
                    targets
                )
            )
            self.assertIn("source dir/", targets)
            self.assertIn(
                "source dir/",
                self._bash_complete(
                    ["repomin", "reduce", "source"], directory
                ),
            )

    @unittest.skipUnless(bash, "Bash is unavailable")
    def test_bash_command_contexts_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                ["bash", "zsh", "fish", "powershell"],
                self._bash_complete(["repomin", "completion", ""], directory),
            )
            self.assertEqual(
                ["--help"],
                self._bash_complete(["repomin", "completion", "--"], directory),
            )
            self.assertEqual(
                ["--adapter"],
                self._bash_complete(["repomin", "doctor", "--ada"], directory),
            )
            self.assertEqual(
                ["--adapter"],
                self._bash_complete(["repomin", "reduce", "--ada"], directory),
            )
            self.assertEqual(
                ["--adapter"],
                self._bash_complete(
                    ["repomin", "reduce", "doctor", "--ada"], directory
                ),
            )
            self.assertEqual(
                [],
                self._bash_complete(
                    ["repomin", "doctor", "--candidate"], directory
                ),
            )
            self.assertEqual(
                [],
                self._bash_complete(
                    ["repomin", "doctor", "--timeout", ""], directory
                ),
            )
            self.assertEqual(
                ["validate", "replay", "compare"],
                self._bash_complete(["repomin", "report", ""], directory),
            )
            self.assertEqual(
                ["--format"],
                self._bash_complete(
                    ["repomin", "report", "validate", "--f"], directory
                ),
            )

    @unittest.skipUnless(bash, "Bash is unavailable")
    def test_bash_completes_choices_and_safe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "payload dir").mkdir()
            (root / "config file.json").write_text("{}", encoding="utf-8")
            (root / "ordinary.txt").write_text("x", encoding="utf-8")

            self.assertIn(
                "docker",
                self._bash_complete(
                    ["repomin", "doctor", "--backend", "d"], directory
                ),
            )
            self.assertEqual(
                ["recorded", "host", "docker"],
                self._bash_complete(
                    [
                        "repomin",
                        "report",
                        "replay",
                        "--backend",
                        "",
                    ],
                    directory,
                ),
            )
            self.assertEqual(
                ["--backend=docker"],
                self._bash_complete(
                    ["repomin", "doctor", "--backend=d"], directory
                ),
            )
            payloads = self._bash_complete(
                [
                    "repomin",
                    "report",
                    "validate",
                    "--payload",
                    "",
                ],
                directory,
            )
            self.assertIn("payload dir/", payloads)
            self.assertNotIn("ordinary.txt", payloads)
            self.assertIn(
                "config file.json",
                self._bash_complete(
                    ["repomin", "--config", "config"], directory
                ),
            )
            self.assertIn(
                "--config=config file.json",
                self._bash_complete(
                    ["repomin", "--config=config"], directory
                ),
            )
            self.assertEqual(
                [],
                self._bash_complete(
                    ["repomin", "demo", "payload dir", ""], directory
                ),
            )

    @unittest.skipUnless(powershell, "PowerShell is unavailable")
    def test_powershell_completion_context(self) -> None:
        assert self.powershell is not None
        program = completion_script("powershell") + (
            "\n$line = 'repomin completion '\n"
            "(TabExpansion2 -inputScript $line "
            "-cursorColumn $line.Length).CompletionMatches.CompletionText\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "completion-test.ps1"
            script.write_text(program, encoding="utf-8")
            result = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(script),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {"bash", "zsh", "fish", "powershell"},
            set(result.stdout.splitlines()),
        )


if __name__ == "__main__":
    unittest.main()
