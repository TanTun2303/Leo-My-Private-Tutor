"""Compress a chat into a concise SummaryV1 with leo-llm, thinking off (§7.8)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from pydantic import ValidationError

from .schemas import SummaryV1

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You compress a tutoring conversation into durable memory for a study assistant.
Return ONLY JSON matching the schema.

Rules:
- summary: at most {max_words} words, third person ("The user…"), covering what was studied and where they ended.
- key_points: at most 5 facts worth remembering later (goals, deadlines, preferences, conclusions), each ≤ 20 words.
- topics: at most 6 short lowercase tags (e.g. "dijkstra", "master theorem").
- weak_spots: at most 3 concepts the user answered wrongly, was unsure about, or said they struggle with or keep confusing.
- mastered: at most 3 concepts the user demonstrated correctly under questioning.
- open_questions: at most 3 unresolved threads to pick up next time.
- If previous_summary is given, MERGE: update and replace outdated items; do not append blindly.
- Exclude greetings, tool logs, citations, reasoning traces, and anything the user asked to forget.
- Write plain text (no LaTeX) in English regardless of the conversation language; keep technical terms exact."""

LIMITS = {"key_points": 5, "topics": 6, "weak_spots": 3, "mastered": 3, "open_questions": 3}
SUMMARY_MAX_CHARS = 600  # DB CHECK constraint
KEY_POINT_MAX_WORDS = 20
ITEM_MAX_CHARS = 120


class SummaryError(RuntimeError):
    pass


class ChatJSON(Protocol):
    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any], max_tokens: int = 500) -> str: ...


def _schema(max_words: int) -> dict[str, Any]:
    def arr(n: int) -> dict[str, Any]:
        return {"type": "array", "items": {"type": "string"}, "maxItems": n}

    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            **{k: arr(n) for k, n in LIMITS.items()},
        },
        "required": ["summary", *LIMITS.keys()],
        "additionalProperties": False,
    }


def _words(text: str, n: int) -> str:
    w = text.split()
    return text.strip() if len(w) <= n else " ".join(w[:n]).rstrip(",;:") + "…"


_LATEX = re.compile(r"\$+|\\[()\[\]]")


def enforce_limits(s: SummaryV1, max_words: int) -> SummaryV1:
    """Clamp every field to the spec's limits, whatever the model returned."""

    def items(xs: list[str], n: int, lower: bool = False) -> list[str]:
        out: list[str] = []
        for x in xs:
            x = _LATEX.sub("", str(x)).strip()
            if lower:
                x = x.lower()
            x = x[:ITEM_MAX_CHARS]
            if x and x not in out:
                out.append(x)
        return out[:n]

    summary = _words(_LATEX.sub("", s.summary), max_words)[:SUMMARY_MAX_CHARS]
    return SummaryV1(
        summary=summary,
        key_points=[_words(k, KEY_POINT_MAX_WORDS) for k in items(s.key_points, LIMITS["key_points"])],
        topics=items(s.topics, LIMITS["topics"], lower=True),
        weak_spots=items(s.weak_spots, LIMITS["weak_spots"]),
        mastered=items(s.mastered, LIMITS["mastered"]),
        open_questions=items(s.open_questions, LIMITS["open_questions"]),
    )


def _parse(raw: str) -> SummaryV1:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    data = json.loads(raw)
    s = SummaryV1.model_validate(data)
    if not s.summary.strip():
        raise ValueError("empty summary")
    return s


def summarize(
    llm: ChatJSON,
    title: str | None,
    previous: dict[str, Any] | None,
    messages: list[dict[str, Any]],
    max_words: int,
) -> SummaryV1:
    """Run the summarizer; validate; retry once; raise SummaryError on failure."""
    user_payload = {
        "title": title or "",
        "previous_summary": previous,
        "conversation": [{"role": m["role"], "content": m["content"]} for m in messages],
    }
    prompt = [
        {"role": "system", "content": SYSTEM_PROMPT.format(max_words=max_words)},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            raw = llm.chat_json(prompt, _schema(max_words), max_tokens=500)
            return enforce_limits(_parse(raw), max_words)
        except (ValueError, ValidationError) as e:  # json.JSONDecodeError is a ValueError
            last_err = e
            log.warning("summarizer output invalid (attempt %d): %s", attempt, type(e).__name__)
    raise SummaryError(f"invalid summarizer output after retry: {last_err}")
