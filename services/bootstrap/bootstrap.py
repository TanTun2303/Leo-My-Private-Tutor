"""Idempotent provisioning of Open WebUI for Leo (§9). Run: make bootstrap"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import psycopg

PROVISION = Path(os.environ.get("PROVISION_DIR", "/provision"))
DOCS = Path(os.environ.get("DOCS_DIR", "/docs"))
WEBUI = os.environ.get("WEBUI_INTERNAL_URL", "http://open-webui:8080").rstrip("/")
MEMORY = os.environ.get("MEMORY_INTERNAL_URL", "http://leo-memory:8000").rstrip("/")
MEMORY_URL_FOR_WEBUI = "http://leo-memory:8000"

INLINE_EXAMPLES = {
    "dollar": "$x^2$ (single dollar signs, no space right after the opening $ or before the closing $)",
    "paren": "\\(x^2\\)",
    "double": "$$x^2$$ written inside the sentence",
}

report: list[tuple[str, str]] = []

# Leo, its tools and the library are readable by every account (sign-up is disabled; accounts
# are created by the admin). Chats and memory stay per user.
PUBLIC_READ = [{"principal_type": "user", "principal_id": "*", "permission": "read"}]


def env(key: str, default: str | None = None) -> str:
    v = os.environ.get(key, default)
    if v is None:
        sys.exit(f"missing env {key}")
    return re.sub(r"\s+#.*$", "", v).strip()


def step(name: str, result: str) -> None:
    report.append((name, result))
    print(f"  ✔ {name}: {result}", flush=True)


class WebUI:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=WEBUI, timeout=120)

    def signin(self) -> None:
        r = self.http.post(
            "/api/v1/auths/signin",
            json={"email": env("WEBUI_ADMIN_EMAIL"), "password": env("WEBUI_ADMIN_PASSWORD")},
        )
        r.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {r.json()['token']}"

    def get(self, path: str, missing_ok: bool = False, **kw: Any) -> Any:
        """GET JSON. Lookup routes answer a missing id with 401/404 ('not found'): None if missing_ok."""
        r = self.http.get(path, **kw)
        if missing_ok and r.status_code in (401, 404):
            return None
        r.raise_for_status()
        return r.json() if r.content else None

    def post(self, path: str, data: Any = None, **kw: Any) -> Any:
        r = self.http.post(path, json=data, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} → {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None


def wait_healthy() -> None:
    deadline = time.time() + 600
    for url in (f"{WEBUI}/health", f"{MEMORY}/healthz"):
        while True:
            try:
                if httpx.get(url, timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                sys.exit(f"timed out waiting for {url}")
            time.sleep(3)
    step("services", "open-webui and leo-memory healthy")


def math_style() -> str:
    """docs/LATEX.md (written by `make test-ui`) wins over LEO_MATH_INLINE."""
    latex = DOCS / "LATEX.md"
    if latex.exists():
        m = re.search(r"Chosen inline style:\s*`(\w+)`", latex.read_text())
        if m and m.group(1) in INLINE_EXAMPLES:
            return m.group(1)
    style = env("LEO_MATH_INLINE", "dollar")
    return style if style in INLINE_EXAMPLES else "dollar"


# ── functions & tools ────────────────────────────────────────────────────────


def upsert_function(
    ui: WebUI, fid: str, name: str, description: str, valves: dict, active: bool, is_global: bool
) -> None:
    content = (PROVISION / "functions" / f"{fid}.py").read_text()
    form = {"id": fid, "name": name, "content": content, "meta": {"description": description}}
    existing = ui.get(f"/api/v1/functions/id/{fid}", missing_ok=True)
    if existing is None:
        ui.post("/api/v1/functions/create", form)
        existing = ui.get(f"/api/v1/functions/id/{fid}")
        action = "created"
    else:
        if existing.get("content") != content or existing.get("name") != name:
            ui.post(f"/api/v1/functions/id/{fid}/update", form)
            action = "updated"
        else:
            action = "unchanged"
        existing = ui.get(f"/api/v1/functions/id/{fid}")
    if valves:
        ui.post(f"/api/v1/functions/id/{fid}/valves/update", valves)
    if bool(existing.get("is_active")) != active:
        ui.post(f"/api/v1/functions/id/{fid}/toggle")
    if bool(existing.get("is_global")) != is_global:
        ui.post(f"/api/v1/functions/id/{fid}/toggle/global")
    final = ui.get(f"/api/v1/functions/id/{fid}")
    assert final and final["is_active"] == active and final["is_global"] == is_global, final
    step(f"function {fid}", f"{action}, active={active}, global={is_global}")


def upsert_tool(ui: WebUI, tid: str, name: str, description: str, valves: dict) -> None:
    content = (PROVISION / "tools" / f"{tid}.py").read_text()
    form = {
        "id": tid,
        "name": name,
        "content": content,
        "meta": {"description": description},
        "access_grants": PUBLIC_READ,
    }
    existing = ui.get(f"/api/v1/tools/id/{tid}", missing_ok=True)
    if existing is None:
        ui.post("/api/v1/tools/create", form)
        action = "created"
    elif existing.get("content") != content:
        ui.post(f"/api/v1/tools/id/{tid}/update", form)
        action = "updated"
    else:
        action = "unchanged"
    ui.post(f"/api/v1/tools/id/{tid}/valves/update", valves)
    ui.post(f"/api/v1/tools/id/{tid}/access/update", {"access_grants": PUBLIC_READ})
    step(f"tool {tid}", action)


# ── knowledge & models ───────────────────────────────────────────────────────


def ensure_knowledge(ui: WebUI, name: str) -> dict:
    page = 1
    while True:
        res = ui.get("/api/v1/knowledge/", params={"page": page}) or {"items": []}
        for kb in res.get("items", []):
            if kb.get("name") == name:
                ui.post(f"/api/v1/knowledge/{kb['id']}/access/update", {"access_grants": PUBLIC_READ})
                step("knowledge base", f"'{name}' exists ({kb['id']}), readable by all users")
                return kb
        if len(res.get("items", [])) == 0 or page * 30 >= res.get("total", 0):
            break
        page += 1
    kb = ui.post(
        "/api/v1/knowledge/create", {"name": name, "description": "Leo's library of books (Marker → Markdown)"}
    )
    step("knowledge base", f"'{name}' created ({kb['id']})")
    return kb


def upsert_model(ui: WebUI, form: dict) -> str:
    existing = ui.get("/api/v1/models/model", missing_ok=True, params={"id": form["id"]})
    if existing is None:
        ui.post("/api/v1/models/create", form)
        return "created"
    ui.post("/api/v1/models/model/update", form)
    return "updated"


def provision_leo(ui: WebUI, kb: dict, style: str) -> None:
    model = json.loads((PROVISION / "models" / "leo.json").read_text())
    prompt = (PROVISION / "prompts" / "leo_system_prompt.md").read_text().strip()
    prompt = prompt.replace("[[INLINE_EXAMPLE]]", INLINE_EXAMPLES[style])
    model["params"]["system"] = prompt
    model["meta"]["knowledge"] = [
        {"id": kb["id"], "name": kb["name"], "type": "collection", "description": kb.get("description", "")}
    ]
    model["meta"]["filterIds"] = ["leo_memory_filter", "leo_think_toggle"]
    model["meta"]["toolIds"] = ["leo_memory_tools"]
    model["access_grants"] = PUBLIC_READ
    step("workspace model leo-study (Leo)", f"{upsert_model(ui, model)}; inline math style '{style}'")

    hidden = {
        "id": "leo",
        "base_model_id": None,
        "name": "leo (raw)",
        "is_active": True,
        "meta": {"hidden": True, "description": "Raw llama.cpp model; use Leo instead."},
        "params": {},
        "access_grants": PUBLIC_READ,  # Leo is built on it: every account needs read access
    }
    step("base model leo", f"{upsert_model(ui, hidden)}, hidden from picker")


def configure(ui: WebUI) -> None:
    cfg = ui.get("/api/v1/configs/models") or {}
    order = [m for m in (cfg.get("MODEL_ORDER_LIST") or []) if m != "leo-study"]
    cfg.update(DEFAULT_MODELS="leo-study", DEFAULT_PINNED_MODELS="leo-study", MODEL_ORDER_LIST=["leo-study", *order])
    ui.post("/api/v1/configs/models", cfg)
    step("default model", "leo-study (pinned, first)")

    admin = ui.get("/api/v1/auths/admin/config")
    if admin.get("ENABLE_MEMORIES"):
        admin["ENABLE_MEMORIES"] = False
        ui.post("/api/v1/auths/admin/config", admin)
    step("built-in Memory", "disabled (leo-memory is the source of truth)")

    try:
        ui.post("/api/v1/evaluations/config", {"ENABLE_EVALUATION_ARENA_MODELS": False})
        step("arena models", "disabled")
    except RuntimeError as e:
        step("arena models", f"skipped ({e})")


def grant_reconcile() -> None:
    url = f"postgresql://openwebui:{env('OPENWEBUI_DB_PASSWORD')}@postgres:5432/openwebui"
    try:
        with psycopg.connect(url, connect_timeout=10, autocommit=True) as c:
            row = c.execute("SELECT to_regclass('public.chat')").fetchone()
            if not row or row[0] is None:
                step("reconcile grant", "skipped: Open WebUI chat table not found")
                return
            c.execute("GRANT USAGE ON SCHEMA public TO leo_reconcile")
            c.execute("GRANT SELECT (id) ON public.chat TO leo_reconcile")
            folder = c.execute("SELECT to_regclass('public.folder')").fetchone()
            if folder and folder[0] is not None:
                c.execute("GRANT SELECT (id) ON public.folder TO leo_reconcile")  # projects
        step("reconcile grant", "leo_reconcile may SELECT (id) ON public.chat and public.folder")
    except psycopg.Error as e:
        step("reconcile grant", f"FAILED ({type(e).__name__}) — chat deletion sync disabled")


def main() -> None:
    print("==> Provisioning Open WebUI", flush=True)
    wait_healthy()
    ui = WebUI()
    ui.signin()
    step("sign in", env("WEBUI_ADMIN_EMAIL"))

    token = env("LEO_MEMORY_TOKEN")
    style = math_style()
    upsert_function(
        ui,
        "leo_memory_filter",
        "Leo Memory",
        "Cross-chat memory: recall + turn events",
        {
            "memory_url": MEMORY_URL_FOR_WEBUI,
            "memory_token": token,
            "enabled": True,
            "inlet_timeout_s": 1.5,
            "outlet_timeout_s": 1.0,
            "priority": 0,
        },
        active=True,
        is_global=True,
    )
    upsert_function(
        ui,
        "leo_think_toggle",
        "Think",
        "Deep reasoning for this message",
        {"max_tokens": 16384, "priority": 10},
        active=True,
        is_global=False,
    )
    upsert_function(
        ui,
        "leo_latex_normalizer",
        "Leo LaTeX",
        "Canonical math delimiters for KaTeX",
        {
            "enabled": True,
            "inline_style": style,
            "stream_normalize": env("LEO_MATH_STREAM_NORMALIZE", "true").lower() == "true",
            "priority": 100,
        },
        active=True,
        is_global=True,
    )
    upsert_tool(
        ui,
        "leo_memory_tools",
        "Leo Memory Tools",
        "remember / forget / what_do_you_remember",
        {"memory_url": MEMORY_URL_FOR_WEBUI, "memory_token": token, "timeout_s": 10.0},
    )
    kb = ensure_knowledge(ui, env("LEO_KNOWLEDGE_NAME", "Leo Library"))
    provision_leo(ui, kb, style)
    configure(ui)
    grant_reconcile()

    print("\n==> Summary")
    width = max(len(k) for k, _ in report)
    for k, v in report:
        print(f"  {k.ljust(width)}  {v}")


if __name__ == "__main__":
    main()
