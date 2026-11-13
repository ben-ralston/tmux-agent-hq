#!/bin/bash
set -eo pipefail

show-help(){
    echo "USAGE: $(basename "$0") <command> [arguments]"
    echo ""

    # Get all commands
    commands=()
    while IFS='' read -r line; do commands+=("$line"); done < \
        <(compgen -A "function" | grep "run-" | sed "s/run-//")

    # Group commands by category
    echo "Project Commands:"
    for cmd in build install-global install-global-here test lint lint-fix agent-hq; do
        if [[ " ${commands[*]} " =~ " ${cmd} " ]]; then
            printf "  %-22s %s\n" "$cmd" "$(help-for "$cmd")"
        fi
    done
}

name=$1
case "$name" in
    "" | "-h" | "--help" | "help")
        show-help
        ;;
    *)
        shift
        if compgen -A "function" | grep "run-$name" >/dev/null ; then
            # Check for per-command help flag
            if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
                help-detail "$name"
            else
                run-"${name}" "$@"
            fi
        else
            echo "ERROR: run-$name not found."
            exit 123
        fi
        ;;
esac
