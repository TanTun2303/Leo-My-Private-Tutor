#!/usr/bin/env bash
# `make test` — unit tests, UI tests and the end-to-end smoke tests of §12.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"
cd "$LEO_ROOT"

results=()
run() {  # run <name> <command...>
  local name="$1"; shift
  log "$name"
  local t0=$SECONDS
  if "$@"; then results+=("✔ $name ($((SECONDS - t0))s)"); else results+=("✘ $name ($((SECONDS - t0))s)"); fi
}

counts() {
  compose exec -T postgres psql -At -U postgres -d openwebui -c \
    "SELECT (SELECT count(*) FROM chat) || ' ' || (SELECT count(*) FROM knowledge) || ' ' || (SELECT count(*) FROM document_chunk)"
  compose exec -T postgres psql -At -U postgres -d leo_memory -c \
    "SELECT (SELECT count(*) FROM memory.chat_summaries) || ' ' || (SELECT count(*) FROM memory.facts)"
}

healthy() {
  local bad
  bad="$(compose ps --format '{{.Service}} {{.Health}}' | awk '$2 != "healthy" {print $1}')"
  [[ -z "$bad" ]] || { fail "unhealthy: $bad"; return 1; }
  ok "all services healthy"
}

restart_persists() {
  local before after
  before="$(counts)"
  compose restart >/dev/null 2>&1
  compose up -d --wait >/dev/null
  after="$(counts)"
  [[ "$before" == "$after" ]] || { fail "data changed across restart: $before → $after"; return 1; }
  ok "data persisted across restart"
}

backup_restore() {
  ./scripts/backup.sh >/dev/null
  ./scripts/restore.sh --verify latest
}

e2e() {
  compose --profile tools run --rm --build -v "$LEO_ROOT/tests/e2e:/e2e:ro" ui-test \
    pytest -q -p no:cacheprovider -o cache_dir=/tmp /e2e
}

run "unit: leo-memory" make -s test-memory
run "unit: LaTeX normalizer" make -s test-normalizer
run "1. services healthy" healthy
run "1. restart keeps data" restart_persists
run "3. UI: render matrix, live math, library" make -s test-ui
run "2/4/5/6/7/9. end-to-end smoke" e2e
run "8. backup → restore into fresh volumes" backup_restore

echo
log "Results"
printf '  %s\n' "${results[@]}"
! printf '%s\n' "${results[@]}" | grep -q "^✘"
