"""Minimal Open WebUI client for smoke tests (stdlib only).

Simulates a browser chat turn: creates a chat, sends a completion with a
session_id (so built-in tools like web search are attached, as in the UI),
then polls the saved chat until the assistant message is complete.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request
import uuid
from typing import Any

BASE = os.environ.get("LEO_URL", "https://leo.localhost")
_CTX = ssl._create_unverified_context()  # Caddy local CA; tests only


class Client:
    def __init__(self, email: str, password: str) -> None:
        self.token = ""
        self.token = self.req("/api/v1/auths/signin", {"email": email, "password": password})["token"]

    def req(self, path: str, data: Any = None, method: str | None = None, timeout: float = 600) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = json.dumps(data).encode() if data is not None else None
        r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
        with urllib.request.urlopen(r, context=_CTX, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def chat_turn(
        self, model: str, prompt: str, features: dict | None = None, timeout: float = 600, **extra: Any
    ) -> dict:
        """Run one UI-like turn. Returns the final assistant message dict from the saved chat."""
        now = int(time.time())
        user_id, asst_id = str(uuid.uuid4()), str(uuid.uuid4())
        user_msg = {
            "id": user_id,
            "parentId": None,
            "childrenIds": [asst_id],
            "role": "user",
            "content": prompt,
            "timestamp": now,
            "models": [model],
        }
        asst_msg = {
            "id": asst_id,
            "parentId": user_id,
            "childrenIds": [],
            "role": "assistant",
            "content": "",
            "model": model,
            "timestamp": now,
            "done": False,
        }
        chat = self.req(
            "/api/v1/chats/new",
            {
                "chat": {
                    "title": f"smoke {now}",
                    "models": [model],
                    "messages": [user_msg, asst_msg],
                    "history": {"messages": {user_id: user_msg, asst_id: asst_msg}, "currentId": asst_id},
                }
            },
        )
        chat_id = chat["id"]
        self.req(
            "/api/chat/completions",
            {
                "model": model,
                "stream": True,
                "chat_id": chat_id,
                "id": asst_id,
                "session_id": f"smoke-{uuid.uuid4().hex[:12]}",
                "messages": [{"role": "user", "content": prompt}],
                "features": features or {},
                **extra,  # e.g. filter_ids=["leo_think_toggle"], tool_ids=[...]
            },
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.req(f"/api/v1/chats/{chat_id}")["chat"]["history"]["messages"].get(asst_id, {})
            if msg.get("done"):
                msg["chat_id"] = chat_id
                return msg
            time.sleep(2)
        raise TimeoutError(f"assistant message not done after {timeout}s (chat {chat_id})")
