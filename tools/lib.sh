#!/usr/bin/env bash
# Shared helpers for the scripts in tools/.  Source this file, do not run it.
#
#   source "$(dirname "$0")/lib.sh"

# Absolute path of the checkout root (the parent of tools/).
repo_root() {
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    (cd "$here/.." && pwd)
}

info() { printf '[*] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Load KEY=VALUE pairs from a .env file.  Real environment variables always win,
# blank lines and # comments are ignored and one layer of quotes is stripped.
load_dotenv() {
    local file="$1" line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in '' | '#'*) continue ;; esac
        # tolerate an optional leading `export `
        case "$line" in 'export '*) line="${line#export }" ;; esac
        case "$line" in *=*) ;; *) continue ;; esac
        key="${line%%=*}"
        value="${line#*=}"
        # trim whitespace around the key
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
        # strip one layer of surrounding quotes from the value
        case "$value" in
            \"*\") value="${value#\"}" && value="${value%\"}" ;;
            \'*\') value="${value#\'}" && value="${value%\'}" ;;
        esac
        if [ -z "${!key:-}" ]; then
            export "$key=$value"
        fi
    done <"$file"
}

# Return $1 if set and non-empty, else the default in $2.
env_or() {
    if [ -n "${1:-}" ]; then printf '%s' "${1}"; else printf '%s' "${2}"; fi
}
