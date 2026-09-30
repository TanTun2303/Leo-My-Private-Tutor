"""
title: Think
author: leo
version: 1.0.0
description: Deep reasoning for this message (slower, stronger on proofs and hard problems).
"""

from pydantic import BaseModel

# Brain outline icon (Lucide "brain", MIT), as a data URI.
_ICON = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tl"
    "PSJjdXJyZW50Q29sb3IiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5k"
    "Ij48cGF0aCBkPSJNMTIgNWEzIDMgMCAxIDAtNS45OTcuMTI1IDQgNCAwIDAgMC0yLjUyNiA1Ljc3IDQgNCAwIDAgMCAuNTU2IDYuNTg4"
    "QTQgNCAwIDEgMCAxMiAxOFoiLz48cGF0aCBkPSJNMTIgNWEzIDMgMCAxIDEgNS45OTcuMTI1IDQgNCAwIDAgMSAyLjUyNiA1Ljc3IDQg"
    "NCAwIDAgMS0uNTU2IDYuNTg4QTQgNCAwIDEgMSAxMiAxOFoiLz48cGF0aCBkPSJNMTIgNXYxMyIvPjwvc3ZnPg=="
)


class Filter:
    class Valves(BaseModel):
        max_tokens: int = 16384
        priority: int = 10

    def __init__(self) -> None:
        self.valves = self.Valves()
        self.toggle = True  # rendered as a toggle button in the chat input (v0.11.4: `toggle`)
        self.icon = _ICON

    async def inlet(self, body: dict, __user__: dict | None = None) -> dict:
        try:
            kwargs = dict(body.get("chat_template_kwargs") or {})
            kwargs["enable_thinking"] = True
            body["chat_template_kwargs"] = kwargs
            # Qwen3.6 recommended sampling for thinking mode.
            body.update(temperature=1.0, top_p=0.95, top_k=20, presence_penalty=1.5)
            body["max_tokens"] = max(int(body.get("max_tokens") or 0), self.valves.max_tokens)
        except Exception:
            pass
        return body
