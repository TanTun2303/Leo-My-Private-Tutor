"""Turn Open WebUI messages into compact, memory-safe text (§7.5).

Drops system and tool messages; strips reasoning, citations, tool payloads and
inline images; caps each message. Also selects only the new part of a chat.
"""

from __future__ import annotations

import re
from typing import Any

MAX_MESSAGE_CHARS = 2000
KEEP_ROLES = {"user", "assistant"}

_BLOCK_PATTERNS = [
    re.compile(r"<think>.*?</think>", re.S | re.I),
    re.compile(r"<thinking>.*?</thinking>", re.S | re.I),
    re.compile(r"<reasoning>.*?</reasoning>", re.S | re.I),
    # Open WebUI renders reasoning / tool calls / code execution as <details type="...">
    re.compile(r"<details\b[^>]*\btype=\"(?:reasoning|tool_calls|code_interpreter)\"[^>]*>.*?</details>", re.S | re.I),
    re.compile(r"<source\b[^>]*>.*?</source>", re.S | re.I),
    re.compile(r"<leo_memory>.*?</leo_memory>", re.S | re.I),
]
_UNCLOSED_THINK = re.compile(r"<think>.*\Z", re.S | re.I)  # truncated reasoning at the end
_DATA_URI = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]{16,}")
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


def _binary_or_keep(m: re.Match[str]) -> str:
    """Replace a long run only if it looks like base64 (mixed case plus digits or +/)."""
    s = m.group(0)
    mixed = any(c.islower() for c in s) and any(c.isupper() for c in s)
    return "[binary]" if mixed and any(c.isdigit() or c in "+/" for c in s) else s


_SOURCE_TAG = re.compile(r"</?source(?:_id)?\b[^>]*>", re.I)
_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")


def content_text(content: Any) -> str:
    """Extract text from a str or an OpenAI-style list of content parts (images dropped)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text", "")))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return ""


def clean_text(text: str, cap: int = MAX_MESSAGE_CHARS) -> str:
    for pat in _BLOCK_PATTERNS:
        text = pat.sub("", text)
    text = _UNCLOSED_THINK.sub("", text)
    text = _DATA_URI.sub("[image]", text)
    text = _BASE64_RUN.sub(_binary_or_keep, text)
    text = _SOURCE_TAG.sub("", text)
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text).strip()
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "…"
    return text


def sanitize_messages(messages: list[dict[str, Any]], cap: int = MAX_MESSAGE_CHARS) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        role = m.get("role")
        if role not in KEEP_ROLES:
            continue
        text = clean_text(content_text(m.get("content")), cap)
        if not text:
            continue
        out.append({"id": m.get("id"), "role": role, "content": text})
    return out


def select_new(messages: list[dict[str, Any]], last_message_id: str | None, max_chars: int) -> list[dict[str, Any]]:
    """Messages after last_message_id when ids exist; otherwise the most recent up to max_chars.

    The result is always trimmed from the front to fit max_chars.
    """
    selected = messages
    if last_message_id and any(m.get("id") for m in messages):
        idx = next((i for i, m in enumerate(messages) if m.get("id") == last_message_id), None)
        if idx is not None:
            selected = messages[idx + 1 :]
    total, keep = 0, []
    for m in reversed(selected):
        total += len(m["content"])
        if total > max_chars and keep:
            break
        keep.append(m)
    return list(reversed(keep))
