# Leo — operations runbook

Everything is driven from the repo root with `make`. Run `make help` for the full list.

## Daily use

| Task | Command |
|---|---|
| Start / stop / restart | `make up` · `make down` · `make restart` |
| Health, RAM/VRAM, last generation speed | `make status` |
| Follow logs | `make logs s=leo-llm` (any service name) |
| Open Leo (this machine) | https://leo.localhost |
| **Open Leo (anywhere)** | **https://chatleo.tngnoai.online** (trusted Let's Encrypt certificate) |
| Open Leo (your Tailscale devices) | https://100.110.187.67:8442 — install `leo-root-ca.crt` once per device |

All services use `restart: unless-stopped`, so the stack comes back by itself after a reboot.
A service stopped explicitly (for example `leo-llm` during `make ingest`) stays stopped until started again.

## First-time setup (already done on this machine)

```
make init ADMIN_EMAIL=you@example.com   # .env with fresh secrets (prints the admin password once)
make check                               # host checks
make models                              # ~19 GB of GGUF files (asks first)
make up && make bootstrap                # start + provision Open WebUI
make tune                                # pick the fastest llama.cpp settings for this GPU
make test-ui                             # confirm math rendering
```

The admin password lives only in `.env` (`grep WEBUI_ADMIN_PASSWORD .env`).

## Books

1. Put PDF / EPUB / DOCX files in `library/inbox/`.
2. `make ingest` — stops `leo-llm` (VRAM), starts `surya-server` (OCR), moves `leo-embed` to the GPU,
   converts each chapter with Marker, uploads it to the **Leo Library** knowledge base, then restores
   everything. Leo cannot answer while it runs (~2.5–3 s per page on this machine).
3. `make library` lists books; `make library-remove BOOK=<slug>` removes one (the original stays in
   `library/processed/`).

Ingest is resumable: if the machine resets mid-run, run `make ingest` again — finished chapters are kept.
`INGEST_WORKERS` (in `.env`) sets how many chapters convert in parallel; it is 1 on this host to limit
power draw (see *Hardware notes*).

## Backup & restore

| Task | Command |
|---|---|
| Back up | `make backup` → `backups/<timestamp>/` (keeps the newest 14) |
| Check a backup restores cleanly (no live data touched) | `./scripts/restore.sh --verify latest` |
| Restore (asks first, replaces current data) | `make restore TS=<timestamp>` |

A backup contains: both databases (`pg_dump -Fc`), Open WebUI's data volume (uploads; the re-downloadable
model cache is excluded), Caddy's local CA, `library/manifest.json` + converted Markdown, and `.env`.

`.env` is encrypted with [age](https://age-encryption.org) when `age` is installed and
`LEO_BACKUP_AGE_RECIPIENT=<age public key>` is set in `.env`; otherwise it is stored with mode 600.
Restoring never overwrites an existing `.env`.

Copy `backups/` to another disk regularly — it lives on the same machine.

## Upgrading

1. Pick new versions and edit the pinned tags in `.env` (and `.env.example`):
   `OPENWEBUI_VERSION`, `LLAMACPP_CUDA_TAG` / `LLAMACPP_CPU_TAG` (same build number), `PGVECTOR_TAG`,
   `SEARXNG_VERSION`, `CADDY_VERSION`, `PLAYWRIGHT_TAG`.
2. Read the upstream release notes; record anything that changes behaviour in `docs/DEVIATIONS.md`.
3. `make update` — backs up, pulls, recreates, then runs `make test`.
4. After an Open WebUI upgrade: `make bootstrap` (idempotent) and `make test-ui`; if the render matrix
   recommends a different inline style, decide whether to change `LEO_MATH_INLINE`, then `make bootstrap`.
5. After a llama.cpp upgrade: `make tune` (it proposes `.env` changes; `make tune ARGS=--apply` asks
   before writing them).

Rollback: set the old tags again and `make restore TS=<the backup make update took>`.

## Tuning

`make tune` restarts `leo-llm` with different `LEO_N_CPU_MOE` / `LEO_UBATCH` (and MTP / quant candidates
if `make models-extra` fetched them), writes `docs/TUNING.md` and proposes `.env` changes.
Current result: `LEO_N_CPU_MOE=20`, `LEO_UBATCH=2048` → ~33.7 tok/s, ~790 MiB VRAM free.

## Tests

| Command | What |
|---|---|
| `make test-memory` | leo-memory unit + DB tests (throwaway database) |
| `make test-normalizer` | LaTeX normalizer unit, property and fuzz tests |
| `make test-ui` | Playwright: render matrix (writes `docs/LATEX.md`), basic UI, live math, library question |
| `make test` | all of the above + end-to-end smoke tests + backup → fresh-volume restore check |

The end-to-end tests create a throwaway account (`smoke-…@example.com`) and delete it afterwards;
your own chats and memory are never touched. `make test` takes ~15 minutes (mostly waiting for the
memory summarizer's idle timer and live answers).

## Projects

Projects work like Claude Projects and are built on Open WebUI **folders**:

1. Sidebar → **Folders** → **+** → name it (e.g. "Linear Algebra course").
2. Folder **⋯ → Edit**: add **instructions** (a system prompt used for every chat in the project) and **files**
   (notes, papers, problem sets — Leo searches them when relevant).
3. Open the project and chat from its page; existing chats can be dragged into it.

Inside a project Leo uses the project's instructions and files, and **its own memory**: session summaries,
pinned facts ("remember that …") and the learner profile are stored per project. Chats outside projects use the
global memory. Nothing crosses between projects. Moving a chat into a project moves its future memory with it;
deleting a project deletes its memory (at the next hourly sync, or immediately via `POST /v1/admin/reconcile`).

## Memory

- Per-user switch: chat input → Integrations → Leo Memory valves → *memory_enabled*.
- Ask Leo "what do you remember about me?", "remember that …", "forget …".
- Raw access (token in `.env`): `compose.debug.yaml` exposes leo-memory on `127.0.0.1:18000`.
- Deleted chats are removed from memory hourly (or immediately: `POST /v1/admin/reconcile`).

## Hardware notes (this machine)

- The monitor is on the iGPU, so all 12 GB of VRAM are available.
- The host hard-reset twice under sustained GPU load (Sep 25–26, 2026). With the GPU capped at 280 W
  (`sudo nvidia-smi -pl 280`) and one ingest worker, long runs completed. The power limit **does not
  survive a reboot** — re-apply it, or make it persistent (for example a systemd unit running
  `nvidia-smi -pm 1 && nvidia-smi -pl 280` at boot). If resets continue, check the PSU.
- SSH on this host is reachable from the internet and sees constant root password guessing:
  set `PermitRootLogin no` and `PasswordAuthentication no` in `/etc/ssh/sshd_config`.

## Public domain (nginx edge)

`https://chatleo.tngnoai.online` is served by the `nginx` + `certbot` services (profile `edge`, switched on by
`COMPOSE_PROFILES=edge` in `.env`). Settings: `LEO_EDGE_DOMAIN`, `LEO_EDGE_BIND` (the public IP, ports 80+443),
`LEO_EDGE_EMAIL` (optional expiry notices), `LEO_EDGE_STAGING`.

- Certificate: issued and renewed automatically (checked every 12 h); nginx reloads within a minute of a change.
  Status: `make logs s=certbot`.
- Login brute-force protection: 10 sign-in attempts per minute per IP (HTTP 429 beyond that).
- Requests for any other host name or the bare IP are refused.
- Turn the public domain off: set `COMPOSE_PROFILES=` in `.env`, then `docker compose stop nginx certbot`.
- New domain: change `LEO_EDGE_DOMAIN` + DNS, `docker compose up -d --force-recreate certbot nginx`.

## Remote access (Caddy, port 8442)

`.env`: `LEO_PUBLIC_BIND=0.0.0.0`, `LEO_PUBLIC_PORT=8442`, `LEO_PUBLIC_HOST=220.149.84.72`,
`LEO_PUBLIC_URL=https://220.149.84.72:8442`. Apply with `docker compose up -d caddy open-webui`.
Turn it off with `LEO_PUBLIC_BIND=127.0.0.1`. This is reachable from the whole internet: keep the admin
password strong, don't create weak accounts, keep Open WebUI updated (`make update`), and watch
`make logs s=caddy` for abuse.

## Troubleshooting

See the table in README §14. Most useful first steps: `make status`, `make logs s=<service>`,
`make test-ui` for rendering issues, `make bootstrap` after changing `.env` settings that Open WebUI
stores in its database.
