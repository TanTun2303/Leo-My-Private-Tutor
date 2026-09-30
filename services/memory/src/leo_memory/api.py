"""leo-memory HTTP API (§7.4). Run: uvicorn leo_memory.api:app"""

import hmac
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import numpy as np
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .db import make_pool
from .embeddings import Embedder, EmbeddingError
from .migrate import migrate
from .recall import recall
from .sanitize import sanitize_messages, select_new
from .schemas import FactIn, ForgetIn, RecallResponse, TurnEvent
from .settings import Settings, get_settings

log = logging.getLogger("leo_memory.api")

FORGET_MIN_SIM = 0.55
# Open WebUI v0.11.4 (utils/chat_id.py): temporary and channel chats are never saved.
NON_SAVED_CHAT_PREFIXES = ("temporary:", "local:", "channel:")


def upsert_job(conn: psycopg.Connection, s: Settings, ev: TurnEvent) -> str:
    """Sanitize a turn and upsert its debounced job. Returns 'queued' or a skip reason."""
    if not ev.chat_id or ev.chat_id.startswith(NON_SAVED_CHAT_PREFIXES):
        return "skipped: temporary chat"
    if not ev.memory_enabled:
        return "skipped: memory disabled"
    msgs = sanitize_messages([m.model_dump() for m in ev.messages])
    row = conn.execute(
        "SELECT user_id, last_message_id FROM memory.chat_summaries WHERE chat_id = %s", (ev.chat_id,)
    ).fetchone()
    if row and row["user_id"] != ev.user_id:
        return "skipped: chat belongs to another user"
    new = select_new(msgs, row["last_message_id"] if row else None, s.memory_max_input_chars)
    if not any(m["role"] == "user" for m in new):
        return "skipped: nothing new"
    conn.execute(
        """
        INSERT INTO memory.jobs AS j (chat_id, user_id, project_id, title, payload, status, first_pending_at,
                                      run_after, updated_at)
        VALUES (%(chat_id)s, %(user_id)s, %(project_id)s, %(title)s, %(payload)s, 'pending', clock_timestamp(),
                clock_timestamp() + LEAST(%(idle)s, %(max_wait)s) * interval '1 second', clock_timestamp())
        ON CONFLICT (chat_id) DO UPDATE SET
          user_id = EXCLUDED.user_id,
          project_id = EXCLUDED.project_id,
          title = COALESCE(EXCLUDED.title, j.title),
          payload = EXCLUDED.payload,
          attempts = CASE WHEN j.status = 'dead' THEN 0 ELSE j.attempts END,
          last_error = CASE WHEN j.status = 'dead' THEN NULL ELSE j.last_error END,
          first_pending_at = CASE WHEN j.status = 'dead' THEN clock_timestamp() ELSE j.first_pending_at END,
          status = 'pending',
          run_after = LEAST(
            clock_timestamp() + %(idle)s * interval '1 second',
            (CASE WHEN j.status = 'dead' THEN clock_timestamp() ELSE j.first_pending_at END)
              + %(max_wait)s * interval '1 second'),
          updated_at = clock_timestamp()
        """,
        {
            "chat_id": ev.chat_id,
            "user_id": ev.user_id,
            "project_id": ev.project_id,
            "title": ev.title,
            "payload": Jsonb({"messages": new}),
            "idle": s.memory_idle_seconds,
            "max_wait": s.memory_max_wait_seconds,
        },
    )
    return "queued"


def create_app(
    settings: Settings | None = None,
    embedder: Any | None = None,
    pool: ConnectionPool | None = None,
) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        migrate(s.database_url, s.leo_migrations_dir)
        own_pool = pool is None
        app.state.pool = pool or make_pool(s.database_url)
        app.state.embedder = embedder or Embedder(s.embed_base_url, s.embed_api_key, s.embed_dim)
        try:
            yield
        finally:
            if own_pool:
                app.state.pool.close()

    app = FastAPI(title="leo-memory", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {s.leo_memory_token}"
        if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

    def conn(request: Request) -> Iterator[psycopg.Connection]:
        with request.app.state.pool.connection() as c:
            yield c

    Auth = Depends(auth)
    Conn = Annotated[psycopg.Connection, Depends(conn)]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(c: Conn) -> dict[str, str]:
        c.execute("SELECT 1 FROM memory.schema_migrations LIMIT 1")
        return {"status": "ready"}

    @app.post("/v1/events/turn", status_code=202, dependencies=[Auth])
    def turn(ev: TurnEvent, c: Conn) -> dict[str, str]:
        result = upsert_job(c, s, ev)
        log.info("turn chat=%s user=%s msgs=%d → %s", ev.chat_id, ev.user_id, len(ev.messages), result)
        return {"status": result}

    @app.get("/v1/recall", response_model=RecallResponse, dependencies=[Auth])
    def get_recall(
        request: Request,
        c: Conn,
        user_id: str,
        q: str = "",
        exclude_chat_id: str | None = None,
        project_id: str = "",
    ) -> RecallResponse:
        return recall(
            c,
            request.app.state.embedder,
            user_id,
            q,
            exclude_chat_id,
            s.memory_recall_top_k,
            s.memory_max_inject_chars,
            s.tz,
            project_id=project_id,
        )

    @app.get("/v1/chats/{chat_id}/summary", dependencies=[Auth])
    def get_summary(chat_id: str, c: Conn) -> dict[str, Any]:
        row = c.execute(
            """SELECT chat_id, user_id, title, summary, key_points, topics, weak_spots, mastered,
                      open_questions, message_count, last_message_id, created_at, updated_at
               FROM memory.chat_summaries WHERE chat_id = %s""",
            (chat_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "no summary")
        return dict(row)

    @app.delete("/v1/chats/{chat_id}", dependencies=[Auth])
    def forget_chat(chat_id: str, c: Conn, user_id: str | None = None) -> dict[str, int]:
        owner = "" if user_id is None else " AND user_id = %s"
        args: tuple[Any, ...] = (chat_id,) if user_id is None else (chat_id, user_id)
        n = c.execute(f"DELETE FROM memory.chat_summaries WHERE chat_id = %s{owner}", args).rowcount
        j = c.execute(f"DELETE FROM memory.jobs WHERE chat_id = %s{owner}", args).rowcount
        return {"summaries": n, "jobs": j}

    @app.post("/v1/facts", status_code=201, dependencies=[Auth])
    def add_fact(body: FactIn, request: Request, c: Conn) -> dict[str, Any]:
        text = " ".join(body.text.split())
        try:
            vec = np.array(request.app.state.embedder.embed_one(text))
        except EmbeddingError:
            vec = None  # still stored; recalled by recency until re-embedded
        row = c.execute(
            """INSERT INTO memory.facts (user_id, project_id, text, embedding) VALUES (%s, %s, %s, %s)
               RETURNING id, project_id, text, created_at""",
            (body.user_id, body.project_id, text, vec),
        ).fetchone()
        return dict(row)  # type: ignore[arg-type]

    @app.get("/v1/facts", dependencies=[Auth])
    def list_facts(c: Conn, user_id: str, project_id: str | None = None) -> list[dict[str, Any]]:
        scope, args = ("", (user_id,)) if project_id is None else (" AND project_id = %s", (user_id, project_id))
        return [
            dict(r)
            for r in c.execute(
                "SELECT id, project_id, text, created_at FROM memory.facts WHERE user_id = %s"
                + scope
                + " ORDER BY created_at DESC",
                args,
            )
        ]

    @app.delete("/v1/facts/{fact_id}", dependencies=[Auth])
    def delete_fact(fact_id: int, c: Conn, user_id: str) -> dict[str, int]:
        n = c.execute("DELETE FROM memory.facts WHERE id = %s AND user_id = %s", (fact_id, user_id)).rowcount
        if not n:
            raise HTTPException(404, "no such fact")
        return {"deleted": n}

    @app.post("/v1/forget", dependencies=[Auth])
    def forget(body: ForgetIn, request: Request, c: Conn) -> dict[str, list[dict[str, Any]]]:
        """Delete facts and chat summaries matching a query (by meaning or by text), within one scope."""
        like = f"%{body.query}%"
        try:
            qv = np.array(request.app.state.embedder.embed_one(body.query))
        except EmbeddingError:
            qv = None
        sim = "(embedding IS NOT NULL AND 1 - (embedding <=> %(qv)s) >= %(min)s)" if qv is not None else "false"
        params = {"u": body.user_id, "p": body.project_id, "like": like, "qv": qv, "min": FORGET_MIN_SIM}
        facts = c.execute(
            f"""DELETE FROM memory.facts WHERE user_id = %(u)s AND project_id = %(p)s
                AND (text ILIKE %(like)s OR {sim}) RETURNING id, text""",
            params,
        ).fetchall()
        sums = c.execute(
            f"""DELETE FROM memory.chat_summaries WHERE user_id = %(u)s AND project_id = %(p)s
                AND (title ILIKE %(like)s OR summary ILIKE %(like)s OR {sim}) RETURNING chat_id, title, summary""",
            params,
        ).fetchall()
        return {"facts": [dict(r) for r in facts], "summaries": [dict(r) for r in sums]}

    @app.get("/v1/users/{user_id}/export", dependencies=[Auth])
    def export(
        user_id: str,
        c: Conn,
        recent: Annotated[int | None, Query(ge=1, le=1000)] = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Everything stored about a user; with project_id, only that scope ("" = outside projects)."""
        scope = "" if project_id is None else " AND project_id = %s"
        args: tuple[Any, ...] = (user_id,) if project_id is None else (user_id, project_id)
        profiles = c.execute(
            "SELECT project_id, weak_spots, mastered, updated_at FROM memory.learner_profile WHERE user_id = %s"
            + scope,
            args,
        ).fetchall()
        facts = c.execute(
            "SELECT id, project_id, text, created_at FROM memory.facts WHERE user_id = %s"
            + scope
            + " ORDER BY created_at DESC",
            args,
        ).fetchall()
        limit = "" if recent is None else f" LIMIT {int(recent)}"
        sums = c.execute(
            """SELECT chat_id, project_id, title, summary, key_points, topics, weak_spots, mastered, open_questions,
                      message_count, created_at, updated_at
               FROM memory.chat_summaries WHERE user_id = %s"""
            + scope
            + " ORDER BY updated_at DESC"
            + limit,
            args,
        ).fetchall()
        jobs = c.execute(
            """SELECT chat_id, project_id, status, attempts, first_pending_at, run_after, updated_at
               FROM memory.jobs WHERE user_id = %s"""
            + scope
            + " ORDER BY run_after",
            args,
        ).fetchall()
        main = next((p for p in profiles if p["project_id"] == (project_id or "")), None)
        return {
            "user_id": user_id,
            "project_id": project_id,
            "profile": dict(main) if main else {"weak_spots": [], "mastered": []},
            "profiles": [dict(p) for p in profiles],
            "facts": [dict(r) for r in facts],
            "summaries": [dict(r) for r in sums],
            "pending_jobs": len(jobs),
            "jobs": [dict(j) for j in jobs],
        }

    @app.delete("/v1/projects/{project_id}", dependencies=[Auth])
    def wipe_project(project_id: str, c: Conn, user_id: str | None = None) -> dict[str, int]:
        """Forget everything stored for one project (all users, or one user)."""
        if not project_id:
            raise HTTPException(400, "project_id required")
        owner, args = ("", (project_id,)) if user_id is None else (" AND user_id = %s", (project_id, user_id))
        out = {}
        for table in ("chat_summaries", "facts", "learner_profile", "jobs"):
            out[table] = c.execute(f"DELETE FROM memory.{table} WHERE project_id = %s{owner}", args).rowcount
        return out

    @app.post("/v1/admin/reconcile", dependencies=[Auth])
    def run_reconcile(request: Request) -> dict[str, int | None]:
        """Run the worker's chat-deletion sync now (normally hourly)."""
        from .worker import reconcile

        return {"removed": reconcile(request.app.state.pool, s)}

    @app.delete("/v1/users/{user_id}", dependencies=[Auth])
    def wipe_user(user_id: str, c: Conn) -> dict[str, int]:
        out = {}
        for table in ("chat_summaries", "facts", "learner_profile", "jobs"):
            out[table] = c.execute(f"DELETE FROM memory.{table} WHERE user_id = %s", (user_id,)).rowcount
        return out

    return app


def _lazy_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return create_app()


def __getattr__(name: str) -> Any:  # `uvicorn leo_memory.api:app` without building at import time in tests
    if name == "app":
        global app
        app = _lazy_app()
        return app
    raise AttributeError(name)
