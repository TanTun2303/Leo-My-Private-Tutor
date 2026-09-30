#!/usr/bin/env bash
# Host checks for Leo (§1). Read-only: reports, never changes host configuration.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

errors=0
bad() { fail "$*"; errors=$((errors + 1)); }

is_wsl=false
grep -qi microsoft /proc/version 2>/dev/null && is_wsl=true

log "Docker"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  ok "docker $(docker version --format '{{.Server.Version}}')"
else
  bad "docker not reachable (is the daemon running and are you in the docker group?)"
fi
if docker compose version >/dev/null 2>&1; then
  ok "compose $(docker compose version --short)"
else
  bad "docker compose plugin missing"
fi

log "GPU"
if gpu="$(docker run --rm --gpus all ubuntu:24.04 nvidia-smi \
    --query-gpu=name,memory.total,memory.used,driver_version,display_active \
    --format=csv,noheader 2>/dev/null)"; then
  ok "visible in containers: $gpu"
  IFS=',' read -r _ vram_total _ _ display <<<"$gpu"
  vram_mib="${vram_total//[^0-9]/}"
  [[ "${vram_mib:-0}" -ge 11500 ]] || bad "less than 12 GB VRAM"
  if [[ "${display// /}" == "Enabled" ]]; then
    warn "the NVIDIA GPU drives a display — move the monitor to the motherboard (iGPU) to free 0.5–1.5 GB VRAM"
  else
    ok "GPU is not driving a display (all VRAM available)"
  fi
else
  bad "GPU not visible in containers (install NVIDIA Container Toolkit / enable Docker Desktop GPU support)"
fi

log "CPU"
cores="$(lscpu -p=core 2>/dev/null | grep -v '^#' | sort -u | wc -l)"
ok "$(lscpu | sed -n 's/^Model name:[[:space:]]*//p') — ${cores} physical cores"
if grep -qw avx2 /proc/cpuinfo; then ok "AVX2"; else bad "AVX2 missing"; fi

log "System RAM"
total_gb=$(awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo)
avail_gb=$(awk '/MemAvailable/ {printf "%d", $2/1048576}' /proc/meminfo)
ok "total ${total_gb} GB, available ${avail_gb} GB"
[[ "$avail_gb" -ge 17 ]] || warn "under ~17 GB available — the stack may swap"
if $is_wsl && [[ "$total_gb" -lt 23 ]]; then
  warn "WSL2 VM limited to ${total_gb} GB — set memory=24GB in %UserProfile%\\.wslconfig, then wsl --shutdown"
fi

log "Memory modules"
dmi=""
if [[ $EUID -eq 0 ]]; then
  dmi="$(dmidecode -t memory 2>/dev/null || true)"
elif sudo -n true 2>/dev/null; then
  dmi="$(sudo -n dmidecode -t memory 2>/dev/null || true)"
fi
if [[ -n "$dmi" ]]; then
  printf '%s\n' "$dmi" | awk '
    /^Memory Device/ { dev=1; size=""; loc=""; speed=""; conf="" }
    dev && /^\tSize:/ { sub(/^\tSize: /, ""); size=$0 }
    dev && /^\tLocator:/ { sub(/^\tLocator: /, ""); loc=$0 }
    dev && /^\tSpeed:/ { sub(/^\tSpeed: /, ""); speed=$0 }
    dev && /^\tConfigured Memory Speed:/ { sub(/^\tConfigured Memory Speed: /, ""); conf=$0 }
    dev && /^$/ { if (size != "") printf "    %-10s %-14s rated %-12s running %s\n", loc, size, speed, conf; dev=0 }'
  sizes="$(printf '%s\n' "$dmi" | awk '/^\tSize: [0-9]/ {print $2}' | sort -u | wc -l)"
  [[ "$sizes" -le 1 ]] || warn "mixed module sizes — Intel Flex Mode runs the unmatched capacity single-channel (slower expert offload)"
  warn "if 'running' is below 'rated', enable XMP in the BIOS (Z-series boards)"
elif $is_wsl; then
  warn "on Windows run: wmic memorychip get capacity,speed,devicelocator"
else
  warn "module layout needs root: sudo dmidecode -t memory | grep -E 'Size|Speed|Locator'"
fi

log "Disk & location"
free_gb=$(df -Pk "$LEO_ROOT" | awk 'NR==2 {printf "%d", $4/1048576}')
if [[ "$free_gb" -ge 50 ]]; then ok "${free_gb} GB free"; else bad "only ${free_gb} GB free (need ~50 GB)"; fi
case "$LEO_ROOT" in
  /mnt/[a-z]/*) bad "repo is under $LEO_ROOT — move it into the WSL2 filesystem (e.g. ~/leo)";;
  *) ok "repo at $LEO_ROOT";;
esac

log "Ports"
for p in "$(env_get LEO_HTTPS_PORT || echo 443)" "$(env_get LEO_HTTP_PORT || echo 80)"; do
  if ss -ltnH "sport = :$p" 2>/dev/null | grep -q .; then
    if docker ps --format '{{.Names}} {{.Ports}}' | grep -q "^leo-caddy.*:$p->"; then ok "port $p (leo caddy)"
    else bad "port $p already in use"; fi
  else ok "port $p free"; fi
done

echo
if [[ $errors -eq 0 ]]; then log "All required checks passed"; else die "$errors check(s) failed"; fi
