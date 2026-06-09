#compdef jarvis jarvis-backup jarvis-calendar jarvis-contacts jarvis-cookbook jarvis-docs jarvis-gallery jarvis-mail jarvis-mcp jarvis-memory jarvis-notes jarvis-personal jarvis-preset jarvis-research jarvis-sessions jarvis-signature jarvis-skills jarvis-tasks jarvis-theme jarvis-webhook
# Zsh tab-completion for the jarvis umbrella + sub-CLIs.
#
# Drop in any directory on $fpath, e.g.:
#     fpath=(/path/to/jarvis-ui/scripts/_completion $fpath)
#     autoload -U compinit; compinit
#
# Then `jarvis <tab>` completes subcommands; `jarvis mail <tab>`
# completes mail subcommands; `jarvis-mail <tab>` works the same.

_jarvis_scripts_dir() {
    local self="${(%):-%x}"
    while [[ -L "$self" ]]; do self="$(readlink "$self")"; done
    cd "${self:h}/.." && pwd
}

typeset -gA _jarvis_subs

_jarvis_refresh() {
    _jarvis_subs=()
    local dir="$(_jarvis_scripts_dir)"
    local py="$dir/../venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3)"
    local f sub help_out commands
    for f in "$dir"/jarvis-*; do
        [[ -x "$f" ]] || continue
        case "$f" in
            *.bak|*.pyc|*.pre-*) continue ;;
        esac
        sub="${${f:t}#jarvis-}"
        help_out=$("$py" "$f" --help 2>/dev/null) || continue
        commands=$(echo "$help_out" | grep -oE '\{[a-z0-9_,-]+\}' | head -1 \
            | tr -d '{}' | tr ',' ' ')
        _jarvis_subs[$sub]="$commands"
    done
}

_jarvis() {
    [[ ${#_jarvis_subs} -eq 0 ]] && _jarvis_refresh

    local cmd="${words[1]}"

    if [[ "$cmd" == "jarvis" ]]; then
        if (( CURRENT == 2 )); then
            local -a subs=(${(k)_jarvis_subs} help)
            _describe 'subcommand' subs
            return
        fi
        local sub="${words[2]}"
        if [[ "$sub" == "help" ]] && (( CURRENT == 3 )); then
            local -a subs=(${(k)_jarvis_subs})
            _describe 'subcommand' subs
            return
        fi
        if (( CURRENT == 3 )); then
            local -a sc=(${(s/ /)_jarvis_subs[$sub]})
            _describe 'command' sc
            return
        fi
        return
    fi

    # jarvis-foo <tab>
    local sub="${cmd#jarvis-}"
    if (( CURRENT == 2 )); then
        local -a sc=(${(s/ /)_jarvis_subs[$sub]})
        _describe 'command' sc
        return
    fi
}

_jarvis "$@"
