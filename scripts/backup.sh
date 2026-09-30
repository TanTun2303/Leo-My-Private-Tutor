#!/usr/bin/env bash
# `make backup` — back up both databases, Open WebUI data, Caddy's CA, the library
# manifest/Markdown and .env into backups/<timestamp>/. Keeps the newest 14.
#
# .env is encrypted with `age` when LEO_BACKUP_AGE_RECIPIENT (an age public key) is set
# in .env and `age` is installed; otherwise it is copied with mode 600.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

KEEP=14
cd "$LEO_ROOT"
[[ -f .env ]] || die ".env missing"
compose ps --status running -q postgres | grep -q . || die "postgres is not running (make up)"

ts="$(date +%Y%m%d-%H%M%S)"
dest="backups/$ts"
umask 077
mkdir -p "$dest"
log "Backup $ts"

for db in openwebui leo_memory; do
  compose exec -T postgres pg_dump -U postgres -Fc "$db" > "$dest/$db.dump"
  ok "database $db ($(du -h "$dest/$db.dump" | cut -f1))"
done

volume_tar() {  # volume_tar <compose volume> <output> [tar excludes...]
  local vol="${COMPOSE_PROJECT_NAME:-leo}_$1" out="$2"
  shift 2
  docker run --rm -v "$vol:/data:ro" alpine:3.22 tar czf - -C /data "$@" . > "$out"
}
# The reranker/model cache is re-downloadable and large; leave it out.
volume_tar openwebui-data "$dest/openwebui-data.tar.gz" --exclude=./cache
ok "open-webui data ($(du -h "$dest/openwebui-data.tar.gz" | cut -f1), cache excluded)"
volume_tar caddy-data "$dest/caddy-data.tar.gz"
ok "caddy data (local CA)"

tar czf "$dest/library.tar.gz" -C library manifest.json markdown
ok "library manifest + Markdown ($(du -h "$dest/library.tar.gz" | cut -f1))"

recipient="$(env_get LEO_BACKUP_AGE_RECIPIENT 2>/dev/null || true)"
if [[ -n "$recipient" ]] && command -v age >/dev/null; then
  age -r "$recipient" -o "$dest/env.age" .env
  ok ".env encrypted with age"
else
  cp .env "$dest/env"
  chmod 600 "$dest/env"
  warn ".env stored unencrypted (mode 600) — set LEO_BACKUP_AGE_RECIPIENT and install age to encrypt"
fi

(cd "$dest" && sha256sum -- * > SHA256SUMS)
ok "checksums written"

mapfile -t old < <(find backups -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | head -n -"$KEEP")
for d in "${old[@]}"; do rm -rf -- "$d"; done
[[ ${#old[@]} -eq 0 ]] || ok "pruned ${#old[@]} old backup(s)"

log "Done: $dest ($(du -sh "$dest" | cut -f1))"
echo "$ts" > backups/LATEST
