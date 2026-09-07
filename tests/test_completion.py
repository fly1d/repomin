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


if __name__ == "__main__":
    unittest.main()
