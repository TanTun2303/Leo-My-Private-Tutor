"""leo-memory worker: summarize idle chats, merge the profile, maintenance (§7.6).

Run: python -m leo_memory.worker
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .db import make_pool
from .embeddings import Embedder
from .llm import LLMClient
from .migrate import migrate
from .profile import merge_into_db
from .settings import Settings, get_settings
from .summarizer import summarize

log = logging.getLogger("leo_memory.worker")

MAINTENANCE_INTERVAL = 3600
RECONCILE_BATCH = 500


# ── job lifecycle ────────────────────────────────────────────────────────────


def claim(pool: ConnectionPool) -> dict[str, Any] | None:
    """Atomically take one due job; commit at once so no lock is held during the LLM call."""
    with pool.connection() as c:
        return c.execute(
            """
            WITH due AS (
              SELECT chat_id FROM memory.jobs
              WHERE status = 'pending' AND run_after <= clock_timestamp()
              ORDER BY run_after LIMIT 1 FOR UPDATE SKIP LOCKED)
            UPDATE memory.jobs j SET status = 'running', attempts = j.attempts + 1, updated_at = clock_timestamp()
            FROM due WHERE j.chat_id = due.chat_id
            RETURNING j.*
            """
        ).fetchone()


def requeue(pool: ConnectionPool, job: dict[str, Any], delay_s: float, error: str | None, count_attempt: bool) -> None:
    """Put a claimed job back to pending — only if no newer turn replaced it meanwhile."""
    with pool.connection() as c:
        c.execute(
            """UPDATE memory.jobs SET status = 'pending',
                 run_after = clock_timestamp() + %s * interval '1 second',
                 attempts = CASE WHEN %s THEN attempts ELSE GREATEST(attempts - 1, 0) END,
                 last_error = COALESCE(%s, last_error), updated_at = clock_timestamp()
               WHERE chat_id = %s AND updated_at = %s""",
            (delay_s, count_attempt, error, job["chat_id"], job["updated_at"]),
        )


def mark_dead(pool: ConnectionPool, job: dict[str, Any], error: str) -> None:
    with pool.connection() as c:
        c.execute(
            "UPDATE memory.jobs SET status = 'dead', last_error = %s WHERE chat_id = %s AND updated_at = %s",
            (error[:1000], job["chat_id"], job["updated_at"]),
        )


def finish(conn: psycopg.Connection, job: dict[str, Any]) -> bool:
    """Delete the job if unchanged since the claim. False → a newer turn arrived (job stays pending)."""
    n = conn.execute(
        "DELETE FROM memory.jobs WHERE chat_id = %s AND updated_at = %s", (job["chat_id"], job["updated_at"])
    ).rowcount
    if n == 0:
        # A newer turn already set status='pending' with a fresh payload; make sure it isn't stuck 'running'.
        conn.execute(
            "UPDATE memory.jobs SET status = 'pending' WHERE chat_id = %s AND status = 'running'", (job["chat_id"],)
        )
    return n == 1


def backoff_seconds(attempts: int, cap: int) -> int:
    return int(min(cap, 30 * (2 ** max(0, attempts - 1))))


# ── processing ───────────────────────────────────────────────────────────────


def process(pool: ConnectionPool, llm: Any, embedder: Any, s: Settings, job: dict[str, Any]) -> str:
    """Summarize one claimed job. Returns an outcome label (for logs/tests)."""
    with pool.connection() as c:
        prev = c.execute(
            """SELECT user_id, title, summary, key_points, topics, weak_spots, mastered, open_questions,
                      message_count, last_message_id
               FROM memory.chat_summaries WHERE chat_id = %s""",
            (job["chat_id"],),
        ).fetchone()
    messages: list[dict[str, Any]] = list(job["payload"].get("messages") or [])
    # Skip messages already summarized by a run that finished after this payload was built.
    if prev and prev["last_message_id"]:
        ids = [m.get("id") for m in messages]
        if prev["last_message_id"] in ids:
            messages = messages[ids.index(prev["last_message_id"]) + 1 :]
    if not any(m["role"] == "user" for m in messages):
        with pool.connection() as c:
            finish(c, job)
        return "nothing-new"

    previous = None
    if prev:
        previous = {k: prev[k] for k in ("summary", "key_points", "topics", "weak_spots", "mastered", "open_questions")}
    title = job.get("title") or (prev["title"] if prev else None)
    result = summarize(llm, title, previous, messages, s.memory_summary_max_words)
    text = f"{title or ''}\n{result.summary}\n{', '.join(result.topics)}".strip()
    vec = np.array(embedder.embed_one(text))
    last_id = next((m.get("id") for m in reversed(messages) if m.get("id")), None)

    with pool.connection() as c, c.transaction():
        c.execute(
            """
            INSERT INTO memory.chat_summaries AS cs
              (chat_id, user_id, project_id, title, summary, key_points, topics, weak_spots, mastered,
               open_questions, message_count, last_message_id, embedding)
            VALUES (%(chat_id)s, %(user_id)s, %(project_id)s, %(title)s, %(summary)s, %(kp)s, %(topics)s,
                    %(weak)s, %(mast)s, %(open)s, %(n)s, %(last)s, %(emb)s)
            ON CONFLICT (chat_id) DO UPDATE SET
              project_id = EXCLUDED.project_id,  -- a chat moved into / out of a project follows it
              title = COALESCE(EXCLUDED.title, cs.title), summary = EXCLUDED.summary,
              key_points = EXCLUDED.key_points, topics = EXCLUDED.topics, weak_spots = EXCLUDED.weak_spots,
              mastered = EXCLUDED.mastered, open_questions = EXCLUDED.open_questions,
              message_count = cs.message_count + EXCLUDED.message_count,
              last_message_id = COALESCE(EXCLUDED.last_message_id, cs.last_message_id),
              embedding = EXCLUDED.embedding, updated_at = now()
            """,
            {
                "chat_id": job["chat_id"],
                "project_id": job.get("project_id") or "",
                "user_id": job["user_id"],
                "title": title,
                "summary": result.summary,
                "kp": Jsonb(result.key_points),
                "topics": result.topics,
                "weak": Jsonb(result.weak_spots),
                "mast": Jsonb(result.mastered),
                "open": Jsonb(result.open_questions),
                "n": len(messages),
                "last": last_id,
                "emb": vec,
            },
        )
        merge_into_db(c, job["user_id"], result.weak_spots, result.mastered, job.get("project_id") or "")
        done = finish(c, job)
    return "done" if done else "done-newer-pending"


def run_one(pool: ConnectionPool, llm: Any, embedder: Any, s: Settings) -> str | None:
    """Claim and handle a single job. Returns the outcome, or None if nothing was due."""
    job = claim(pool)
    if job is None:
        return None
    waited = (datetime.now(UTC) - job["first_pending_at"]).total_seconds()
    if not llm.healthy():
        requeue(pool, job, s.worker_busy_requeue_seconds, None, count_attempt=False)
        return "llm-unhealthy"
    if llm.busy() and waited < s.memory_max_wait_seconds:
        requeue(pool, job, s.worker_busy_requeue_seconds, None, count_attempt=False)
        return "llm-busy"
    try:
        outcome = process(pool, llm, embedder, s, job)
        log.info("chat=%s %s (attempt %d)", job["chat_id"], outcome, job["attempts"])
        return outcome
    except Exception as e:  # noqa: BLE001 — any failure is retried with backoff
        err = f"{type(e).__name__}: {e}"[:500]
        if job["attempts"] >= s.worker_max_attempts:
            mark_dead(pool, job, err)
            log.error("chat=%s dead after %d attempts: %s", job["chat_id"], job["attempts"], type(e).__name__)
            return "dead"
        delay = backoff_seconds(job["attempts"], s.worker_backoff_cap_seconds)
        requeue(pool, job, delay, err, count_attempt=True)
        log.warning("chat=%s failed (%s); retry in %ds", job["chat_id"], type(e).__name__, delay)
        return "retry"


# ── maintenance ──────────────────────────────────────────────────────────────


def reset_stale(pool: ConnectionPool, s: Settings) -> int:
    with pool.connection() as c:
        return c.execute(
            """UPDATE memory.jobs SET status = 'pending', run_after = clock_timestamp()
               WHERE status = 'running' AND updated_at < clock_timestamp() - %s * interval '1 second'""",
            (s.worker_stale_running_seconds,),
        ).rowcount


def purge_retention(pool: ConnectionPool, s: Settings) -> int:
    if s.memory_retention_days <= 0:
        return 0
    with pool.connection() as c:
        return c.execute(
            "DELETE FROM memory.chat_summaries WHERE updated_at < now() - %s * interval '1 day'",
            (s.memory_retention_days,),
        ).rowcount


def reconcile(pool: ConnectionPool, s: Settings) -> int | None:
    """Delete summaries of chats deleted in Open WebUI. None = skipped (no access / no table)."""
    if not s.reconcile_database_url:
        return None
    try:
        with psycopg.connect(s.reconcile_database_url, connect_timeout=5) as owui:
            table = owui.execute("SELECT to_regclass('public.chat')").fetchone()
            if not table or table[0] is None:
                log.warning("reconcile: Open WebUI chat table not found; skipping")
                return None
            with pool.connection() as c:
                ids = [
                    r["chat_id"]
                    for r in c.execute(
                        "SELECT chat_id FROM memory.chat_summaries UNION SELECT chat_id FROM memory.jobs"
                    )
                ]
            gone: list[str] = []
            for i in range(0, len(ids), RECONCILE_BATCH):
                batch = ids[i : i + RECONCILE_BATCH]
                present = {r[0] for r in owui.execute("SELECT id FROM public.chat WHERE id = ANY(%s)", (batch,))}
                gone += [x for x in batch if x not in present]
    except psycopg.Error as e:
        log.warning("reconcile skipped: %s", type(e).__name__)
        return None
    if gone:
        with pool.connection() as c:
            c.execute("DELETE FROM memory.chat_summaries WHERE chat_id = ANY(%s)", (gone,))
            c.execute("DELETE FROM memory.jobs WHERE chat_id = ANY(%s)", (gone,))
    return len(gone) + reconcile_projects(pool, s)


PROJECT_TABLES = ("chat_summaries", "facts", "learner_profile", "jobs")


def reconcile_projects(pool: ConnectionPool, s: Settings) -> int:
    """Delete memory scoped to projects (Open WebUI folders) that no longer exist. Returns projects removed."""
    with pool.connection() as c:
        pids = [
            r["project_id"]
            for r in c.execute(
                " UNION ".join(f"SELECT project_id FROM memory.{t} WHERE project_id <> ''" for t in PROJECT_TABLES)
            )
        ]
    if not pids:
        return 0
    try:
        with psycopg.connect(s.reconcile_database_url, connect_timeout=5) as owui:
            table = owui.execute("SELECT to_regclass('public.folder')").fetchone()
            if not table or table[0] is None:
                return 0
            present = {r[0] for r in owui.execute("SELECT id FROM public.folder WHERE id = ANY(%s)", (pids,))}
    except psycopg.Error as e:
        log.warning("project reconcile skipped: %s", type(e).__name__)
        return 0
    gone = [p for p in pids if p not in present]
    if gone:
        with pool.connection() as c:
            for t in PROJECT_TABLES:
                c.execute(f"DELETE FROM memory.{t} WHERE project_id = ANY(%s)", (gone,))
        log.info("reconcile: removed memory of %d deleted project(s)", len(gone))
    return len(gone)


def maintenance(pool: ConnectionPool, s: Settings) -> None:
    stale = reset_stale(pool, s)
    purged = purge_retention(pool, s)
    removed = reconcile(pool, s)
    log.info("maintenance: stale=%d purged=%d reconciled=%s", stale, purged, removed)


# ── main loop ────────────────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = get_settings()
    migrate(s.database_url, s.leo_migrations_dir)
    pool = make_pool(s.database_url, max_size=3)
    llm = LLMClient(s.llm_base_url, s.llm_api_key, s.llm_model)
    embedder = Embedder(s.embed_base_url, s.embed_api_key, s.embed_dim)
    stop = False

    def _stop(*_: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Jobs left 'running' by a crashed worker become due again.
    with pool.connection() as c:
        c.execute("UPDATE memory.jobs SET status = 'pending' WHERE status = 'running'")
    log.info("worker started (idle=%ss, max_wait=%ss)", s.memory_idle_seconds, s.memory_max_wait_seconds)
    next_maint = time.monotonic()
    while not stop:
        try:
            if time.monotonic() >= next_maint:
                maintenance(pool, s)
                next_maint = time.monotonic() + MAINTENANCE_INTERVAL
            while not stop and run_one(pool, llm, embedder, s) is not None:
                pass
        except Exception:  # noqa: BLE001 — keep the loop alive (e.g. DB restart)
            log.exception("worker loop error")
        for _ in range(int(s.worker_poll_seconds * 10)):
            if stop:
                break
            time.sleep(0.1)
    pool.close()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
