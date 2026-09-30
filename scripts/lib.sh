#!/usr/bin/env bash
# Shared helpers for Leo scripts. Source this file; do not execute it.
# shellcheck disable=SC2034

LEO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${LEO_ROOT}/.env"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✘\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# env_get KEY [FILE] — read one value from a dotenv file, stripping inline comments
# and surrounding quotes. Never `source` .env: values may contain spaces.
env_get() {
  local key="$1" file="${2:-$ENV_FILE}"
  [[ -f "$file" ]] || return 1
  awk -v k="$key" '
    $0 ~ "^[[:space:]]*"k"=" {
      sub("^[[:space:]]*"k"=", "")
      sub(/[[:space:]]+#.*$/, "")
      sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, "")
      gsub(/^["\047]|["\047]$/, "")
      print; found=1; exit
    }
    END { exit found ? 0 : 1 }' "$file"
}

confirm() {
  local prompt="$1" reply
  if [[ "${LEO_YES:-}" == "1" ]]; then return 0; fi
  read -r -p "$prompt [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

compose() { docker compose --project-directory "$LEO_ROOT" -f "$LEO_ROOT/compose.yaml" "$@"; }
