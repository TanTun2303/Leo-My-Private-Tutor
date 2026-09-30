"""
title: Leo Memory Tools
author: leo
version: 1.0.0
description: Let Leo pin, forget and list what it remembers about you.
"""

from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, Field


async def _project_id(user: dict | None, metadata: dict | None) -> str:
    """The current chat's project (Open WebUI folder id); "" outside projects."""
    md = metadata or {}
    if md.get("folder_id"):
        return str(md["folder_id"])
    chat_id = md.get("chat_id")
    if not chat_id or not user:
        return ""
    try:
        from open_webui.models.chats import Chats

        return (await Chats.get_chat_folder_id(chat_id, user["id"])) or ""
    except Exception:
        return ""


class Tools:
    class Valves(BaseModel):
        memory_url: str = Field(default="http://leo-memory:8000")
        memory_token: str = Field(default="")
        timeout_s: float = 10.0

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.valves.memory_url,
            timeout=self.valves.timeout_s,
            headers={"Authorization": f"Bearer {self.valves.memory_token}"},
        )

    async def remember(self, fact: str, __user__: dict | None = None, __metadata__: dict | None = None) -> str:
        """
        Pin a fact about the learner so it is remembered in future chats.
        Use ONLY when the learner explicitly asks you to remember something.
        :param fact: The fact to remember, one short sentence (max 300 characters).
        """
        if not __user__ or not __user__.get("id"):
            return "Memory is unavailable (no user)."
        fact = " ".join(fact.split())[:300]
        try:
            async with self._client() as c:
                r = await c.post(
                    "/v1/facts",
                    json={
                        "user_id": __user__["id"],
                        "text": fact,
                        "project_id": await _project_id(__user__, __metadata__),
                    },
                )
                r.raise_for_status()
            return f"Pinned: {fact}"
        except httpx.HTTPError as e:
            return f"Could not save that memory ({type(e).__name__})."

    async def forget(self, query: str, __user__: dict | None = None, __metadata__: dict | None = None) -> str:
        """
        Delete pinned facts and past-session summaries that match a description, then report what was removed.
        Use when the learner asks you to forget something.
        :param query: What to forget, e.g. "my exam date" or "the chat about Bayes".
        """
        if not __user__ or not __user__.get("id"):
            return "Memory is unavailable (no user)."
        try:
            async with self._client() as c:
                r = await c.post(
                    "/v1/forget",
                    json={
                        "user_id": __user__["id"],
                        "query": query[:500],
                        "project_id": await _project_id(__user__, __metadata__),
                    },
                )
                r.raise_for_status()
                out = r.json()
        except httpx.HTTPError as e:
            return f"Could not forget that ({type(e).__name__})."
        facts = [f["text"] for f in out.get("facts", [])]
        sums = [s.get("title") or s["summary"][:60] for s in out.get("summaries", [])]
        if not facts and not sums:
            return "Nothing matching was stored."
        parts = []
        if facts:
            parts.append("Forgot pinned facts: " + "; ".join(facts))
        if sums:
            parts.append("Forgot session summaries: " + "; ".join(sums))
        return "\n".join(parts)

    async def what_do_you_remember(self, __user__: dict | None = None, __metadata__: dict | None = None) -> str:
        """
        Show everything Leo remembers about the learner in the current project (or outside projects):
        learner profile, pinned facts and the 5 most recent session summaries.
        Use when the learner asks what you remember about them.
        """
        if not __user__ or not __user__.get("id"):
            return "Memory is unavailable (no user)."
        try:
            async with self._client() as c:
                r = await c.get(
                    f"/v1/users/{__user__['id']}/export",
                    params={"recent": 5, "project_id": await _project_id(__user__, __metadata__)},
                )
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as e:
            return f"Could not read memory ({type(e).__name__})."
        prof = data.get("profile") or {}
        view = {
            "weak_spots": [f"{i['concept']} (×{i['count']})" for i in prof.get("weak_spots", [])],
            "mastered": [i["concept"] for i in prof.get("mastered", [])],
            "pinned_facts": [f["text"] for f in data.get("facts", [])],
            "recent_sessions": [
                {
                    "date": str(s.get("updated_at", ""))[:10],
                    "title": s.get("title"),
                    "summary": s.get("summary"),
                    "open_questions": s.get("open_questions"),
                }
                for s in data.get("summaries", [])
            ],
        }
        return json.dumps(view, ensure_ascii=False, indent=1)
