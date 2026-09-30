#!/usr/bin/env bash
# Stack health, VRAM/RAM usage and last generation speed.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

log "Services"
compose ps --format 'table {{.Service}}\t{{.Status}}'

log "Container memory"
ids="$(compose ps -q)"
if [[ -n "$ids" ]]; then
  # shellcheck disable=SC2086
  docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' $ids
fi

log "GPU"
nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null \
  || warn "nvidia-smi not available on the host"

log "Last generation speed (leo-llm)"
if compose ps --status running -q leo-llm 2>/dev/null | grep -q .; then
  key="$(env_get LLM_API_KEY)"
  compose exec -T leo-llm curl -fsS -H "Authorization: Bearer $key" http://localhost:8080/metrics 2>/dev/null \
    | awk '/^llamacpp:(predicted_tokens_seconds|prompt_tokens_seconds|requests_processing|n_decode_total)/ {printf "  %-40s %s\n", $1, $2}' \
    || warn "metrics unavailable"
else
  warn "leo-llm not running"
fi
