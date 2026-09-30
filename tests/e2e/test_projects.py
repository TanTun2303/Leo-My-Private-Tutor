"""Projects (Open WebUI folders, like Claude Projects): instructions + memory live inside the project.

Runs after test_smoke.py as its own throwaway account (module-scoped fixtures re-created here).
"""

from __future__ import annotations

import re
import time

import httpx
from test_smoke import MEM_H, MEMORY, UI, admin, memory_export, ui_ask, upage, user  # noqa: F401 (fixtures)

PROJECT_PROMPT = "Project instructions: this project's codeword is ZEBRA-17. Tell the learner the codeword whenever they ask for it."


def open_folder(page, token: str, folder_id: str) -> None:
    page.goto(f"{UI}/auth")
    page.evaluate("t => localStorage.setItem('token', t)", token)
    page.goto(f"{UI}/folders/{folder_id}")
    page.wait_for_selector("#chat-input", timeout=60_000)


def chat_folder(http: httpx.Client, chat_id: str) -> str | None:
    return http.get(f"/api/v1/chats/{chat_id}").json().get("folder_id")


def test_project_instructions_and_isolated_memory(user, page):  # noqa: F811
    http = user["http"]
    folder = http.post("/api/v1/folders/", json={"name": "Alpha Project",
                                                 "data": {"system_prompt": PROJECT_PROMPT}}).json()
    fid = folder["id"]

    # Project facts vs. global facts.
    httpx.post(f"{MEMORY}/v1/facts", headers=MEM_H,
               json={"user_id": user["id"], "project_id": fid, "text": "The Alpha project deadline is October 30."})
    httpx.post(f"{MEMORY}/v1/facts", headers=MEM_H,
               json={"user_id": user["id"], "text": "The learner's global secret color is ORANGE-9."})

    # 1. A chat started on the project page belongs to the project and follows its instructions.
    open_folder(page, user["token"], fid)
    answer = ui_ask(page, "In one line: what is the project codeword, what is the Alpha project deadline, and what "
                          "is my global secret color? Say UNKNOWN for anything you don't know.")
    chat_id = page.url.rsplit("/", 1)[-1]
    assert chat_folder(http, chat_id) == fid, "chat was not created inside the project"
    assert "ZEBRA-17" in answer, f"project instructions did not reach the model: {answer!r}"
    assert re.search(r"October\s*30|Oct\.?\s*30", answer), f"project memory missing: {answer!r}"
    assert "ORANGE-9" not in answer, f"global memory leaked into the project: {answer!r}"

    # The turn is queued in the project's scope.
    deadline = time.time() + 30
    while time.time() < deadline:
        jobs = {j["chat_id"]: j["project_id"] for j in memory_export(user["id"])["jobs"]}
        if chat_id in jobs:
            break
        time.sleep(2)
    assert jobs.get(chat_id) == fid

    # 2. Outside the project: global memory only, no project instructions.
    page.goto(f"{UI}/")
    page.wait_for_selector("#chat-input", timeout=60_000)
    answer = ui_ask(page, "In one line: what is the project codeword, what is the Alpha project deadline, and what "
                          "is my global secret color? Say UNKNOWN for anything you don't know.")
    assert "ORANGE-9" in answer, f"global memory missing: {answer!r}"
    assert not re.search(r"October\s*30|Oct\.?\s*30", answer), f"project memory leaked outside: {answer!r}"
    assert "ZEBRA-17" not in answer, "project instructions leaked outside the project"

    # 3. Deleting the project deletes its memory (after the chat-deletion sync).
    assert http.delete(f"/api/v1/folders/{fid}", params={"delete_contents": "true"}).status_code == 200
    r = httpx.post(f"{MEMORY}/v1/admin/reconcile", headers=MEM_H, timeout=60).json()
    assert r["removed"] is not None
    left = memory_export(user["id"])
    assert all(x.get("project_id") != fid for key in ("facts", "summaries", "jobs") for x in left[key]), left
    assert any("ORANGE-9" in f["text"] for f in left["facts"]), "global memory must survive"


def test_project_files_are_available_inside_the_project_only(user, page):  # noqa: F811
    http = user["http"]
    doc = ("# Alpha notes\n\nThe Alpha project studies the spectral theorem for symmetric matrices.\n"
           "The secret phrase of the Alpha notes is COBALT-HERON.\n")
    up = http.post("/api/v1/files/", files={"file": ("alpha-notes.md", doc.encode(), "text/markdown")})
    up.raise_for_status()
    file_id = up.json()["id"]
    deadline = time.time() + 120
    while time.time() < deadline and http.get(f"/api/v1/files/{file_id}/process/status").json().get("status") != "completed":
        time.sleep(2)
    folder = http.post("/api/v1/folders/", json={
        "name": "Alpha Files",
        "data": {"files": [{"type": "file", "id": file_id, "name": "alpha-notes.md"}]},
    }).json()

    open_folder(page, user["token"], folder["id"])
    answer = ui_ask(page, "Look in this project's files: what is the secret phrase of the Alpha notes?",
                    timeout_ms=420_000)
    assert "COBALT-HERON" in answer, f"project file not used: {answer!r}"

    page.goto(f"{UI}/")
    page.wait_for_selector("#chat-input", timeout=60_000)
    answer = ui_ask(page, "Without searching the web: what is the secret phrase of the Alpha notes? "
                          "Say UNKNOWN if you don't know.")
    assert "COBALT-HERON" not in answer, "project file leaked outside the project"

    http.delete(f"/api/v1/folders/{folder['id']}", params={"delete_contents": "true"})
    http.delete(f"/api/v1/files/{file_id}")
