"""Client for leo-llm (llama-server, OpenAI-compatible) used by the summarizer."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, transport: httpx.BaseTransport | None = None) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.root_url = self.base_url.removesuffix("/v1")
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=httpx.Timeout(300.0, connect=5.0),
            transport=transport,
        )

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any], max_tokens: int = 500) -> str:
        """Non-thinking chat completion constrained to a JSON schema. Returns the raw content."""
        body = {
            "model": self.model,
            "messages": messages,
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.5,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "SummaryV1", "schema": schema}},
        }
        r = self._http.post(f"{self.base_url}/chat/completions", json=body)
        r.raise_for_status()
        return str(r.json()["choices"][0]["message"].get("content") or "")

    def healthy(self) -> bool:
        try:
            return self._http.get(f"{self.root_url}/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def busy(self) -> bool:
        """True if any llama-server slot is processing a request (GET /slots, needs --slots)."""
        try:
            r = self._http.get(f"{self.root_url}/slots", timeout=3)
            r.raise_for_status()
            return any(bool(s.get("is_processing")) for s in r.json())
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.debug("slots check failed: %s", e)
            return False

    def close(self) -> None:
        self._http.close()
