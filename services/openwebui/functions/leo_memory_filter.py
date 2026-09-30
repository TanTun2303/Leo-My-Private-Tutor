"""
title: Leo Memory
author: leo
version: 1.0.0
description: Injects a compact <leo_memory> block (recall) and posts finished turns to leo-memory. Memory is scoped per project (folder).
"""

# Uses only what the Open WebUI image already ships (httpx, pydantic). Never raises.

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _chat_id(body: dict, metadata: dict | None) -> str | None:
    cid = (metadata or {}).get("chat_id") or body.get("chat_id")
    return str(cid) if cid else None


async def _chat_title(chat_id: str) -> str | None:
    """Title from Open WebUI's own chat table (outlet bodies carry no title). Best-effort."""
    try:
        from open_webui.models.chats import Chats  # available inside Open WebUI only

        chat = await Chats.get_chat_by_id(chat_id)
        title = getattr(chat, "title", None)
        return title if title and title != "New Chat" else None
    except Exception:
        return None


async def _project_id(chat_id: str | None, user_id: str, metadata: dict | None) -> str:
    """The chat's project = its Open WebUI folder id ("" outside projects). Best-effort."""
    pid = (metadata or {}).get("folder_id")
    if pid:
        return str(pid)
    if not chat_id:
        return ""
    try:
        from open_webui.models.chats import Chats  # available inside Open WebUI only

        return (await Chats.get_chat_folder_id(chat_id, user_id)) or ""
    except Exception:
        return ""


def _temporary(chat_id: str | None) -> bool:
    # Open WebUI v0.11.4 utils/chat_id.py: temporary ("temporary:", legacy "local:") and channel chats aren't saved.
    return not chat_id or chat_id.startswith(("temporary:", "local:", "channel:"))


class Filter:
    class Valves(BaseModel):
        memory_url: str = Field(default="http://leo-memory:8000", description="leo-memory base URL")
        memory_token: str = Field(default="", description="Bearer token for leo-memory")
        enabled: bool = True
        inlet_timeout_s: float = 1.5
        outlet_timeout_s: float = 1.0
        priority: int = 0

    class UserValves(BaseModel):
        memory_enabled: bool = Field(default=True, description="Let Leo remember your study sessions")

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._tasks: set[asyncio.Task] = set()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.valves.memory_token}"}

    @staticmethod
    def _user_enabled(user: dict | None) -> bool:
        valves = (user or {}).get("valves")
        return bool(getattr(valves, "memory_enabled", True)) if valves is not None else True

    async def inlet(self, body: dict, __user__: dict | None = None, __metadata__: dict | None = None) -> dict:
        try:
            if not self.valves.enabled or not __user__ or not __user__.get("id"):
                return body
            chat_id = _chat_id(body, __metadata__)
            if _temporary(chat_id) or not self._user_enabled(__user__):
                return body
            messages = body.get("messages") or []
            query = next((_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
            project_id = await _project_id(chat_id, __user__["id"], __metadata__)
            async with httpx.AsyncClient(timeout=self.valves.inlet_timeout_s) as client:
                r = await client.get(
                    f"{self.valves.memory_url}/v1/recall",
                    params={
                        "user_id": __user__["id"],
                        "q": query[:2000],
                        "exclude_chat_id": chat_id,
                        "project_id": project_id,
                    },
                    headers=self._headers(),
                )
                r.raise_for_status()
                block = r.json().get("block") or ""
            log.info("leo memory: recall scope=%s injected=%d chars", project_id or "global", len(block))
            if not block:
                return body
            # Own system message at the front; Open WebUI later prepends the model's system
            # prompt to the first system message, so the order becomes: Leo prompt, then memory.
            if messages and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str):
                messages[0]["content"] = f"{messages[0]['content']}\n\n{block}"
            else:
                messages.insert(0, {"role": "system", "content": block})
            body["messages"] = messages
        except Exception as e:  # recall is best-effort
            log.warning("leo memory recall skipped: %s", type(e).__name__)
        return body

    async def _post_turn(self, payload: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=self.valves.outlet_timeout_s) as client:
                await client.post(f"{self.valves.memory_url}/v1/events/turn", json=payload, headers=self._headers())
        except Exception as e:
            log.warning("leo memory turn event failed: %s", type(e).__name__)

    async def outlet(self, body: dict, __user__: dict | None = None, __metadata__: dict | None = None) -> dict:
        try:
            if not self.valves.enabled or not __user__ or not __user__.get("id"):
                return body
            chat_id = _chat_id(body, __metadata__)
            if _temporary(chat_id):
                return body
            payload = {
                "chat_id": chat_id,
                "user_id": __user__["id"],
                "title": await _chat_title(chat_id),
                "memory_enabled": self._user_enabled(__user__),
                "project_id": await _project_id(chat_id, __user__["id"], __metadata__),
                "messages": [
                    {"id": m.get("id"), "role": m.get("role"), "content": m.get("content") or ""}
                    for m in (body.get("messages") or [])
                ],
            }
            task = asyncio.create_task(self._post_turn(payload))  # fire-and-forget
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as e:
            log.warning("leo memory outlet skipped: %s", type(e).__name__)
        return body
