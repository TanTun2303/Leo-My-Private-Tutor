#!/bin/sh
# leo-llm: llama-server launcher. All tuning comes from LEO_* env vars (.env).
set -eu
set -- --host 0.0.0.0 --port 8080 \
  --model "/models/${LEO_MODEL_FILE}" --alias leo \
  --n-gpu-layers 999 --n-cpu-moe "${LEO_N_CPU_MOE}" \
  --ctx-size "${LEO_CTX}" --parallel "${LEO_PARALLEL}" --kv-unified \
  --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 \
  --threads "${LEO_THREADS}" --ubatch-size "${LEO_UBATCH}" \
  --jinja --reasoning-format deepseek --reasoning-budget "${LEO_REASONING_BUDGET}" \
  --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0 --presence-penalty 1.5 \
  --metrics --slots --no-webui

# Fast by default: thinking off unless a request (the Think toggle) turns it on.
if [ "${LEO_THINK_DEFAULT:-false}" != "true" ]; then
  set -- "$@" --chat-template-kwargs '{"enable_thinking": false}'
fi

if [ -n "${LEO_MMPROJ_FILE:-}" ]; then
  set -- "$@" --mmproj "/models/${LEO_MMPROJ_FILE}"
  [ "${LEO_MMPROJ_ON_GPU:-false}" = "true" ] || set -- "$@" --no-mmproj-offload
fi

if [ "${LEO_MTP:-false}" = "true" ]; then
  set -- "$@" --spec-type draft-mtp --spec-draft-n-max "${LEO_MTP_DRAFT_MAX:-2}"
fi

# shellcheck disable=SC2086
exec /app/llama-server "$@" ${LEO_EXTRA_ARGS:-}
