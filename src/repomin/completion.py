"""Generate shell completion from the public repomin parsers."""

from __future__ import annotations

import argparse
import shlex
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Sequence, Tuple


SUPPORTED_SHELLS: Final = ("bash", "zsh", "fish", "powershell")

_ROOT_COMMANDS: Final = (
    ("demo", "run a self-contained first reduction"),
    ("doctor", "check reducers, toolchains, and an optional baseline"),
    ("report", "inspect, replay, or compare report evidence"),
    ("completion", "print a shell completion script"),
)
_REPORT_COMMANDS: Final = (
    ("validate", "validate report structure and optional payload evidence"),
    ("replay", "run the recorded failure in fresh payload copies"),
    ("compare", "compare privacy-safe evidence from two or more reports"),
)


@dataclass(frozen=True)
class CompletionArgument:
    """Completion metadata derived from one argparse action."""

    name: str
    description: str
    takes_value: bool = True
    value_kind: str = "value"
    choices: Tuple[str, ...] = ()
    repeatable: bool = False


@dataclass(frozen=True)
class CompletionCommand:
    """Options and positionals accepted in one exact command context."""

    key: str
    options: Tuple[CompletionArgument, ...]
    positionals: Tuple[CompletionArgument, ...] = ()


# argparse cannot distinguish a path from an arbitrary string, or a file from a
# directory. This is the only completion metadata maintained beside the parsers.
_PATH_KINDS: Final = {
    "reduce": {
        "source": "directory",
        "--config": "file",
        "--output": "directory",
        "--session": "directory",
        "--ignore-path": "path",
        "--gitignore-file": "file",
        "--keep": "path",
        "--text-file": "file",
        "--java-classpath": "path",
    },
    "doctor": {
        "source": "directory",
        "--config": "file",
        "--output": "path",
        "--ignore-path": "path",
        "--keep": "path",
        "--text-file": "file",
        "--gitignore-file": "file",
    },
    "demo": {"workspace": "directory"},
    "report_validate": {"report": "file", "--payload": "directory"},
    "report_replay": {"report": "file", "--payload": "directory"},
    "report_compare": {"reports": "file"},
}


def _description(action: argparse.Action, fallback: str) -> str:
    help_text = action.help
    if not help_text or help_text is argparse.SUPPRESS:
        if fallback == "--version":
            return "show the installed version"
        return fallback.replace("--", "").replace("-", " ")
    return " ".join(str(help_text).split())


def _value_name(action: argparse.Action) -> str:
    metavar = action.metavar
    if isinstance(metavar, tuple):
        metavar = metavar[0] if metavar else None
    if metavar:
        return str(metavar)
    return action.dest.replace("_", "-").upper()


def _command_from_parser(
    key: str,
    parser: argparse.ArgumentParser,
) -> CompletionCommand:
    hints = _PATH_KINDS.get(key, {})
    used_hints = set()
    options = []
    positionals = []
    for action in parser._actions:
        long_options = [
            option for option in action.option_strings if option.startswith("--")
        ]
        choices = tuple(str(choice) for choice in (action.choices or ()))
        if long_options:
            for option in long_options:
                value_kind = hints.get(option, "value")
                if option in hints:
                    used_hints.add(option)
                options.append(
                    CompletionArgument(
                        name=option,
                        description=_description(action, option),
                        takes_value=action.nargs != 0,
                        value_kind=value_kind,
                        choices=choices,
                        repeatable=type(action).__name__ == "_AppendAction",
                    )
                )
            continue

        value_kind = hints.get(action.dest, "value")
        if action.dest in hints:
            used_hints.add(action.dest)
        positionals.append(
            CompletionArgument(
                name=_value_name(action),
                description=_description(action, action.dest),
                value_kind=value_kind,
                choices=choices,
                repeatable=action.nargs in ("*", "+"),
            )
        )

    unused_hints = set(hints) - used_hints
    if unused_hints:
        raise RuntimeError(
            "completion path hints do not match %s parser: %s"
            % (key, ", ".join(sorted(unused_hints)))
        )
    return CompletionCommand(key, tuple(options), tuple(positionals))


def _help_option(description: str) -> CompletionArgument:
    return CompletionArgument("--help", description, takes_value=False)


@lru_cache(maxsize=1)
def completion_command_specs() -> Tuple[CompletionCommand, ...]:
    """Return immutable completion specs sourced from the real CLI parsers."""
    from repomin.cli import (
        _build_demo_parser,
        _build_doctor_parser,
        _build_report_compare_parser,
        _build_report_replay_parser,
        _build_report_validate_parser,
        build_parser,
    )

    parser_specs = (
        _command_from_parser(
            "reduce", build_parser(semantic_environment_defaults=False)
        ),
        _command_from_parser("doctor", _build_doctor_parser()),
        _command_from_parser("demo", _build_demo_parser()),
        _command_from_parser(
            "report_validate", _build_report_validate_parser()
        ),
        _command_from_parser("report_replay", _build_report_replay_parser()),
        _command_from_parser("report_compare", _build_report_compare_parser()),
    )
    completion = CompletionCommand(
        "completion",
        (_help_option("show completion help"),),
        (
            CompletionArgument(
                "SHELL",
                "shell",
                choices=tuple(SUPPORTED_SHELLS),
            ),
        ),
    )
    report = CompletionCommand(
        "report",
        (_help_option("show report help"),),
        (
            CompletionArgument(
                "COMMAND",
                "report command",
                choices=tuple(name for name, _ in _REPORT_COMMANDS),
            ),
        ),
    )
    return parser_specs[:3] + (completion, report) + parser_specs[3:]


def _value_options(command: CompletionCommand) -> Tuple[str, ...]:
    return tuple(option.name for option in command.options if option.takes_value)


def _path_options(
    command: CompletionCommand,
    kinds: Sequence[str],
) -> Tuple[str, ...]:
    return tuple(
        option.name
        for option in command.options
        if option.takes_value and option.value_kind in kinds
    )


def _choice_options(
    commands: Sequence[CompletionCommand],
) -> Tuple[Tuple[str, str, Tuple[str, ...]], ...]:
    return tuple(
        (command.key, option.name, option.choices)
        for command in commands
        for option in command.options
        if option.choices
    )


def _bash_words(values: Sequence[str]) -> str:
    return " ".join(values).replace("\\", "\\\\").replace('"', '\\"')


def _render_bash(commands: Sequence[CompletionCommand]) -> str:
    lines = [
        "# Bash completion for repomin.",
        "_repomin_complete_directories() {",
        "    local candidate",
        "    while IFS= read -r candidate; do",
        '        COMPREPLY+=("$completion_prefix${candidate%/}/")',
        '    done < <(compgen -d -- "$cur")',
        "}",
        "",
        "_repomin_complete_files() {",
        "    local candidate",
        "    while IFS= read -r candidate; do",
        '        COMPREPLY+=("$completion_prefix$candidate")',
        '    done < <(compgen -f -- "$cur")',
        "}",
        "",
        "_repomin_prefix_completions() {",
        "    local index",
        '    if [[ -n "$completion_prefix" ]]; then',
        '        for (( index=0; index<${#COMPREPLY[@]}; index++ )); do',
        '            COMPREPLY[index]="$completion_prefix${COMPREPLY[index]}"',
        "        done",
        "    fi",
        "}",
        "",
        "_repomin() {",
        "    local cur prev context options value_options file_options directory_options completion_prefix",
        '    cur="${COMP_WORDS[COMP_CWORD]}"',
        '    prev="${COMP_WORDS[COMP_CWORD-1]}"',
        "    COMPREPLY=()",
        '    completion_prefix=""',
        '    if [[ "$cur" == --*=* ]]; then',
        '        prev="${cur%%=*}"',
        '        completion_prefix="$prev="',
        '        cur="${cur#*=}"',
        "    fi",
        '    context="reduce"',
        '    case "${COMP_WORDS[1]}" in',
        '        demo|doctor|completion) context="${COMP_WORDS[1]}" ;;',
        "        report)",
        '            context="report"',
        '            case "${COMP_WORDS[2]}" in',
        '                validate|replay|compare) context="report_${COMP_WORDS[2]}" ;;',
        "            esac",
        "            ;;",
        "    esac",
        '    options=""',
        '    value_options=""',
        '    file_options=""',
        '    directory_options=""',
        '    positional_kind=""',
        '    positional_choices=""',
        '    case "$context" in',
    ]
    for command in commands:
        positional = command.positionals[0] if command.positionals else None
        lines.extend(
            [
                "        %s)" % command.key,
                '            options="%s"'
                % _bash_words(tuple(option.name for option in command.options)),
                '            value_options="%s"'
                % _bash_words(_value_options(command)),
                '            file_options="%s"'
                % _bash_words(_path_options(command, ("file", "path"))),
                '            directory_options="%s"'
                % _bash_words(_path_options(command, ("directory",))),
                '            positional_kind="%s"'
                % (positional.value_kind if positional else ""),
                '            positional_choices="%s"'
                % _bash_words(positional.choices if positional else ()),
                "            ;;",
            ]
        )
    lines.extend(
        [
            "    esac",
            "",
            '    if (( COMP_CWORD == 1 )) && [[ -z "$completion_prefix" ]]; then',
            '        if [[ "$cur" == -* ]]; then',
            '            COMPREPLY=( $(compgen -W "$options" -- "$cur") )',
            "        else",
            '            COMPREPLY=( $(compgen -W "%s" -- "$cur") )'
            % _bash_words(tuple(name for name, _ in _ROOT_COMMANDS)),
            "            _repomin_complete_directories",
            "        fi",
            "        return 0",
            "    fi",
            "",
            '    case "$context:$prev" in',
        ]
    )
    for key, option, choices in _choice_options(commands):
        lines.append(
            '        "%s:%s") COMPREPLY=( $(compgen -W "%s" -- "$cur") ); _repomin_prefix_completions; return 0 ;;'
            % (key, option, _bash_words(choices))
        )
    lines.extend(
        [
            "    esac",
            '    if [[ " $value_options " == *" $prev "* ]]; then',
            '        if [[ " $directory_options " == *" $prev "* ]]; then',
            "            _repomin_complete_directories",
            '        elif [[ " $file_options " == *" $prev "* ]]; then',
            "            _repomin_complete_files",
            "        fi",
            "        return 0",
            "    fi",
            '    if [[ "$cur" == -* ]]; then',
            '        COMPREPLY=( $(compgen -W "$options" -- "$cur") )',
            "        return 0",
            "    fi",
            '    if [[ "$context" == "demo" || "$context" == "completion" || "$context" == "report" ]]; then',
            "        if (( COMP_CWORD > 2 )); then",
            "            return 0",
            "        fi",
            "    fi",
            '    if [[ -n "$positional_choices" ]]; then',
            '        COMPREPLY=( $(compgen -W "$positional_choices" -- "$cur") )',
            '    elif [[ "$positional_kind" == "directory" ]]; then',
            "        _repomin_complete_directories",
            '    elif [[ "$positional_kind" == "file" || "$positional_kind" == "path" ]]; then',
            "        _repomin_complete_files",
            "    fi",
            "}",
            "complete -F _repomin repomin",
        ]
    )
    return "\n".join(lines) + "\n"


def _zsh_description(value: str) -> str:
    return value.replace("[", "(").replace("]", ")").replace(":", "-")


def _zsh_argument(argument: CompletionArgument, position: int = 0) -> str:
    description = _zsh_description(argument.description)
    if position:
        prefix = "*" if argument.repeatable else str(position)
        base = "%s:%s:" % (prefix, description)
    else:
        base = ("*" if argument.repeatable else "") + argument.name
        base += "[%s]" % description
        if not argument.takes_value:
            return base
        value_name = argument.name.lower().replace("_", "-").lstrip("-")
        base += ":%s:" % value_name

    if argument.choices:
        return base + "(%s)" % " ".join(argument.choices)
    if argument.value_kind == "directory":
        return base + "_directories"
    if argument.value_kind in ("file", "path"):
        return base + "_files"
    return base


def _zsh_arguments(
    command: CompletionCommand,
    indent: str,
    extras: Sequence[str] = (),
) -> Sequence[str]:
    arguments = list(extras)
    arguments.extend(_zsh_argument(option) for option in command.options)
    arguments.extend(
        _zsh_argument(positional, index)
        for index, positional in enumerate(command.positionals, start=1)
    )
    lines = [indent + "_arguments -s \\"]
    for index, argument in enumerate(arguments):
        suffix = " \\" if index < len(arguments) - 1 else ""
        lines.append(indent + "    " + shlex.quote(argument) + suffix)
    return lines


def _render_zsh(commands: Sequence[CompletionCommand]) -> str:
    specs = {command.key: command for command in commands}
    lines = ["#compdef repomin", "", "_repomin() {"]
    lines.append('    if [[ "$words[2]" == "completion" ]]; then')
    lines.extend(['        words=("${words[@]:1}")', "        (( CURRENT-- ))"])
    lines.extend(_zsh_arguments(specs["completion"], "        "))
    lines.extend(["        return", "    fi"])
    lines.append('    if [[ "$words[2]" == "report" ]]; then')
    for index, name in enumerate(("validate", "replay", "compare")):
        keyword = "if" if index == 0 else "elif"
        lines.append(
            '        %s [[ "$words[3]" == "%s" ]]; then'
            % (keyword, name)
        )
        lines.extend(
            [
                '            words=("${words[@]:2}")',
                "            (( CURRENT -= 2 ))",
            ]
        )
        lines.extend(_zsh_arguments(specs["report_" + name], "            "))
    lines.append("        else")
    lines.extend(['            words=("${words[@]:1}")', "            (( CURRENT-- ))"])
    lines.extend(_zsh_arguments(specs["report"], "            "))
    lines.extend(["        fi", "        return", "    fi"])
    for name in ("doctor", "demo"):
        lines.append('    if [[ "$words[2]" == "%s" ]]; then' % name)
        lines.extend(['        words=("${words[@]:1}")', "        (( CURRENT-- ))"])
        lines.extend(_zsh_arguments(specs[name], "        "))
        lines.extend(["        return", "    fi"])

    root_commands = tuple(
        "%s[%s]" % (name, _zsh_description(description))
        for name, description in _ROOT_COMMANDS
    )
    lines.extend(_zsh_arguments(specs["reduce"], "    ", root_commands))
    lines.extend(["}", "", '_repomin "$@"'])
    return "\n".join(lines) + "\n"


def _fish_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _fish_action(argument: CompletionArgument) -> str:
    if argument.choices:
        return " ".join(argument.choices)
    if argument.value_kind == "directory":
        return "(__fish_complete_directories)"
    if argument.value_kind in ("file", "path"):
        return "(__fish_complete_path)"
    return ""


def _render_fish(commands: Sequence[CompletionCommand]) -> str:
    root_names = tuple(name for name, _ in _ROOT_COMMANDS)
    lines = [
        "# Fish completion for repomin.",
        "function __repomin_using_command",
        "    set -l tokens (commandline -opc)",
        "    switch $argv[1]",
        "        case reduce",
        "            if test (count $tokens) -lt 2",
        "                return 0",
        "            end",
        "            not contains -- $tokens[2] %s"
        % " ".join(root_names),
        "        case demo doctor completion",
        "            test (count $tokens) -ge 2; and test $tokens[2] = $argv[1]",
        "        case report",
        "            test (count $tokens) -ge 2; and test $tokens[2] = report; or return 1",
        "            test (count $tokens) -lt 3; and return 0",
        "            not contains -- $tokens[3] validate replay compare",
        "        case report_validate report_replay report_compare",
        "            set -l report_command (string replace report_ '' $argv[1])",
        "            test (count $tokens) -ge 3; and test $tokens[2] = report; and test $tokens[3] = $report_command",
        "    end",
        "end",
        "",
        "function __repomin_demo_needs_workspace",
        "    __repomin_using_command demo; and test (count (commandline -opc)) -eq 2",
        "end",
        "",
        "complete -c repomin -f -n '__fish_use_subcommand' -a %s -d 'command'"
        % _fish_quote(" ".join(root_names)),
        "complete -c repomin -f -n '__repomin_using_command report' -a %s -d 'report command'"
        % _fish_quote(" ".join(name for name, _ in _REPORT_COMMANDS)),
    ]

    for command in commands:
        for option in command.options:
            condition = "__repomin_using_command %s" % command.key
            parts = [
                "complete -c repomin -f -n %s" % _fish_quote(condition),
                "-l %s" % option.name[2:],
            ]
            if option.takes_value:
                parts.append("-r")
            action = _fish_action(option)
            if action:
                parts.append("-a %s" % _fish_quote(action))
            parts.append("-d %s" % _fish_quote(option.description))
            lines.append(" ".join(parts))

        for positional in command.positionals:
            condition = (
                "__repomin_demo_needs_workspace"
                if command.key == "demo"
                else "__repomin_using_command %s" % command.key
            )
            parts = ["complete -c repomin -f -n %s" % _fish_quote(condition)]
            action = _fish_action(positional)
            if action:
                parts.append("-a %s" % _fish_quote(action))
            parts.append("-d %s" % _fish_quote(positional.description))
            lines.append(" ".join(parts))
    return "\n".join(lines) + "\n"


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell_array(values: Sequence[str]) -> str:
    return "@(%s)" % ", ".join(_powershell_quote(value) for value in values)


def _powershell_map(
    variable: str,
    values: Sequence[Tuple[str, Sequence[str]]],
) -> Sequence[str]:
    lines = ["    $%s = @{" % variable]
    for key, items in values:
        lines.append(
            "        %s = %s" % (_powershell_quote(key), _powershell_array(items))
        )
    lines.append("    }")
    return lines


def _render_powershell(commands: Sequence[CompletionCommand]) -> str:
    option_values = tuple(
        (command.key, tuple(option.name for option in command.options))
        for command in commands
    )
    value_options = tuple(
        (command.key, _value_options(command)) for command in commands
    )
    file_options = tuple(
        (command.key, _path_options(command, ("file", "path")))
        for command in commands
    )
    directory_options = tuple(
        (command.key, _path_options(command, ("directory",)))
        for command in commands
    )
    choice_options = tuple(
        ("%s|%s" % (command.key, option.name), option.choices)
        for command in commands
        for option in command.options
        if option.choices
    )
    positional_choices = tuple(
        (command.key, command.positionals[0].choices)
        for command in commands
        if command.positionals and command.positionals[0].choices
    )
    positional_kinds = tuple(
        (command.key, (command.positionals[0].value_kind,))
        for command in commands
        if command.positionals
    )
    tooltips = tuple(
        (
            "%s|%s" % (command.key, option.name),
            (option.description,),
        )
        for command in commands
        for option in command.options
    )

    lines = [
        "# PowerShell completion for repomin.",
        "Register-ArgumentCompleter -Native -CommandName repomin -ScriptBlock {",
        "    param($wordToComplete, $commandAst, $cursorPosition)",
        "",
    ]
    lines.extend(_powershell_map("commandOptions", option_values))
    lines.extend(_powershell_map("commandValueOptions", value_options))
    lines.extend(_powershell_map("commandFileOptions", file_options))
    lines.extend(_powershell_map("commandDirectoryOptions", directory_options))
    lines.extend(_powershell_map("commandChoices", choice_options))
    lines.extend(_powershell_map("positionChoices", positional_choices))
    lines.extend(_powershell_map("positionKinds", positional_kinds))
    lines.extend(_powershell_map("optionTooltips", tooltips))
    lines.extend(
        [
            "",
            "    function Complete-ReproMinPath {",
            "        param([bool]$DirectoriesOnly)",
            "        $parentPath = if ([string]::IsNullOrEmpty($wordToComplete)) {",
            "            '.'",
            "        } else {",
            "            Split-Path -Parent $wordToComplete",
            "        }",
            "        $leafPrefix = if ([string]::IsNullOrEmpty($wordToComplete)) {",
            "            ''",
            "        } else {",
            "            Split-Path -Leaf $wordToComplete",
            "        }",
            "        if ([string]::IsNullOrEmpty($parentPath)) {",
            "            $parentPath = '.'",
            "        }",
            "        $items = if ($DirectoriesOnly) {",
            "            Get-ChildItem -LiteralPath $parentPath -Directory -Force -ErrorAction SilentlyContinue",
            "        } else {",
            "            Get-ChildItem -LiteralPath $parentPath -Force -ErrorAction SilentlyContinue",
            "        }",
            "        $items |",
            "            Where-Object {",
            "                $_.Name.StartsWith(",
            "                    $leafPrefix,",
            "                    [System.StringComparison]::OrdinalIgnoreCase",
            "                )",
            "            } |",
            "            ForEach-Object {",
            "                $completionText = $_.FullName",
            "                if ($_.PSIsContainer) {",
            "                    $completionText += [System.IO.Path]::DirectorySeparatorChar",
            "                }",
            "                $completionText = \"'\" + $completionText.Replace(\"'\", \"''\") + \"'\"",
            "                [System.Management.Automation.CompletionResult]::new(",
            "                    $completionText, $_.Name, 'ProviderItem', $_.FullName",
            "                )",
            "            }",
            "    }",
            "",
            "    function Complete-ReproMinValues {",
            "        param($Values, [string]$ResultType)",
            "        $Values |",
            "            Where-Object { $_ -like \"$wordToComplete*\" } |",
            "            ForEach-Object {",
            "                $tooltip = if ($ResultType -eq 'ParameterName') {",
            "                    $optionTooltips[$context + '|' + $_][0]",
            "                } else {",
            "                    $_",
            "                }",
            "                [System.Management.Automation.CompletionResult]::new(",
            "                    $_, $_, $ResultType, $tooltip",
            "                )",
            "            }",
            "    }",
            "",
            "    $elements = @($commandAst.CommandElements)",
            "    $subcommand = if ($elements.Count -gt 1) {",
            "        $elements[1].Extent.Text",
            "    } else {",
            "        ''",
            "    }",
            "    $reportCommand = if ($elements.Count -gt 2) {",
            "        $elements[2].Extent.Text",
            "    } else {",
            "        ''",
            "    }",
            "    $context = switch ($subcommand) {",
            "        'demo' { 'demo'; break }",
            "        'doctor' { 'doctor'; break }",
            "        'completion' { 'completion'; break }",
            "        'report' {",
            "            if ($reportCommand -in @('validate', 'replay', 'compare')) {",
            "                'report_' + $reportCommand",
            "            } else {",
            "                'report'",
            "            }",
            "            break",
            "        }",
            "        default { 'reduce' }",
            "    }",
            "    $previous = ''",
            "    if ($elements.Count -gt 1) {",
            "        $lastElement = $elements[$elements.Count - 1].Extent.Text",
            "        $previousIndex = if ($lastElement -eq $wordToComplete) {",
            "            $elements.Count - 2",
            "        } else {",
            "            $elements.Count - 1",
            "        }",
            "        if ($previousIndex -ge 1) {",
            "            $previous = $elements[$previousIndex].Extent.Text",
            "        }",
            "    }",
            "",
            "    $choiceKey = $context + '|' + $previous",
            "    if ($commandChoices.ContainsKey($choiceKey)) {",
            "        Complete-ReproMinValues -Values $commandChoices[$choiceKey] -ResultType 'ParameterValue'",
            "        return",
            "    }",
            "    if ($commandDirectoryOptions[$context] -contains $previous) {",
            "        Complete-ReproMinPath -DirectoriesOnly $true",
            "        return",
            "    }",
            "    if ($commandFileOptions[$context] -contains $previous) {",
            "        Complete-ReproMinPath -DirectoriesOnly $false",
            "        return",
            "    }",
            "    if ($commandValueOptions[$context] -contains $previous) {",
            "        return",
            "    }",
            "    if ($wordToComplete -like '-*') {",
            "        Complete-ReproMinValues -Values $commandOptions[$context] -ResultType 'ParameterName'",
            "        return",
            "    }",
            "",
            "    if ($context -eq 'reduce' -and $elements.Count -le 2) {",
            "        Complete-ReproMinValues -Values %s -ResultType 'Command'"
            % _powershell_array(tuple(name for name, _ in _ROOT_COMMANDS)),
            "    }",
            "    if ($positionChoices.ContainsKey($context)) {",
            "        Complete-ReproMinValues -Values $positionChoices[$context] -ResultType 'ParameterValue'",
            "        return",
            "    }",
            "    if ($positionKinds[$context][0] -eq 'directory') {",
            "        Complete-ReproMinPath -DirectoriesOnly $true",
            "    } elseif ($positionKinds.ContainsKey($context)) {",
            "        Complete-ReproMinPath -DirectoriesOnly $false",
            "    }",
            "}",
        ]
    )
    return "\n".join(lines) + "\n"


def completion_script(shell: str) -> str:
    """Return a deterministic completion script for one supported shell."""
    renderers = {
        "bash": _render_bash,
        "zsh": _render_zsh,
        "fish": _render_fish,
        "powershell": _render_powershell,
    }
    try:
        renderer = renderers[shell]
    except KeyError as exc:
        raise ValueError(
            "unsupported shell %r (choose one of: %s)"
            % (shell, ", ".join(SUPPORTED_SHELLS))
        ) from exc
    return renderer(completion_command_specs())
