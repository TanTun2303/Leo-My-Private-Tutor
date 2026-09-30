"""Deterministic learner-profile merge (§7.9).

weak_spots / mastered are lists of {"concept", "count", "last_seen"} (ISO timestamps).
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

MAX_ITEMS = 15
HALF_LIFE_DAYS = 30.0

_WS = re.compile(r"\s+")


def normalize(concept: str) -> str:
    """Lowercase, trim, collapse spaces, simple singularization of the last word."""
    c = _WS.sub(" ", concept.strip().lower()).strip(" .,;:!?")
    if not c:
        return ""
    head, _, last = c.rpartition(" ")
    if len(last) > 4 and last.endswith("ies"):
        last = last[:-3] + "y"
    elif len(last) > 3 and last.endswith("s") and not last.endswith(("ss", "us", "is")):
        last = last[:-1]
    return f"{head} {last}".strip()


def _parse_ts(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(v))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return datetime.fromtimestamp(0, UTC)


def rank(item: dict[str, Any], now: datetime) -> float:
    age_days = max(0.0, (now - _parse_ts(item.get("last_seen"))).total_seconds() / 86400)
    return float(item.get("count", 1)) * math.pow(0.5, age_days / HALF_LIFE_DAYS)


def _top(items: dict[str, dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    ordered = sorted(items.values(), key=lambda i: (-rank(i, now), i["concept"]))
    return ordered[:MAX_ITEMS]


def merge_profile(
    weak: list[dict[str, Any]],
    mastered: list[dict[str, Any]],
    new_weak: list[str],
    new_mastered: list[str],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ts = now.isoformat()
    w = {i["concept"]: dict(i) for i in weak if i.get("concept")}
    m = {i["concept"]: dict(i) for i in mastered if i.get("concept")}

    for raw in new_weak:
        c = normalize(raw)
        if not c:
            continue
        item = w.setdefault(c, {"concept": c, "count": 0})
        item["count"] = int(item["count"]) + 1
        item["last_seen"] = ts

    for raw in new_mastered:
        c = normalize(raw)
        if not c:
            continue
        item = m.setdefault(c, {"concept": c, "count": 0})
        item["count"] = int(item["count"]) + 1
        item["last_seen"] = ts
        if c in w:
            w[c]["count"] = int(w[c]["count"]) - 1
            if w[c]["count"] <= 0:
                del w[c]

    return _top(w, now), _top(m, now)


def merge_into_db(
    conn: psycopg.Connection, user_id: str, new_weak: list[str], new_mastered: list[str], project_id: str = ""
) -> None:
    """Merge inside the caller's transaction (row locked FOR UPDATE). One profile per user and project."""
    now = datetime.now(UTC)
    conn.execute(
        """INSERT INTO memory.learner_profile (user_id, project_id) VALUES (%s, %s)
           ON CONFLICT (user_id, project_id) DO NOTHING""",
        (user_id, project_id),
    )
    row = conn.execute(
        """SELECT weak_spots, mastered FROM memory.learner_profile
           WHERE user_id = %s AND project_id = %s FOR UPDATE""",
        (user_id, project_id),
    ).fetchone()
    assert row is not None
    weak, mast = merge_profile(row["weak_spots"], row["mastered"], new_weak, new_mastered, now)  # type: ignore[index]
    conn.execute(
        """UPDATE memory.learner_profile SET weak_spots = %s, mastered = %s, updated_at = now()
           WHERE user_id = %s AND project_id = %s""",
        (Jsonb(weak), Jsonb(mast), user_id, project_id),
    )


def top_concepts(items: list[dict[str, Any]], n: int, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    return sorted(items, key=lambda i: (-rank(i, now), i.get("concept", "")))[:n]
