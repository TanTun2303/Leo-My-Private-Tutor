# CLAUDE.md — Leo

Self-hosted math & algorithms tutor. `README.md` is the original specification; `docs/DEVIATIONS.md`
records every place the build differs from it (read it before changing anything it mentions).

## Architecture (one workstation, Docker Compose)

- `nginx` + `certbot` (profile `edge`): public `https://chatleo.tngnoai.online` on 220.149.84.72:80/443, Let's Encrypt, → `open-webui`.
- `caddy` (127.0.0.1:80/443 `leo.localhost`, :8442 IP/Tailscale, `tls internal`) → `open-webui` (v0.11.4, Postgres + pgvector).
- `leo-llm`: llama.cpp CUDA `llama-server`, Qwen3.6-35B-A3B UD-IQ4_XS, alias `leo`, thinking **off** by
  default (`--chat-template-kwargs`), per-request `chat_template_kwargs.enable_thinking` turns it on.
  Launched by `services/llm/entrypoint.sh` from `LEO_*` env vars. Tuned: `LEO_N_CPU_MOE=20`, `LEO_UBATCH=2048`.
- `leo-embed`: llama.cpp CPU, Qwen3-Embedding-0.6B (1024-d), alias `leo-embed`. GPU only during `make ingest`
  (`compose.ingest.yaml`).
- `searxng` (egress net) for web search; Open WebUI exposes it as a **native tool** (`search_web`).
- `leo-memory` (FastAPI) + `leo-memory-worker` (`services/memory`, one image): per-chat summaries,
  learner profile, pinned facts in DB `leo_memory` (schema `memory`). Worker summarizes after
  `MEMORY_IDLE_SECONDS` via leo-llm (thinking off, JSON schema), embeds, merges profile, reconciles
  deleted chats hourly (role `leo_reconcile`, `SELECT (id)` on `public.chat`).
- Projects = Open WebUI folders (instructions + files native); leo-memory scopes everything by `project_id`
  (folder id, `''` = global) — filter/tools resolve it via `Chats.get_chat_folder_id`.
- Open WebUI plugins (`services/openwebui`, provisioned by `make bootstrap` → `services/bootstrap/bootstrap.py`):
  - `leo_memory_filter` (global, priority 0): inlet recall → `<leo_memory>` appended to first system msg;
    outlet posts the turn (reads title from Open WebUI's `Chats`).
  - `leo_think_toggle` (toggle, priority 10, shown in the Integrations menu).
  - `leo_latex_normalizer` (global, priority 100): `normalize_math()` + `StreamNormalizer` (stream hook +
    outlet). Line/block state machine; streaming output == whole-text output (property/fuzz tested).
  - `leo_memory_tools`: remember / forget / what_do_you_remember.
  - Workspace model `leo-study` ("Leo") on base `leo` (hidden); system prompt `prompts/leo_system_prompt.md`
    (`[[INLINE_EXAMPLE]]` filled by bootstrap); Leo Library knowledge; trimmed `builtinTools`.
- Ingest (`services/ingest`, profile `ingest`): Marker 2.0 (CPU torch) + `surya-server` (llama.cpp CUDA,
  surya-2 GGUF) → one Markdown per outline chapter → Open WebUI files + knowledge. Resumable via
  `library/manifest.json` (`books`, `partial`).

Networks: `frontend` (caddy, open-webui), `backend` (internal, no internet), `egress`, `testnet` (tests).

## Commands

`make help` lists everything. Key ones: `up`, `down`, `status`, `logs s=<svc>`, `bootstrap`, `ingest`,
`library`, `tune`, `backup`, `restore TS=`, `update`, `lint`, `test-memory`, `test-normalizer`,
`test-ui`, `test` (full suite, ~15 min). Runbook: `docs/OPERATIONS.md`.

## Conventions

- Everything pinned in `.env.example` (no `:latest`). Empty `.env` values must not have an inline comment (Compose keeps it as the value). Secrets only in `.env` (600, gitignored).
  Never `source .env` (values contain spaces/comments) — use `env_get` from `scripts/lib.sh`.
- Shell: `set -euo pipefail`, shellcheck clean (`make lint` runs it and ruff in containers).
- Python: typed, `ruff` (root `ruff.toml`; `services/memory/pyproject.toml`), pytest.
- Open WebUI plugin files must be self-contained and must never raise (catch-all is intentional).
- Filter changes go live only after `make bootstrap`; memory service changes need
  `docker compose up -d --build leo-memory leo-memory-worker`.
- Verify Open WebUI behaviour against the pinned source (`backend/open_webui/…` at the tag) — many
  settings are DB-backed ConfigVars (bootstrap sets them via API).
- Tests never touch the owner's data: memory tests use a throwaway DB; e2e tests create/delete a
  temporary account; `restore.sh --verify` restores into throwaway volumes.
- Long GPU jobs: the host hard-reset twice under load; GPU capped at 280 W (not persistent across reboots).

## Gotchas learned the hard way

- Open WebUI v0.11.4 attaches built-in tools (web search, knowledge, code) only to requests with a
  `session_id` (the browser); API smoke tests use `scripts/lib/owui_client.py` or Playwright.
- The outlet message list comes from the `chat_message` table — chats created via API may lack it.
- Code blocks render in CodeMirror (`.cm-content`), older messages load on scroll-up.
- Knowledge add re-embeds every chunk (upload already embedded them once); CPU embedding runs ~32 s per 32 chunks, GPU ~0.6 s.
- `pkill -f <pattern>` from a tool shell can kill that shell itself — kill by PID.
