#!/usr/bin/env bash
# `make tune` — measure llama-server settings and write docs/TUNING.md (§13).
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

[[ -f "$ENV_FILE" ]] || die ".env missing — run make init"
model="$(env_get LEO_MODEL_FILE)"
[[ -f "$LEO_ROOT/data/models/$model" ]] || die "model $model missing — run make models"
log "Tuning leo-llm (restarts it several times; takes ~20–40 min)"
# One tuning run at a time: concurrent runs recreate leo-llm under each other.
rc=0
flock -n -E 75 "$LEO_ROOT/data/.tune.lock" python3 "$LEO_ROOT/scripts/tune.py" "$@" || rc=$?
[[ $rc -ne 75 ]] || die "another make tune is already running"
exit "$rc"
