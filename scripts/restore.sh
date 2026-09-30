#!/usr/bin/env bash
# `make restore TS=<timestamp>` — restore a backup made by scripts/backup.sh (asks first).
# `scripts/restore.sh --verify <timestamp>` — restore into throwaway fresh volumes and compare
#   row counts with the live stack; touches no live data (used by `make test`).
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

cd "$LEO_ROOT"
verify=false
if [[ "${1:-}" == "--verify" ]]; then verify=true; shift; fi
ts="${1:-}"
[[ "$ts" == "latest" && -f backups/LATEST ]] && ts="$(cat backups/LATEST)"
src="backups/$ts"
[[ -n "$ts" && -d "$src" ]] || die "no backup '$ts' (see: ls backups)"
(cd "$src" && sha256sum --quiet -c SHA256SUMS) || die "checksum mismatch in $src"
ok "checksums verified ($src)"

PG_IMAGE="pgvector/pgvector:$(env_get PGVECTOR_TAG)"

# restore_db <container> <db> <owner> <dumpfile>
# The database is recreated empty with pgvector, then objects are restored as <owner>
# (the extension itself stays owned by the superuser, as in init/01-init.sh).
restore_db() {
  local ctr="$1" db="$2" owner="$3" dump="$4"
  docker exec -i "$ctr" psql -v ON_ERROR_STOP=1 -q -U postgres -d postgres <<SQL
\o /dev/null
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$db' AND pid <> pg_backend_pid();
\o
DROP DATABASE IF EXISTS $db;
CREATE DATABASE $db OWNER $owner;
REVOKE ALL ON DATABASE $db FROM PUBLIC;
\connect $db
CREATE EXTENSION IF NOT EXISTS vector;
SQL
  docker cp "$dump" "$ctr:/tmp/$db.dump"
  docker exec "$ctr" sh -c "pg_restore -l /tmp/$db.dump | grep -v -E ' EXTENSION | COMMENT .* EXTENSION ' > /tmp/$db.list"
  docker exec "$ctr" pg_restore -U postgres --no-owner --role="$owner" -L "/tmp/$db.list" -d "$db" --exit-on-error "/tmp/$db.dump"
  docker exec "$ctr" rm -f "/tmp/$db.dump" "/tmp/$db.list"
  [[ "$db" != openwebui ]] || docker exec "$ctr" psql -q -U postgres -d postgres -c "GRANT CONNECT ON DATABASE openwebui TO leo_reconcile"
}

counts() {  # counts <container> → "name=value" lines
  local ctr="$1"
  docker exec "$ctr" psql -At -U postgres -d openwebui -c \
    "SELECT 'chats=' || count(*) FROM chat UNION ALL SELECT 'knowledge=' || count(*) FROM knowledge
     UNION ALL SELECT 'files=' || count(*) FROM file UNION ALL SELECT 'chunks=' || count(*) FROM document_chunk
     UNION ALL SELECT 'users=' || count(*) FROM \"user\""
  docker exec "$ctr" psql -At -U postgres -d leo_memory -c \
    "SELECT 'summaries=' || count(*) FROM memory.chat_summaries UNION ALL SELECT 'facts=' || count(*) FROM memory.facts
     UNION ALL SELECT 'profiles=' || count(*) FROM memory.learner_profile"
}

if $verify; then
  log "Verifying $ts by restoring into fresh volumes"
  ctr="leo-restore-verify-$$"
  vol_pg="leo-restore-verify-pg-$$"
  vol_owui="leo-restore-verify-owui-$$"
  # shellcheck disable=SC2329  # invoked by the EXIT trap
  cleanup() { docker rm -f "$ctr" >/dev/null 2>&1 || true; docker volume rm -f "$vol_pg" "$vol_owui" >/dev/null 2>&1 || true; }
  trap cleanup EXIT
  docker run -d --name "$ctr" --network none -v "$vol_pg:/var/lib/postgresql/data" \
    -v "$LEO_ROOT/services/postgres/init:/docker-entrypoint-initdb.d:ro" \
    -e POSTGRES_PASSWORD=verify -e OPENWEBUI_DB_PASSWORD=verify -e LEO_MEMORY_DB_PASSWORD=verify \
    -e LEO_RECONCILE_DB_PASSWORD=verify "$PG_IMAGE" >/dev/null
  for _ in $(seq 60); do docker exec "$ctr" pg_isready -U postgres -q 2>/dev/null && break; sleep 1; done
  sleep 2  # init scripts run before the final server start
  for _ in $(seq 60); do docker exec "$ctr" pg_isready -U postgres -q 2>/dev/null && break; sleep 1; done
  restore_db "$ctr" openwebui openwebui "$src/openwebui.dump"
  restore_db "$ctr" leo_memory leo_memory "$src/leo_memory.dump"
  docker run --rm -v "$vol_owui:/data" -v "$LEO_ROOT/$src:/b:ro" alpine:3.22 tar xzf /b/openwebui-data.tar.gz -C /data
  restored="$(counts "$ctr")"
  live="$(counts "$(compose ps -q postgres)")"
  printf '  %-12s %10s %10s\n' item backup live
  status=0
  while IFS='=' read -r k v; do
    lv="$(grep "^$k=" <<<"$live" | cut -d= -f2)"
    mark="✔"; [[ "$v" == "$lv" ]] || { mark="≠ (changed since the backup?)"; status=1; }
    printf '  %-12s %10s %10s  %s\n' "$k" "$v" "$lv" "$mark"
  done <<<"$restored"
  up="$(docker run --rm -v "$vol_owui:/data:ro" alpine:3.22 sh -c 'find /data -type f | wc -l')"
  ok "open-webui data restored into a fresh volume ($up files)"
  [[ $status -eq 0 ]] || die "restored data differs from the live stack"
  log "Backup $ts restores cleanly"
  exit 0
fi

cat <<MSG

  This REPLACES the current databases (openwebui, leo_memory), Open WebUI data,
  Caddy data and the library manifest/Markdown with backup $ts.
  Current data is lost unless you back it up first (make backup).

MSG
confirm "Restore $ts now?" || { warn "cancelled"; exit 1; }

log "Stopping services that write data"
compose stop caddy open-webui leo-memory leo-memory-worker
pg="$(compose ps -q postgres)"
[[ -n "$pg" ]] || { compose up -d --wait postgres; pg="$(compose ps -q postgres)"; }

restore_db "$pg" openwebui openwebui "$src/openwebui.dump"
ok "database openwebui"
restore_db "$pg" leo_memory leo_memory "$src/leo_memory.dump"
ok "database leo_memory"

vol_restore() {  # vol_restore <compose volume> <archive>; keeps a re-downloadable ./cache
  docker run --rm -v "${COMPOSE_PROJECT_NAME:-leo}_$1:/data" -v "$LEO_ROOT/$src:/b:ro" alpine:3.22 \
    sh -c "find /data -mindepth 1 -maxdepth 1 ! -name cache -exec rm -rf {} + && tar xzf /b/$2 -C /data"
}
vol_restore openwebui-data openwebui-data.tar.gz
ok "open-webui data"
vol_restore caddy-data caddy-data.tar.gz
ok "caddy data"
rm -rf library/markdown && tar xzf "$src/library.tar.gz" -C library
ok "library manifest + Markdown"

if [[ ! -f .env ]]; then
  if [[ -f "$src/env" ]]; then cp "$src/env" .env && chmod 600 .env && ok ".env restored"
  else warn ".env is encrypted in the backup: age -d -i <key> -o .env $src/env.age"; fi
else
  ok ".env kept (the backup copy is in $src)"
fi

log "Starting the stack"
compose up -d --wait
log "Restore of $ts complete"
