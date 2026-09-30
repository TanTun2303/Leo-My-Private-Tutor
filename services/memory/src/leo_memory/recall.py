"""Recall: pick relevant memory for the current message and render <leo_memory> (§7.10)."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import numpy as np
import psycopg

from .embeddings import EmbeddingError
from .profile import top_concepts
from .schemas import RecallItem, RecallResponse

log = logging.getLogger(__name__)

SUMMARY_CANDIDATES = 20
SUMMARY_MIN_SIM = 0.35
FACT_NEAREST = 5
FACT_MIN_SIM = 0.30
FACT_RECENT = 2
PROFILE_WEAK = 5
PROFILE_STRONG = 3
RECENCY_DAYS = 30.0

_TRIVIAL = re.compile(
    r"^\s*(hi|hello|hey|yo|thanks?|thank you|ok(ay)?|yes|no|sure|cool|great|bye"
    r"|안녕(하세요)?|감사합니다|고마워|네|응)\W*$",
    re.I,
)


# Qwen3-Embedding is instruction-aware: queries get a task prefix, stored documents don't.
QUERY_INSTRUCTION = (
    "Instruct: Given a student's message to a tutor, retrieve summaries of past study sessions "
    "that are relevant to it\nQuery: "
)

# Messages that point at earlier sessions without naming a topic ("what did we do last time?").
_PAST = re.compile(
    r"\b(last time|last session|previous(ly)?|before|earlier|remember|recap|where (did )?we (leave|left)|"
    r"yesterday|last week|again)\b|지난번|저번|이전에|기억|어제",
    re.I,
)


def refers_to_past(text: str) -> bool:
    return bool(_PAST.search(text))


class EmbedOne(Protocol):
    def embed_one(self, text: str) -> list[float]: ...


def is_trivial(text: str) -> bool:
    t = text.strip()
    return not t or bool(_TRIVIAL.match(t)) or (len(t.split()) < 3 and len(t) < 12)


def score(sim: float, age_days: float) -> float:
    return 0.7 * sim + 0.3 * math.exp(-max(0.0, age_days) / RECENCY_DAYS)


@dataclass
class Candidate:
    chat_id: str
    title: str
    summary: str
    open_questions: list[str]
    updated_at: datetime
    sim: float
    score: float


def _age_days(ts: datetime, now: datetime) -> float:
    return (now - ts).total_seconds() / 86400


def _summaries(
    conn: psycopg.Connection,
    user_id: str,
    qvec: list[float] | None,
    exclude: str | None,
    k: int,
    now: datetime,
    project_id: str = "",
) -> list[Candidate]:
    if qvec is None:  # trivial message or embedding failure → recency
        rows = conn.execute(
            """SELECT chat_id, title, summary, open_questions, updated_at, 0.0 AS sim
               FROM memory.chat_summaries
               WHERE user_id = %s AND project_id = %s AND chat_id IS DISTINCT FROM %s
               ORDER BY updated_at DESC LIMIT %s""",
            (user_id, project_id, exclude, k),
        ).fetchall()
        return [
            Candidate(
                r["chat_id"],
                r["title"] or "",
                r["summary"],
                r["open_questions"],
                r["updated_at"],
                0.0,
                score(0.0, _age_days(r["updated_at"], now)),
            )
            for r in rows
        ]
    rows = conn.execute(
        """SELECT chat_id, title, summary, open_questions, updated_at, 1 - (embedding <=> %s) AS sim
           FROM memory.chat_summaries
           WHERE user_id = %s AND project_id = %s AND chat_id IS DISTINCT FROM %s AND embedding IS NOT NULL
           ORDER BY embedding <=> %s LIMIT %s""",
        (np.array(qvec), user_id, project_id, exclude, np.array(qvec), SUMMARY_CANDIDATES),
    ).fetchall()
    cands = [
        Candidate(
            r["chat_id"],
            r["title"] or "",
            r["summary"],
            r["open_questions"],
            r["updated_at"],
            float(r["sim"]),
            score(float(r["sim"]), _age_days(r["updated_at"], now)),
        )
        for r in rows
        if float(r["sim"]) >= SUMMARY_MIN_SIM
    ]
    cands.sort(key=lambda c: -c.score)
    return cands[:k]


def _facts(
    conn: psycopg.Connection, user_id: str, qvec: list[float] | None, project_id: str = ""
) -> list[dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if qvec is not None:
        for r in conn.execute(
            """SELECT id, text, created_at, 1 - (embedding <=> %s) AS sim FROM memory.facts
               WHERE user_id = %s AND project_id = %s AND embedding IS NOT NULL ORDER BY embedding <=> %s LIMIT %s""",
            (np.array(qvec), user_id, project_id, np.array(qvec), FACT_NEAREST),
        ):
            if float(r["sim"]) >= FACT_MIN_SIM:
                out[r["id"]] = {**r, "sim": float(r["sim"])}
    for r in conn.execute(
        """SELECT id, text, created_at, NULL AS sim FROM memory.facts
           WHERE user_id = %s AND project_id = %s ORDER BY created_at DESC LIMIT %s""",
        (user_id, project_id, FACT_RECENT),
    ):
        out.setdefault(r["id"], dict(r))
    return list(out.values())


def _profile(
    conn: psycopg.Connection, user_id: str, project_id: str = ""
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    r = conn.execute(
        "SELECT weak_spots, mastered FROM memory.learner_profile WHERE user_id = %s AND project_id = %s",
        (user_id, project_id),
    ).fetchone()
    if not r:
        return [], []
    return top_concepts(r["weak_spots"], PROFILE_WEAK), top_concepts(r["mastered"], PROFILE_STRONG)


def _profile_line(weak: list[dict[str, Any]], strong: list[dict[str, Any]]) -> str:
    def fmt(i: dict[str, Any]) -> str:
        return f"{i['concept']} (×{i['count']})" if int(i.get("count", 1)) > 1 else str(i["concept"])

    parts = []
    if weak:
        parts.append("weak: " + ", ".join(fmt(i) for i in weak))
    if strong:
        parts.append("strong: " + ", ".join(str(i["concept"]) for i in strong))
    return ("Profile — " + " · ".join(parts)) if parts else ""


def _session_line(c: Candidate, tz: ZoneInfo) -> str:
    date = c.updated_at.astimezone(tz).strftime("%Y-%m-%d")
    line = f'- {date} "{c.title}": {c.summary.rstrip(".")}' if c.title else f"- {date}: {c.summary.rstrip('.')}"
    if c.open_questions:
        line += "; open: " + ", ".join(q.rstrip(". ") for q in c.open_questions[:2])
    return line + "."


def render(profile_line: str, sessions: list[str], facts: list[str], max_chars: int) -> str:
    """Fit the block into max_chars. Priority: profile, pinned facts, then sessions (best first)."""
    head, tail = "<leo_memory>", "</leo_memory>"
    budget = max_chars - len(head) - len(tail) - 2  # two newlines around the body
    lines: list[str] = []

    def used(extra: list[str]) -> int:
        return len("\n".join(lines + extra))

    if profile_line and used([profile_line]) <= budget:
        lines.append(profile_line)
    if facts:
        pinned = "Pinned: "
        kept: list[str] = []
        for f in facts:
            cand = pinned + " ".join(s.rstrip(".") + "." for s in [*kept, f])
            if used([cand]) <= budget:
                kept.append(f)
        if kept:
            lines.append(pinned + " ".join(s.rstrip(".") + "." for s in kept))
    session_lines: list[str] = []
    for s in sessions:
        trial = ["Past sessions:", *session_lines, s]
        if used(trial) <= budget:
            session_lines.append(s)
    if session_lines:
        # Sessions go between profile and pinned facts, as in the spec example.
        insert_at = 1 if profile_line and lines and lines[0] == profile_line else 0
        lines[insert_at:insert_at] = ["Past sessions:", *session_lines]
    if not lines:
        return ""
    return f"{head}\n" + "\n".join(lines) + f"\n{tail}"


def recall(
    conn: psycopg.Connection,
    embedder: EmbedOne,
    user_id: str,
    query: str,
    exclude_chat_id: str | None,
    top_k: int,
    max_chars: int,
    tz_name: str = "UTC",
    now: datetime | None = None,
    project_id: str = "",
) -> RecallResponse:
    """Recall within one scope: a project (Open WebUI folder id) or the global scope ("")."""
    now = now or datetime.now(UTC)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — bad TZ must not break recall
        tz = ZoneInfo("UTC")
    qvec: list[float] | None = None
    if not is_trivial(query):
        try:
            qvec = embedder.embed_one(QUERY_INSTRUCTION + query[:2000])
        except EmbeddingError as e:
            log.warning("recall embedding failed, using recency: %s", e)

    sums = _summaries(conn, user_id, qvec, exclude_chat_id, top_k, now, project_id)
    if qvec is not None and len(sums) < top_k and refers_to_past(query):
        # Fill free slots with the most recent sessions, below the semantic matches.
        seen = {c.chat_id for c in sums}
        recent = _summaries(conn, user_id, None, exclude_chat_id, top_k, now, project_id)
        sums += [c for c in recent if c.chat_id not in seen][: top_k - len(sums)]
    facts = _facts(conn, user_id, qvec, project_id)
    weak, strong = _profile(conn, user_id, project_id)

    items: list[RecallItem] = []
    pl = _profile_line(weak, strong)
    if pl:
        items.append(RecallItem(type="profile", id=user_id, text=pl))
    items += [RecallItem(type="summary", id=c.chat_id, score=round(c.score, 4), text=c.summary) for c in sums]
    items += [RecallItem(type="fact", id=str(f["id"]), score=f.get("sim"), text=f["text"]) for f in facts]

    block = render(pl, [_session_line(c, tz) for c in sums], [f["text"] for f in facts], max_chars)
    return RecallResponse(block=block, items=items)
