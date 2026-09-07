from __future__ import annotations

import unittest

from repomin.completion import SUPPORTED_SHELLS, completion_script


class CompletionScriptTests(unittest.TestCase):
    def test_reduction_completion_includes_quiet_with_description(self) -> None:
        for shell in SUPPORTED_SHELLS:
            with self.subTest(shell=shell):
                script = completion_script(shell)
                expected_option = "-l quiet" if shell == "fish" else "--quiet"
                self.assertIn(expected_option, script)
                if shell != "bash":
                    self.assertIn("suppress routine status output", script)

    def test_quiet_is_not_a_doctor_option(self) -> None:
        bash = completion_script("bash")
        bash_start = bash.index('if [[ "${COMP_WORDS[1]}" == "doctor" ]]; then')
        bash_end = bash.index("if (( COMP_CWORD == 1 ))", bash_start)
        self.assertNotIn("--quiet", bash[bash_start:bash_end])

        zsh = completion_script("zsh")
        zsh_start = zsh.index('if [[ "$words[2]" == "doctor" ]]; then')
        zsh_end = zsh.index("\n        return\n    fi", zsh_start)
        self.assertNotIn("--quiet", zsh[zsh_start:zsh_end])

    def test_doctor_completion_includes_output_formats(self) -> None:
        bash = completion_script("bash")
        bash_start = bash.index('if [[ "${COMP_WORDS[1]}" == "doctor" ]]; then')
        bash_end = bash.index("if (( COMP_CWORD == 1 ))", bash_start)
        bash_doctor = bash[bash_start:bash_end]
        self.assertIn("--format", bash_doctor)
        self.assertIn(
            '--format) COMPREPLY=( $(compgen -W "text json markdown"',
            bash_doctor,
        )

        zsh = completion_script("zsh")
        zsh_start = zsh.index('if [[ "$words[2]" == "doctor" ]]; then')
        zsh_end = zsh.index("\n        return\n    fi", zsh_start)
        zsh_doctor = zsh[zsh_start:zsh_end]
        self.assertIn(
            "'--format[output format]:format:(text json markdown)'",
            zsh_doctor,
        )

        fish = completion_script("fish")
        self.assertIn(
            "__fish_seen_subcommand_from doctor' -l format -r -a 'text json markdown'",
            fish,
        )

        powershell = completion_script("powershell")
        doctor_options_start = powershell.index("$doctorOptions = @(")
        doctor_options_end = powershell.index("\n    )", doctor_options_start)
        self.assertIn("'--format'", powershell[doctor_options_start:doctor_options_end])
        doctor_start = powershell.index("if ($doctorMode) {")
        doctor_end = powershell.index(
            "if ($enumValues.ContainsKey($previous))", doctor_start
        )
        doctor_format = powershell[doctor_start:doctor_end]
        self.assertIn("if ($previous -eq '--format')", doctor_format)
        self.assertIn("@('text', 'json', 'markdown')", doctor_format)
        self.assertIn("$doctorMode = $subcommand -eq 'doctor'", powershell)
        self.assertIn("$reportMode = $subcommand -eq 'report'", powershell)
        self.assertNotIn("$doctorMode = $elements |", powershell)


if __name__ == "__main__":
    unittest.main()
