#!/usr/bin/env bash
# Download / verify GGUF model files into data/models (§6).
#
#   ./scripts/fetch-models.sh required   # Leo + vision projector + embeddings
#   ./scripts/fetch-models.sh extra      # optional A/B candidates for `make tune`
#
# On the host this asks for confirmation and re-runs itself inside the
# `models-fetch` container (python + huggingface_hub, egress network only).
# Resumable: files already present with the expected size are skipped.
set -euo pipefail

HF_HUB_VERSION=2.0.0
MTP_REPO=unsloth/Qwen3.6-35B-A3B-MTP-GGUF
IQ3_FILE=Qwen3.6-35B-A3B-UD-IQ3_XXS.gguf

set_name="${1:-required}"
case "$set_name" in required|extra|surya) ;; *) echo "usage: $0 required|extra|surya" >&2; exit 2;; esac

# ── host side ──────────────────────────────────────────────────────
if [[ "${LEO_IN_CONTAINER:-}" != "1" ]]; then
  # shellcheck source=scripts/lib.sh
  source "$(dirname "$0")/lib.sh"
  if [[ "$set_name" == surya ]]; then
    msg="Download the Surya OCR model for book ingestion (~1.5 GB: surya-2.gguf + mmproj)?"
  elif [[ "$set_name" == required ]]; then
    msg="Download the required models (~19.3 GB: $(env_get LEO_MODEL_FILE), $(env_get LEO_MMPROJ_FILE), $(env_get EMBED_MODEL_FILE))?"
  else
    msg="Download optional A/B models (~32 GB: MTP $(env_get LEO_MODEL_FILE) 18.2 GB into models/mtp/, ${IQ3_FILE} 13.2 GB)?"
  fi
  confirm "$msg" || { warn "skipped"; exit 0; }
  mkdir -p "$LEO_ROOT/data/models"
  exec docker compose --project-directory "$LEO_ROOT" --profile tools run --rm \
    -e LEO_IN_CONTAINER=1 models-fetch "$set_name"
fi

# ── container side ─────────────────────────────────────────────────
pip install -q --disable-pip-version-check --root-user-action=ignore "huggingface_hub==${HF_HUB_VERSION}"

python3 - "$set_name" <<PY
import os, sys
from pathlib import Path
from huggingface_hub import HfApi, hf_hub_download

def env(k):
    v = os.environ.get(k, "")
    return v.split(" #")[0].strip()

wanted = []  # (repo, filename, subdir)
if sys.argv[1] == "required":
    wanted.append((env("LEO_MODEL_REPO"), env("LEO_MODEL_FILE"), ""))
    if env("LEO_MMPROJ_FILE"):
        wanted.append((env("LEO_MODEL_REPO"), env("LEO_MMPROJ_FILE"), ""))
    wanted.append((env("EMBED_MODEL_REPO"), env("EMBED_MODEL_FILE"), ""))
elif sys.argv[1] == "surya":
    wanted.append(("datalab-to/surya-ocr-2-gguf", "surya-2.gguf", "surya"))
    wanted.append(("datalab-to/surya-ocr-2-gguf", "surya-2-mmproj.gguf", "surya"))
else:
    wanted.append(("${MTP_REPO}", env("LEO_MODEL_FILE"), "mtp"))
    wanted.append((env("LEO_MODEL_REPO"), "${IQ3_FILE}", ""))

api = HfApi()
# Fail loudly before downloading anything if a configured filename doesn't exist.
sizes = {}
for repo, fname, _ in wanted:
    info = {s.path: s.size for s in api.list_repo_tree(repo, recursive=True) if hasattr(s, "size")}
    if fname not in info:
        ggufs = sorted(p for p in info if p.endswith(".gguf"))
        sys.exit(f"ERROR: {fname} not found in {repo}. Available:\n  " + "\n  ".join(ggufs))
    sizes[(repo, fname)] = info[fname]

for repo, fname, sub in wanted:
    dest = Path("/models") / sub
    dest.mkdir(parents=True, exist_ok=True)
    target, size = dest / fname, sizes[(repo, fname)]
    if target.exists() and target.stat().st_size == size:
        print(f"✔ {sub + '/' if sub else ''}{fname} present ({size/1e9:.2f} GB)")
        continue
    print(f"↓ {repo} :: {fname} ({size/1e9:.2f} GB)", flush=True)
    hf_hub_download(repo, fname, local_dir=str(dest))
    got = target.stat().st_size
    if got != size:
        sys.exit(f"ERROR: size mismatch for {fname}: {got} != {size}")
    print(f"✔ {fname} done", flush=True)
PY
