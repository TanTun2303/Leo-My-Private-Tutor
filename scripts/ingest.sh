#!/usr/bin/env bash
# `make ingest` — convert books in library/inbox with Marker and upload them to Leo's Knowledge base (§8).
# Stops leo-llm to free VRAM for the OCR model; a trap always starts it again.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

shopt -s nullglob nocaseglob
books=("$LEO_ROOT"/library/inbox/*.{pdf,epub,docx})
shopt -u nocaseglob
if [[ ${#books[@]} -eq 0 ]]; then
  ok "library/inbox is empty — nothing to ingest"
  exit 0
fi
log "Books in inbox: ${#books[@]}"

if [[ ! -f "$LEO_ROOT/data/models/surya/surya-2.gguf" || ! -f "$LEO_ROOT/data/models/surya/surya-2-mmproj.gguf" ]]; then
  "$LEO_ROOT/scripts/fetch-models.sh" surya
  [[ -f "$LEO_ROOT/data/models/surya/surya-2.gguf" ]] || die "Surya OCR model missing"
fi

restore() {
  log "Restoring leo-embed (CPU) and leo-llm"
  compose --profile ingest stop surya-server >/dev/null 2>&1 || true
  compose --profile ingest rm -f surya-server >/dev/null 2>&1 || true
  compose up -d --wait --no-deps --force-recreate leo-embed || warn "leo-embed did not become healthy"
  compose up -d --wait leo-llm || warn "leo-llm did not become healthy — check: make logs s=leo-llm"
}
trap restore EXIT

log "Stopping leo-llm (frees VRAM for OCR)"
compose stop leo-llm
log "Moving leo-embed to the GPU for fast chunk embedding"
compose -f "$LEO_ROOT/compose.ingest.yaml" up -d --wait --no-deps --force-recreate leo-embed
log "Starting surya-server"
compose --profile ingest up -d --wait surya-server
log "Converting and uploading"
mkdir -p "$LEO_ROOT/data/ingest-cache"
compose --profile ingest run --rm --build ingest run
