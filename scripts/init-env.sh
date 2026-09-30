#!/usr/bin/env bash
# Create .env from .env.example with freshly generated secrets.
# Idempotent: refuses to overwrite an existing .env.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

cd "$LEO_ROOT"
if [[ -f .env ]]; then
  ok ".env already exists — leaving it untouched (delete it manually to regenerate)"
  exit 0
fi
command -v openssl >/dev/null || die "openssl is required"

umask 077
tmp="$(mktemp .env.XXXXXX)"
trap 'rm -f "$tmp"' EXIT

# 24 URL-safe characters (18 random bytes) — well under bcrypt's 72-byte limit.
admin_password="$(openssl rand -base64 18 | tr '+/' '-_')"

while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" == WEBUI_ADMIN_PASSWORD=__GENERATE__* ]]; then
    line="WEBUI_ADMIN_PASSWORD=${admin_password}"
  elif [[ "$line" == *=__GENERATE__* ]]; then
    line="${line%%=*}=$(openssl rand -hex 32)"
  elif [[ -n "${ADMIN_EMAIL:-}" && "$line" == WEBUI_ADMIN_EMAIL=* ]]; then
    line="WEBUI_ADMIN_EMAIL=${ADMIN_EMAIL}"
  fi
  printf '%s\n' "$line"
done < .env.example > "$tmp"

if grep -q '__GENERATE__\|__PIN__' "$tmp"; then
  die "unresolved placeholders remain in .env.example"
fi

mv "$tmp" .env
trap - EXIT
chmod 600 .env
log "Created .env (mode 600)"
printf '\n  Admin email:    %s\n' "$(env_get WEBUI_ADMIN_EMAIL)"
printf '  Admin password: %s\n\n' "$admin_password"
warn "This password is shown once. Store it in your password manager."
