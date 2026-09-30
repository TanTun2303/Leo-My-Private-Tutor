"""Shared Playwright/API fixtures for Leo UI tests. Runs inside the ui-test container."""

from __future__ import annotations

import importlib.util
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

UI = os.environ.get("LEO_UI_URL", "http://open-webui:8080").rstrip("/")
FIXTURES = Path(__file__).parent / "fixtures"
DOCS = Path(os.environ.get("LEO_DOCS_DIR", "/docs"))


def env(key: str, default: str = "") -> str:
    return re.sub(r"\s+#.*$", "", os.environ.get(key, default)).strip()


def load_normalizer():
    path = Path(os.environ.get("LEO_FUNCTIONS_DIR", "/functions")) / "leo_latex_normalizer.py"
    spec = importlib.util.spec_from_file_location("leo_latex_normalizer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class Api:
    def __init__(self) -> None:
        r = httpx.post(
            f"{UI}/api/v1/auths/signin",
            json={"email": env("WEBUI_ADMIN_EMAIL"), "password": env("WEBUI_ADMIN_PASSWORD")},
            timeout=30,
        )
        r.raise_for_status()
        self.token = r.json()["token"]
        self.http = httpx.Client(base_url=UI, headers={"Authorization": f"Bearer {self.token}"}, timeout=60)
        self.created: list[str] = []

    def create_chat(self, title: str, pairs: list[tuple[str, str]], model: str = "leo-study") -> tuple[str, list[str]]:
        """Chat with (user, assistant) pairs as saved history. Returns (chat_id, assistant_ids)."""
        now = int(time.time())
        messages, history, parent, asst_ids = [], {}, None, []
        for user_text, asst_text in pairs:
            uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
            um = {
                "id": uid,
                "parentId": parent,
                "childrenIds": [aid],
                "role": "user",
                "content": user_text,
                "timestamp": now,
            }
            am = {
                "id": aid,
                "parentId": uid,
                "childrenIds": [],
                "role": "assistant",
                "content": asst_text,
                "model": model,
                "modelName": "Leo",
                "timestamp": now,
                "done": True,
            }
            if parent:
                history[parent]["childrenIds"] = [uid]
            history[uid], history[aid] = um, am
            messages += [um, am]
            parent = aid
            asst_ids.append(aid)
        r = self.http.post(
            "/api/v1/chats/new",
            json={
                "chat": {
                    "title": title,
                    "models": [model],
                    "messages": messages,
                    "history": {"messages": history, "currentId": parent},
                }
            },
        )
        r.raise_for_status()
        cid = r.json()["id"]
        self.created.append(cid)
        return cid, asst_ids

    def delete_chat(self, cid: str) -> None:
        self.http.delete(f"/api/v1/chats/{cid}")

    def cleanup(self) -> None:
        for cid in self.created:
            self.delete_chat(cid)


@pytest.fixture(scope="session")
def api() -> Any:
    a = Api()
    yield a
    a.cleanup()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1280, "height": 900}, "locale": "en-US"}


@pytest.fixture
def ui(page, api):
    """A logged-in page."""
    page.goto(f"{UI}/auth")
    page.evaluate("t => localStorage.setItem('token', t)", api.token)
    page.goto(f"{UI}/")
    page.wait_for_selector("#message-input-container", timeout=60_000)
    return page
