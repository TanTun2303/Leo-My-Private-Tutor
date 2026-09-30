"""End-to-end smoke tests (§12), run by `make test` inside the ui-test container.

Everything runs as a throwaway user created for the run, so the owner's chats and
memory are never touched; the user and its memory are deleted at the end.
Tests run in file order and build on each other (memory round trip → forget/delete).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid

import httpx
import pytest

UI = os.environ.get("LEO_UI_URL", "http://open-webui:8080").rstrip("/")
MEMORY = os.environ.get("LEO_MEMORY_URL", "http://leo-memory:8000").rstrip("/")
IDLE = int(re.sub(r"\s+#.*", "", os.environ.get("MEMORY_IDLE_SECONDS", "120")))
MAX_WAIT = int(re.sub(r"\s+#.*", "", os.environ.get("MEMORY_MAX_WAIT_SECONDS", "900")))
TOKEN = re.sub(r"\s+#.*", "", os.environ["LEO_MEMORY_TOKEN"]).strip()
MEM_H = {"Authorization": f"Bearer {TOKEN}"}
STATE: dict = {}


def env(key: str) -> str:
    return re.sub(r"\s+#.*$", "", os.environ.get(key, "")).strip()


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def admin():
    r = httpx.post(f"{UI}/api/v1/auths/signin",
                   json={"email": env("WEBUI_ADMIN_EMAIL"), "password": env("WEBUI_ADMIN_PASSWORD")})
    r.raise_for_status()
    return httpx.Client(base_url=UI, headers={"Authorization": f"Bearer {r.json()['token']}"}, timeout=120)


@pytest.fixture(scope="module")
def user(admin):
    email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
    password = uuid.uuid4().hex
    r = admin.post("/api/v1/auths/add", json={"name": "Smoke Test", "email": email, "password": password,
                                              "role": "user"})
    r.raise_for_status()
    uid = r.json()["id"]
    tok = httpx.post(f"{UI}/api/v1/auths/signin", json={"email": email, "password": password}).json()["token"]
    client = httpx.Client(base_url=UI, headers={"Authorization": f"Bearer {tok}"}, timeout=300)
    yield {"id": uid, "token": tok, "http": client}
    httpx.delete(f"{MEMORY}/v1/users/{uid}", headers=MEM_H)
    admin.delete(f"/api/v1/users/{uid}")


@pytest.fixture
def upage(page, user):
    page.goto(f"{UI}/auth")
    page.evaluate("t => localStorage.setItem('token', t)", user["token"])
    page.goto(f"{UI}/")
    page.wait_for_selector("#message-input-container", timeout=60_000)
    return page


# ── helpers ─────────────────────────────────────────────────────────────────


def ui_ask(page, prompt: str, timeout_ms: int = 300_000) -> str:
    box = page.locator("#chat-input")
    box.click()
    box.fill(prompt)
    page.keyboard.press("Enter")
    page.wait_for_timeout(1500)
    last = page.locator("[id^=message-]").filter(has=page.locator("#response-content-container")).last
    last.locator("button[aria-label='Copy']").wait_for(timeout=timeout_ms)
    page.wait_for_timeout(1500)
    return last.locator("#response-content-container").inner_text()


def memory_export(uid: str) -> dict:
    return httpx.get(f"{MEMORY}/v1/users/{uid}/export", headers=MEM_H, timeout=30).json()


def stream(http: httpx.Client, body: dict) -> tuple[float | None, str, str]:
    """POST a streaming completion; returns (seconds to first content/reasoning token, reasoning, content)."""
    t0, first, reasoning, content = time.time(), None, [], []
    with http.stream("POST", "/api/chat/completions", json={**body, "stream": True}, timeout=600) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:") or line.strip() == "data: [DONE]":
                continue
            try:
                delta = (json.loads(line[5:]).get("choices") or [{}])[0].get("delta") or {}
            except json.JSONDecodeError:
                continue
            piece_r = delta.get("reasoning_content") or delta.get("reasoning") or ""
            piece_c = delta.get("content") or ""
            if (piece_r or piece_c) and first is None:
                first = time.time() - t0
            reasoning.append(piece_r)
            content.append(piece_c)
    return first, "".join(reasoning), "".join(content)


# ── 1. services ─────────────────────────────────────────────────────────────


def test_1_services_healthy(admin):
    assert httpx.get(f"{UI}/health").status_code == 200
    assert httpx.get(f"{MEMORY}/healthz").status_code == 200
    ids = [m["id"] for m in admin.get("/api/models").json()["data"]]
    assert "leo-study" in ids


# ── 2. modes ────────────────────────────────────────────────────────────────


def test_2_modes(user):
    msgs = [{"role": "user", "content": "In one sentence: what is a spanning tree?"}]
    first, reasoning, content = stream(user["http"], {"model": "leo-study", "messages": msgs})
    assert content.strip(), "no answer"
    assert not reasoning.strip(), "Think off produced a reasoning block"
    assert first is not None and first < 4.0, f"first token after {first:.1f}s (target ~3 s)"
    STATE["ttft_off"] = first

    msgs = [{"role": "user", "content": "Is 221 prime? Answer in one line."}]
    _, reasoning, content = stream(user["http"], {"model": "leo-study", "messages": msgs,
                                                  "filter_ids": ["leo_think_toggle"]})
    assert reasoning.strip(), "Think on produced no reasoning"
    assert content.strip()


# ── 4. system prompt + <leo_memory> reach llama-server ──────────────────────


def test_4_system_prompt_and_memory_reach_model(user, upage):
    code = f"TEAL-{uuid.uuid4().hex[:4].upper()}"
    r = httpx.post(f"{MEMORY}/v1/facts", headers=MEM_H,
                   json={"user_id": user["id"], "text": f"The learner's secret study code is {code}."})
    assert r.status_code == 201
    answer = ui_ask(upage, "What is my secret study code, and what is your name? Answer in one short line.")
    assert code in answer, f"memory block did not reach the model: {answer!r}"
    assert "Leo" in answer, f"system prompt did not reach the model: {answer!r}"
    httpx.post(f"{MEMORY}/v1/forget", headers=MEM_H, json={"user_id": user["id"], "query": code})


# ── 5. memory round trip ────────────────────────────────────────────────────


def test_5_memory_round_trip(user, upage):
    ui_ask(upage, "I'm studying Dijkstra. I keep confusing relaxation with visiting order.")
    STATE["chat_a"] = upage.url.rsplit("/", 1)[-1]
    deadline = time.time() + IDLE + 30 + 180
    summary = None
    while time.time() < deadline:
        sums = [s for s in memory_export(user["id"])["summaries"] if s["chat_id"] == STATE["chat_a"]]
        if sums:
            summary = sums[0]
            break
        time.sleep(10)
    assert summary, f"no summary within idle ({IDLE}s) + margin"
    assert len(summary["summary"].split()) <= 60
    blob = json.dumps(summary).lower()
    assert "dijkstra" in blob and ("relax" in blob or "visit" in blob), summary
    prof = memory_export(user["id"])["profile"]
    assert any("relax" in w["concept"] or "dijkstra" in w["concept"] or "visit" in w["concept"]
               for w in prof["weak_spots"]), prof

    upage.goto(f"{UI}/")
    upage.wait_for_selector("#message-input-container", timeout=60_000)
    answer = ui_ask(upage, "What was I struggling with last time?")
    STATE["chat_b"] = upage.url.rsplit("/", 1)[-1]
    assert re.search(r"relax|dijkstra|visit", answer, re.I), answer


# ── 6. temporary chat ───────────────────────────────────────────────────────


def test_6_temporary_chat_not_remembered(user, page):
    page.goto(f"{UI}/auth")
    page.evaluate("t => localStorage.setItem('token', t)", user["token"])
    page.goto(f"{UI}/?temporary-chat=true")
    page.wait_for_selector("#message-input-container", timeout=60_000)
    before = {j["chat_id"] for j in memory_export(user["id"])["jobs"]}
    ui_ask(page, "Remember nothing of this: my favourite graph is the Petersen graph.")
    assert "/c/" not in page.url, "temporary chat was saved"
    time.sleep(5)
    after = memory_export(user["id"])
    new_jobs = {j["chat_id"] for j in after["jobs"]} - before
    assert not new_jobs, f"temporary chat created memory jobs: {new_jobs}"
    assert not any("petersen" in s["summary"].lower() for s in after["summaries"])


# ── 7. forget / chat deletion ───────────────────────────────────────────────


def test_7_forget_tool_and_chat_deletion(user, upage, admin):
    # Forget through Leo's tool.
    ui_ask(upage, "Please use your memory tool to forget everything you remember about Dijkstra.")
    time.sleep(3)
    ids = {s["chat_id"] for s in memory_export(user["id"])["summaries"]}
    assert STATE["chat_a"] not in ids, "forget left the Dijkstra session summary"

    # Delete a chat in Open WebUI → reconcile removes its memory.
    httpx.post(f"{MEMORY}/v1/events/turn", headers=MEM_H, json={
        "chat_id": STATE["chat_b"], "user_id": user["id"], "title": "tmp",
        "messages": [{"id": "x1", "role": "user", "content": "quick question about heaps"}]})
    assert user["http"].delete(f"/api/v1/chats/{STATE['chat_b']}").status_code == 200
    r = httpx.post(f"{MEMORY}/v1/admin/reconcile", headers=MEM_H, timeout=60).json()
    assert r["removed"] is not None, "reconcile has no access to Open WebUI's chat table"
    exp = memory_export(user["id"])
    assert STATE["chat_b"] not in {s["chat_id"] for s in exp["summaries"]} | {j["chat_id"] for j in exp["jobs"]}


# ── 9. stress: debounce ─────────────────────────────────────────────────────


def test_9_twenty_rapid_turns_one_job(user):
    chat_id = f"stress-{uuid.uuid4().hex[:8]}"
    msgs = []
    for i in range(20):
        msgs += [{"id": f"u{i}", "role": "user", "content": f"Rapid question {i} about matrix rank"},
                 {"id": f"a{i}", "role": "assistant", "content": f"answer {i}"}]
        r = httpx.post(f"{MEMORY}/v1/events/turn", headers=MEM_H,
                       json={"chat_id": chat_id, "user_id": user["id"], "messages": msgs})
        assert r.status_code == 202
    jobs = [j for j in memory_export(user["id"])["jobs"] if j["chat_id"] == chat_id]
    assert len(jobs) == 1, jobs
    from datetime import datetime

    first = datetime.fromisoformat(jobs[0]["first_pending_at"])
    run_after = datetime.fromisoformat(jobs[0]["run_after"])
    assert (run_after - first).total_seconds() <= MAX_WAIT + 1
    httpx.delete(f"{MEMORY}/v1/chats/{chat_id}", headers=MEM_H)
