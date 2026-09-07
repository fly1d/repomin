"""Shell completion scripts for the public ``repomin`` command."""

from __future__ import annotations

from typing import Final


SUPPORTED_SHELLS: Final = ("bash", "zsh", "fish", "powershell")


_BASH = r'''# Bash completion for repomin.
_repomin() {
    local cur prev options value_options candidate
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    if [[ "${COMP_WORDS[1]}" == "report" ]]; then
        if (( COMP_CWORD == 2 )); then
            COMPREPLY=( $(compgen -W "validate replay compare --help" -- "$cur") )
            return 0
        fi
        if [[ "${COMP_WORDS[2]}" == "replay" ]]; then
            options="--help --payload --runs --timeout --env --backend --docker-image --docker-network --exit-code --yes --json"
            value_options="--payload --runs --timeout --env --backend --docker-image --docker-network --exit-code"
            case "$prev" in
                --backend) COMPREPLY=( $(compgen -W "recorded host docker" -- "$cur") ); return 0 ;;
                --docker-network) COMPREPLY=( $(compgen -W "none bridge host" -- "$cur") ); return 0 ;;
            esac
            if [[ " $value_options " == *" $prev "* ]]; then
                if [[ "$prev" == "--payload" ]]; then
                    COMPREPLY=( $(compgen -f -- "$cur") )
                fi
                return 0
            fi
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "$options" -- "$cur") )
            else
                COMPREPLY=( $(compgen -f -- "$cur") )
            fi
            return 0
        fi
        if [[ "${COMP_WORDS[2]}" == "validate" ]]; then
            options="--help --payload --json --format"
            value_options="--payload --format"
            case "$prev" in
                --format) COMPREPLY=( $(compgen -W "text json markdown" -- "$cur") ); return 0 ;;
            esac
            if [[ " $value_options " == *" $prev "* ]]; then
                if [[ "$prev" == "--payload" ]]; then
                    COMPREPLY=( $(compgen -f -- "$cur") )
                fi
                return 0
            fi
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "$options" -- "$cur") )
            else
                COMPREPLY=( $(compgen -f -- "$cur") )
            fi
            return 0
        fi
        if [[ "${COMP_WORDS[2]}" == "compare" ]]; then
            options="--help --format --label"
            value_options="--format --label"
            case "$prev" in
                --format) COMPREPLY=( $(compgen -W "text json markdown" -- "$cur") ); return 0 ;;
            esac
            if [[ " $value_options " == *" $prev "* ]]; then
                if [[ "$prev" == "--label" ]]; then
                    COMPREPLY=()
                fi
                return 0
            fi
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "$options" -- "$cur") )
            else
                COMPREPLY=( $(compgen -f -- "$cur") )
            fi
            return 0
        fi
    fi
    if [[ "${COMP_WORDS[1]}" == "doctor" ]]; then
        options="--version --help --config --command --match --exit-code --java-exception --python-exception --process-failure --adapter --source-reducer --backend --docker-image --docker-network --docker-cpus --docker-memory --docker-pids-limit --docker-tmpfs-size --docker-workspace-limit --timeout --baseline-runs --min-baseline-passes --min-baseline-rate --confidence --output --ignore --ignore-path --keep --text-file --gitignore --gitignore-file --gitignore-recursive --env --json --format"
        value_options="--config --command --match --exit-code --adapter --source-reducer --backend --docker-image --docker-network --docker-cpus --docker-memory --docker-pids-limit --docker-tmpfs-size --docker-workspace-limit --timeout --baseline-runs --min-baseline-passes --min-baseline-rate --confidence --output --ignore --ignore-path --keep --text-file --gitignore-file --env --format"
        case "$prev" in
            --format) COMPREPLY=( $(compgen -W "text json markdown" -- "$cur") ); return 0 ;;
            --backend) COMPREPLY=( $(compgen -W "host docker" -- "$cur") ); return 0 ;;
            --docker-network) COMPREPLY=( $(compgen -W "none bridge host" -- "$cur") ); return 0 ;;
            --adapter) COMPREPLY=( $(compgen -W "auto none maven gradle python pipenv node composer dotnet ruby cargo go" -- "$cur") ); return 0 ;;
            --source-reducer) COMPREPLY=( $(compgen -W "auto none java python" -- "$cur") ); return 0 ;;
        esac
        if [[ " $value_options " == *" $prev "* ]]; then
            COMPREPLY=( $(compgen -f -- "$cur") )
        elif [[ "$cur" == -* ]]; then
            COMPREPLY=( $(compgen -W "$options" -- "$cur") )
        else
            COMPREPLY=( $(compgen -f -- "$cur") )
        fi
        return 0
    fi
    if [[ "${COMP_WORDS[1]}" == "demo" ]]; then
        COMPREPLY=()
        if (( COMP_CWORD == 2 )); then
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "--help" -- "$cur") )
            else
                while IFS= read -r candidate; do
                    COMPREPLY+=("${candidate%/}/")
                done < <(compgen -d -- "$cur")
            fi
        fi
        return 0
    fi
    if (( COMP_CWORD == 1 )); then
        COMPREPLY=( $(compgen -W "completion demo doctor report" -- "$cur") )
        return 0
    fi
    options="--version --help --config --command --match --exit-code --output --session --resume --timeout --backend --docker-image --docker-network --docker-cpus --docker-memory --docker-pids-limit --docker-tmpfs-size --docker-workspace-limit --jobs --no-cache --max-attempts --max-duration --ignore --ignore-path --gitignore --gitignore-file --gitignore-recursive --keep --env --java-exception --python-exception --process-failure --baseline-runs --min-baseline-passes --candidate-runs --min-candidate-passes --min-baseline-rate --min-candidate-rate --confidence --run-confidence --holdout-runs --min-holdout-rate --holdout-confidence --adapter --source-reducer --text-file --semantic-reducer --semantic-endpoint --semantic-model --semantic-timeout --java-classpath --quiet --verbose"
    value_options="--config --command --match --exit-code --output --session --timeout --backend --docker-image --docker-network --docker-cpus --docker-memory --docker-pids-limit --docker-tmpfs-size --docker-workspace-limit --jobs --max-attempts --max-duration --ignore --ignore-path --gitignore-file --keep --env --baseline-runs --min-baseline-passes --candidate-runs --min-candidate-passes --min-baseline-rate --min-candidate-rate --confidence --run-confidence --holdout-runs --min-holdout-rate --holdout-confidence --adapter --source-reducer --text-file --semantic-reducer --semantic-endpoint --semantic-model --semantic-timeout --java-classpath"
    case "$prev" in
        --backend) COMPREPLY=( $(compgen -W "host docker" -- "$cur") ); return 0 ;;
        --docker-network) COMPREPLY=( $(compgen -W "none bridge host" -- "$cur") ); return 0 ;;
        --adapter) COMPREPLY=( $(compgen -W "auto none maven gradle python pipenv node composer dotnet ruby cargo go" -- "$cur") ); return 0 ;;
        --source-reducer) COMPREPLY=( $(compgen -W "auto none java python" -- "$cur") ); return 0 ;;
        --semantic-reducer) COMPREPLY=( $(compgen -W "none http" -- "$cur") ); return 0 ;;
    esac
    if [[ " $value_options " == *" $prev "* ]]; then
        COMPREPLY=( $(compgen -f -- "$cur") )
        return 0
    fi
    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$options" -- "$cur") )
    else
        COMPREPLY=( $(compgen -f -- "$cur") )
    fi
}
complete -F _repomin repomin
'''


_ZSH = r'''#compdef repomin

_repomin() {
    local -a options
    if [[ "$words[2]" == "report" ]]; then
        if [[ "$words[3]" == "validate" ]]; then
            _arguments -s \
                '1:report:_files' \
                '--help[show report validation help]' \
                '--payload[exported payload directory]:directory:_files -/' \
                '--json[print a machine-readable result]' \
                '--format[output format]:format:(text json markdown)'
        elif [[ "$words[3]" == "replay" ]]; then
            _arguments -s \
                '1:report:_files' \
                '--help[show report replay help]' \
                '--payload[exported payload directory]:directory:_files -/' \
                '--runs[number of fresh replay copies]:count:' \
                '--timeout[seconds per replay run]:seconds:' \
                '*--env[recorded environment variable]:NAME=VALUE:' \
                '--backend[execution backend]:backend:(recorded host docker)' \
                '--docker-image[local Docker image]:image:' \
                '--docker-network[Docker network policy]:network:(none bridge host)' \
                '--exit-code[legacy exit-code contract]:code:' \
                '--yes[acknowledge execution of the report command]' \
                '--json[print machine-readable replay evidence]'
        elif [[ "$words[3]" == "compare" ]]; then
            _arguments -s \
                '*:report:_files' \
                '--help[show report comparison help]' \
                '*--label[short report label]:name:' \
                '--format[output format]:format:(text json markdown)'
        else
            _arguments -s '1:report command:(validate replay compare)' '--help[show report help]'
        fi
        return
    fi
    if [[ "$words[2]" == "doctor" ]]; then
        _arguments -s \
            '1:repository:_files' \
            '--version[show the installed version]' \
            '--help[show Doctor help]' \
            '--config[versioned JSON reduction spec]:file:_files' \
            '--command[optional failure reproduction command]:command:' \
            '--match[regular expression required in output]:pattern:' \
            '--exit-code[exact process exit code]:code:' \
            '--java-exception[preserve a normalized Java exception]' \
            '--python-exception[preserve a normalized Python exception]' \
            '--process-failure[preserve process termination]' \
            '--adapter[structured manifest reducer]:adapter:(auto none maven gradle python pipenv node composer dotnet ruby cargo go)' \
            '--source-reducer[source-level reducer]:reducer:(auto none java python)' \
            '--backend[execution backend]:backend:(host docker)' \
            '--docker-image[Docker image]:image:' \
            '--docker-network[Docker network policy]:network:(none bridge host)' \
            '--docker-cpus[Docker CPU quota]:cores:' \
            '--docker-memory[Docker memory limit]:size:' \
            '--docker-pids-limit[maximum container processes]:count:' \
            '--docker-tmpfs-size[container /tmp size]:size:' \
            '--docker-workspace-limit[writable workspace limit]:size:' \
            '--timeout[seconds per baseline run]:seconds:' \
            '--baseline-runs[fresh baseline copies]:count:' \
            '--min-baseline-passes[minimum baseline passes]:count:' \
            '--min-baseline-rate[minimum baseline rate]:rate:' \
            '--confidence[confidence level]:level:' \
            '--output[output path to check]:path:_files' \
            '--ignore[ignored basename]:name:' \
            '--ignore-path[ignored repository path]:path:_files' \
            '*--keep[protected repository path]:path:_files' \
            '*--text-file[UTF-8 text reduction target]:path:_files' \
            '--gitignore[apply repository .gitignore]' \
            '--gitignore-file[apply a gitignore-style file]:file:_files' \
            '--gitignore-recursive[apply nested .gitignore files]' \
            '--env[baseline environment variable]:NAME=VALUE:' \
            '--json[print a machine-readable result]' \
            '--format[output format]:format:(text json markdown)'
        return
    fi
    if [[ "$words[2]" == "demo" ]]; then
        _arguments -s \
            '1:new workspace:_directories' \
            '--help[show demo help]'
        return
    fi
    options=(
        '1:repository:_files'
        'demo[run a self-contained first reduction]'
        'doctor[check reducers, toolchains, and an optional baseline]'
        'report[inspect or validate a report]'
        'completion[print a shell completion script]'
        '--version[show the installed version]'
        '--help[show command help]'
        '--config[versioned JSON reduction spec]:file:_files'
        '--command[failure reproduction command]:command:'
        '--match[regular expression that must remain present]:pattern:'
        '--exit-code[required exit code]:code:(0 1 2 7 9)'
        '--output[output directory]:directory:_files -/'
        '--session[persistent session directory]:directory:_files -/'
        '--resume[resume an existing session]'
        '--timeout[seconds per run]:seconds:'
        '--backend[execution backend]:backend:(host docker)'
        '--docker-image[Docker image]:image:'
        '--docker-network[Docker network policy]:network:(none bridge host)'
        '--docker-cpus[Docker CPU quota]:cores:'
        '--docker-memory[Docker memory limit]:size:'
        '--docker-pids-limit[maximum container processes]:count:'
        '--docker-tmpfs-size[container /tmp size]:size:'
        '--docker-workspace-limit[writable workspace limit]:size:'
        '--jobs[concurrent candidate commands]:count:'
        '--no-cache[disable result caching]'
        '--max-attempts[logical candidate attempt budget]:count:'
        '--max-duration[wall-clock budget]:seconds:'
        '--ignore[ignored basename]:name:'
        '--ignore-path[ignored repository-relative path]:path:_files'
        '--gitignore[apply repository .gitignore]'
        '--gitignore-file[apply a gitignore-style file]:file:_files'
        '--gitignore-recursive[apply nested .gitignore files]'
        '--keep[protect a repository-relative path]:path:_files'
        '--env[reproduction environment variable]:NAME=VALUE:'
        '--java-exception[preserve a normalized Java exception]'
        '--python-exception[preserve a normalized Python exception]'
        '--process-failure[preserve process termination]'
        '--baseline-runs[baseline samples]:count:'
        '--min-baseline-passes[minimum baseline passes]:count:'
        '--candidate-runs[candidate samples]:count:'
        '--min-candidate-passes[minimum candidate passes]:count:'
        '--min-baseline-rate[minimum baseline rate]:rate:'
        '--min-candidate-rate[minimum candidate rate]:rate:'
        '--confidence[confidence level]:level:'
        '--run-confidence[run-wide confidence]:level:'
        '--holdout-runs[holdout samples]:count:'
        '--min-holdout-rate[minimum holdout rate]:rate:'
        '--holdout-confidence[holdout confidence]:level:'
        '--adapter[structured manifest reducer]:adapter:(auto none maven gradle python pipenv node composer dotnet ruby cargo go)'
        '--source-reducer[source-level reducer]:reducer:(auto none java python)'
        '--text-file[line-reduce a UTF-8 text file]:path:_files'
        '--semantic-reducer[semantic reducer backend]:backend:(none http)'
        '--semantic-endpoint[OpenAI-compatible endpoint]:url:'
        '--semantic-model[semantic model name]:name:'
        '--semantic-timeout[semantic HTTP timeout]:seconds:'
        '--java-classpath[Java analysis classpath]:path:_files'
        '--quiet[suppress routine status output]'
        '--verbose[print detailed reduction progress]'
    )
    _arguments -s $options '*:repository or command:_files'
}

_repomin "$@"
'''


_FISH = r'''# Fish completion for repomin.
function __repomin_demo_mode
    set -l tokens (commandline -opc)
    test (count $tokens) -ge 2; and test "$tokens[2]" = demo
end

function __repomin_demo_needs_workspace
    __repomin_demo_mode; and test (count (commandline -opc)) -eq 2
end

complete -c repomin -f -n '__fish_use_subcommand' -a 'completion demo doctor report' -d 'command'
complete -c repomin -f -n '__fish_seen_subcommand_from completion' -a 'bash zsh fish powershell' -d 'shell'
complete -c repomin -f -n '__repomin_demo_mode; and __repomin_demo_needs_workspace' -a '(__fish_complete_directories)' -d 'new workspace'
complete -c repomin -f -n '__repomin_demo_mode; and __repomin_demo_needs_workspace' -l help -d 'show demo help'
complete -c repomin -f -n '__fish_seen_subcommand_from report' -a 'validate replay compare' -d 'report command'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from validate' -l payload -r -a '(__fish_complete_directories)' -d 'exported payload directory'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from validate' -l json -d 'print a machine-readable result'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from validate' -l format -r -a 'text json markdown' -d 'output format'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from compare' -l format -r -a 'text json markdown' -d 'output format'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from compare' -l label -r -d 'short report label'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from compare' -a '(__fish_complete_path)' -d 'report.json'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from replay' -l payload -r -a '(__fish_complete_directories)' -d 'exported payload directory'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from replay' -l runs -r -d 'fresh replay copies'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from replay' -l timeout -r -d 'seconds per replay run'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from replay' -l env -r -d 'recorded environment variable'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from replay' -l backend -r -a 'recorded host docker' -d 'execution backend'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from replay' -l docker-image -r -d 'local Docker image'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from replay' -l docker-network -r -a 'none bridge host' -d 'Docker network policy'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from replay' -l exit-code -r -d 'legacy exit-code contract'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from replay' -l yes -d 'acknowledge report command execution'
complete -c repomin -f -n '__fish_seen_subcommand_from report; and __fish_seen_subcommand_from replay' -l json -d 'print machine-readable replay evidence'
complete -c repomin -f -n '__fish_seen_subcommand_from doctor' -l json -d 'print a machine-readable result'
complete -c repomin -f -n '__fish_seen_subcommand_from doctor' -l format -r -a 'text json markdown' -d 'output format'
complete -c repomin -f -n '__fish_seen_subcommand_from doctor' -l gitignore -d 'apply repository .gitignore'
complete -c repomin -f -n '__fish_seen_subcommand_from doctor' -l gitignore-file -r -a '(__fish_complete_path)' -d 'apply a gitignore-style file'
complete -c repomin -f -n '__fish_seen_subcommand_from doctor' -l gitignore-recursive -d 'apply nested .gitignore files'

set -l boolean_options version help resume no-cache gitignore gitignore-recursive java-exception python-exception process-failure
for option in $boolean_options
    complete -c repomin -f -n 'not __repomin_demo_mode' -l $option
end

complete -c repomin -f -n 'not __repomin_demo_mode' -l quiet -d 'suppress routine status output'
complete -c repomin -f -n 'not __repomin_demo_mode' -l verbose -d 'print detailed reduction progress'

complete -c repomin -f -n 'not __repomin_demo_mode' -l command -r -d 'failure reproduction command'
complete -c repomin -f -n 'not __repomin_demo_mode' -l config -r -a '(__fish_complete_path)' -d 'versioned JSON reduction spec'
complete -c repomin -f -n 'not __repomin_demo_mode' -l match -r -d 'failure output pattern'
complete -c repomin -f -n 'not __repomin_demo_mode' -l exit-code -r -d 'required exit code'
complete -c repomin -f -n 'not __repomin_demo_mode' -l output -r -a '(__fish_complete_directories)'
complete -c repomin -f -n 'not __repomin_demo_mode' -l session -r -a '(__fish_complete_directories)'
complete -c repomin -f -n 'not __repomin_demo_mode' -l timeout -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l backend -r -a 'host docker'
complete -c repomin -f -n 'not __repomin_demo_mode' -l docker-image -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l docker-network -r -a 'none bridge host'
complete -c repomin -f -n 'not __repomin_demo_mode' -l docker-cpus -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l docker-memory -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l docker-pids-limit -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l docker-tmpfs-size -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l docker-workspace-limit -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l jobs -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l max-attempts -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l max-duration -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l ignore -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l ignore-path -r -a '(__fish_complete_path)'
complete -c repomin -f -n 'not __repomin_demo_mode' -l gitignore-file -r -a '(__fish_complete_path)'
complete -c repomin -f -n 'not __repomin_demo_mode' -l keep -r -a '(__fish_complete_path)'
complete -c repomin -f -n 'not __repomin_demo_mode' -l env -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l baseline-runs -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l min-baseline-passes -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l candidate-runs -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l min-candidate-passes -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l min-baseline-rate -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l min-candidate-rate -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l confidence -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l run-confidence -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l holdout-runs -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l min-holdout-rate -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l holdout-confidence -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l adapter -r -a 'auto none maven gradle python pipenv node composer dotnet ruby cargo go'
complete -c repomin -f -n 'not __repomin_demo_mode' -l source-reducer -r -a 'auto none java python'
complete -c repomin -f -n 'not __repomin_demo_mode' -l text-file -r -a '(__fish_complete_path)'
complete -c repomin -f -n 'not __repomin_demo_mode' -l semantic-reducer -r -a 'none http'
complete -c repomin -f -n 'not __repomin_demo_mode' -l semantic-endpoint -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l semantic-model -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l semantic-timeout -r
complete -c repomin -f -n 'not __repomin_demo_mode' -l java-classpath -r -a '(__fish_complete_path)'
'''


_POWERSHELL = r'''# PowerShell completion for repomin.
Register-ArgumentCompleter -Native -CommandName repomin -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    $options = @(
        'doctor', 'report', 'demo', 'completion',
        '--version', '--help', '--config', '--command', '--match', '--exit-code', '--output', '--json',
        '--session', '--resume', '--timeout', '--backend', '--docker-image',
        '--docker-network', '--docker-cpus', '--docker-memory',
        '--docker-pids-limit', '--docker-tmpfs-size', '--docker-workspace-limit',
        '--jobs', '--no-cache', '--max-attempts', '--max-duration', '--ignore',
        '--ignore-path', '--gitignore', '--gitignore-file', '--gitignore-recursive',
        '--keep', '--env', '--java-exception', '--python-exception',
        '--process-failure', '--baseline-runs', '--min-baseline-passes',
        '--candidate-runs', '--min-candidate-passes', '--min-baseline-rate',
        '--min-candidate-rate', '--confidence', '--run-confidence',
        '--holdout-runs', '--min-holdout-rate', '--holdout-confidence',
        '--adapter', '--source-reducer', '--text-file', '--semantic-reducer',
        '--semantic-endpoint', '--semantic-model', '--semantic-timeout',
        '--java-classpath', '--quiet', '--verbose'
    )
    $optionTooltips = @{
        '--quiet' = 'suppress routine status output'
        '--verbose' = 'print detailed reduction progress'
    }
    $doctorOptions = @(
        '--version', '--help', '--config', '--command', '--match', '--exit-code',
        '--java-exception', '--python-exception', '--process-failure', '--adapter',
        '--source-reducer', '--backend', '--docker-image', '--docker-network',
        '--docker-cpus', '--docker-memory', '--docker-pids-limit',
        '--docker-tmpfs-size', '--docker-workspace-limit', '--timeout',
        '--baseline-runs', '--min-baseline-passes', '--min-baseline-rate',
        '--confidence', '--output', '--ignore', '--ignore-path', '--keep',
        '--text-file', '--gitignore', '--gitignore-file', '--gitignore-recursive',
        '--env', '--json', '--format'
    )
    $doctorValueOptions = @(
        '--config', '--command', '--match', '--exit-code', '--adapter',
        '--source-reducer', '--backend', '--docker-image', '--docker-network',
        '--docker-cpus', '--docker-memory', '--docker-pids-limit',
        '--docker-tmpfs-size', '--docker-workspace-limit', '--timeout',
        '--baseline-runs', '--min-baseline-passes', '--min-baseline-rate',
        '--confidence', '--output', '--ignore', '--ignore-path', '--keep',
        '--text-file', '--gitignore-file', '--env', '--format'
    )
    $doctorPathOptions = @(
        '--config', '--output', '--ignore-path', '--keep', '--text-file',
        '--gitignore-file'
    )
    $reportOptions = @('validate', 'replay', 'compare', '--help')
    $reportValidateOptions = @('--help', '--payload', '--json', '--format')
    $reportReplayOptions = @(
        '--help', '--payload', '--runs', '--timeout', '--env', '--backend',
        '--docker-image', '--docker-network', '--exit-code', '--yes', '--json'
    )
    $reportCompareOptions = @('--help', '--label', '--format')
    $reportPathOptions = @('--payload')
    $valueOptions = @(
        '--config', '--command', '--match', '--exit-code', '--output', '--session', '--timeout',
        '--backend', '--docker-image', '--docker-network', '--docker-cpus',
        '--docker-memory', '--docker-pids-limit', '--docker-tmpfs-size',
        '--docker-workspace-limit', '--jobs', '--max-attempts', '--max-duration',
        '--ignore', '--ignore-path', '--gitignore-file', '--keep', '--env',
        '--baseline-runs', '--min-baseline-passes', '--candidate-runs',
        '--min-candidate-passes', '--min-baseline-rate', '--min-candidate-rate',
        '--confidence', '--run-confidence', '--holdout-runs', '--min-holdout-rate',
        '--holdout-confidence', '--adapter', '--source-reducer', '--text-file',
        '--semantic-reducer', '--semantic-endpoint', '--semantic-model',
        '--semantic-timeout', '--java-classpath'
    )
    $pathOptions = @(
        '--config', '--output', '--session', '--ignore-path', '--gitignore-file', '--keep',
        '--text-file', '--java-classpath'
    )
    $enumValues = @{
        '--backend' = @('host', 'docker')
        '--docker-network' = @('none', 'bridge', 'host')
        '--adapter' = @('auto', 'none', 'maven', 'gradle', 'python', 'pipenv', 'node', 'composer', 'dotnet', 'ruby', 'cargo', 'go')
        '--source-reducer' = @('auto', 'none', 'java', 'python')
        '--semantic-reducer' = @('none', 'http')
    }

    $elements = @($commandAst.CommandElements)
    $previous = if ($elements.Count -gt 1) {
        $elements[$elements.Count - 2].Extent.Text
    } else {
        ''
    }
    $subcommand = if ($elements.Count -gt 1) {
        $elements[1].Extent.Text
    } else {
        ''
    }
    $doctorMode = $subcommand -eq 'doctor'
    $reportMode = $subcommand -eq 'report'
    $demoMode = $subcommand -eq 'demo'
    if ($demoMode) {
        $acceptsWorkspace = $elements.Count -eq 2 -or (
            $elements.Count -eq 3 -and -not [string]::IsNullOrEmpty($wordToComplete)
        )
        if (-not $acceptsWorkspace) {
            return
        }
        if ($wordToComplete -like '-*') {
            @('--help') |
                Where-Object { $_ -like "$wordToComplete*" } |
                ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new(
                        $_, $_, 'ParameterName', 'show demo help'
                    )
                }
            return
        }
        $parentPath = if ([string]::IsNullOrEmpty($wordToComplete)) {
            '.'
        } else {
            Split-Path -Parent $wordToComplete
        }
        $leafPrefix = if ([string]::IsNullOrEmpty($wordToComplete)) {
            ''
        } else {
            Split-Path -Leaf $wordToComplete
        }
        if ([string]::IsNullOrEmpty($parentPath)) {
            $parentPath = '.'
        }
        Get-ChildItem -LiteralPath $parentPath -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name.StartsWith(
                    $leafPrefix,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            } |
            ForEach-Object {
                $completionText = $_.FullName
                if ($_.PSIsContainer) {
                    $completionText += [System.IO.Path]::DirectorySeparatorChar
                }
                $completionText = "'" + $completionText.Replace("'", "''") + "'"
                [System.Management.Automation.CompletionResult]::new(
                    $completionText, $_.Name, 'ProviderItem', $_.FullName
                )
            }
        return
    }
    if ($reportMode) {
        $validateMode = $elements | Where-Object { $_.Extent.Text -eq 'validate' }
        $replayMode = $elements | Where-Object { $_.Extent.Text -eq 'replay' }
        $compareMode = $elements | Where-Object { $_.Extent.Text -eq 'compare' }
        if (-not $validateMode -and -not $replayMode -and -not $compareMode) {
            $reportOptions |
                Where-Object { $_ -like "$wordToComplete*" } |
                ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new(
                        $_, $_, 'Command', $_
                    )
                }
            return
        }
        if ($replayMode -and $previous -eq '--backend') {
            @('recorded', 'host', 'docker') |
                Where-Object { $_ -like "$wordToComplete*" } |
                ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new(
                        $_, $_, 'ParameterValue', $_
                    )
                }
            return
        }
        if ($replayMode -and $previous -eq '--docker-network') {
            @('none', 'bridge', 'host') |
                Where-Object { $_ -like "$wordToComplete*" } |
                ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new(
                        $_, $_, 'ParameterValue', $_
                    )
                }
            return
        }
        if ($validateMode -and $previous -eq '--format') {
            @('text', 'json', 'markdown') |
                Where-Object { $_ -like "$wordToComplete*" } |
                ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new(
                        $_, $_, 'ParameterValue', $_
                    )
                }
            return
        }
        if ($compareMode -and $previous -eq '--format') {
            @('text', 'json', 'markdown') |
                Where-Object { $_ -like "$wordToComplete*" } |
                ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new(
                        $_, $_, 'ParameterValue', $_
                    )
                }
            return
        }
        if ($reportPathOptions -contains $previous) {
            $pathPattern = if ([string]::IsNullOrEmpty($wordToComplete)) {
                '*'
            } else {
                "$wordToComplete*"
            }
            Get-ChildItem -Path $pathPattern -Force -ErrorAction SilentlyContinue |
                ForEach-Object {
                    $completionText = $_.FullName
                    if ($_.PSIsContainer) {
                        $completionText += [System.IO.Path]::DirectorySeparatorChar
                    }
                    [System.Management.Automation.CompletionResult]::new(
                        $completionText, $_.Name, 'ProviderItem', $_.FullName
                    )
                }
            return
        }
        $activeReportOptions = if ($replayMode) {
            $reportReplayOptions
        } elseif ($compareMode) {
            $reportCompareOptions
        } else {
            $reportValidateOptions
        }
        $activeReportOptions |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object {
                [System.Management.Automation.CompletionResult]::new(
                    $_, $_, 'ParameterName', $_
                )
            }
        return
    }
    if ($doctorMode) {
        if ($previous -eq '--format') {
            @('text', 'json', 'markdown') |
                Where-Object { $_ -like "$wordToComplete*" } |
                ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new(
                        $_, $_, 'ParameterValue', $_
                    )
                }
            return
        }
        if ($enumValues.ContainsKey($previous)) {
            $enumValues[$previous] |
                Where-Object { $_ -like "$wordToComplete*" } |
                ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new(
                        $_, $_, 'ParameterValue', $_
                    )
                }
            return
        }
        if ($doctorValueOptions -contains $previous) {
            if ($doctorPathOptions -contains $previous) {
                $pathPattern = if ([string]::IsNullOrEmpty($wordToComplete)) {
                    '*'
                } else {
                    "$wordToComplete*"
                }
                Get-ChildItem -Path $pathPattern -Force -ErrorAction SilentlyContinue |
                    ForEach-Object {
                        $completionText = $_.FullName
                        if ($_.PSIsContainer) {
                            $completionText += [System.IO.Path]::DirectorySeparatorChar
                        }
                        [System.Management.Automation.CompletionResult]::new(
                            $completionText, $_.Name, 'ProviderItem', $_.FullName
                        )
                    }
            }
            return
        }
        $doctorOptions |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object {
                [System.Management.Automation.CompletionResult]::new(
                    $_, $_, 'ParameterName', $_
                )
            }
        return
    }
    if ($enumValues.ContainsKey($previous)) {
        $enumValues[$previous] |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object {
                [System.Management.Automation.CompletionResult]::new(
                    $_, $_, 'ParameterValue', $_
                )
            }
        return
    }
    if ($valueOptions -contains $previous) {
        if ($pathOptions -contains $previous) {
            $pathPattern = if ([string]::IsNullOrEmpty($wordToComplete)) {
                '*'
            } else {
                "$wordToComplete*"
            }
            Get-ChildItem -Path $pathPattern -Force -ErrorAction SilentlyContinue |
                ForEach-Object {
                    $completionText = $_.FullName
                    if ($_.PSIsContainer) {
                        $completionText += [System.IO.Path]::DirectorySeparatorChar
                    }
                    [System.Management.Automation.CompletionResult]::new(
                        $completionText, $_.Name, 'ProviderItem', $_.FullName
                    )
                }
        }
        return
    }
    $options |
        Where-Object { $_ -like "$wordToComplete*" } |
        ForEach-Object {
            $tooltip = if ($optionTooltips.ContainsKey($_)) {
                $optionTooltips[$_]
            } else {
                $_
            }
            [System.Management.Automation.CompletionResult]::new(
                $_, $_, 'ParameterName', $tooltip
            )
        }
}
'''


def completion_script(shell: str) -> str:
    """Return the completion script for one supported shell."""
    scripts = {
        "bash": _BASH,
        "zsh": _ZSH,
        "fish": _FISH,
        "powershell": _POWERSHELL,
    }
    try:
        return scripts[shell]
    except KeyError as exc:
        raise ValueError(
            "unsupported shell %r (choose one of: %s)"
            % (shell, ", ".join(SUPPORTED_SHELLS))
        ) from exc
