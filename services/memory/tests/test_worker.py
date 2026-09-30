from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from leo_memory import worker
from leo_memory.api import create_app

from .conftest import TOKEN, FakeLLM, summary_json

H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(settings, pool, embedder):
    with TestClient(create_app(settings, embedder=embedder, pool=pool)) as c:
        yield c


def post_turn(client, n, chat_id="c1", user="u1"):
    msgs = []
    for i in range(n):
        msgs += [
            {"id": f"u{i}", "role": "user", "content": f"Dijkstra question {i}"},
            {"id": f"a{i}", "role": "assistant", "content": f"answer {i}"},
        ]
    r = client.post(
        "/v1/events/turn",
        json={"chat_id": chat_id, "user_id": user, "title": "Dijkstra practice", "messages": msgs},
        headers=H,
    )
    assert r.status_code == 202


def make_due(pool, chat_id="c1"):
    with pool.connection() as c:
        c.execute(
            "UPDATE memory.jobs SET run_after = clock_timestamp() - interval '1 second' WHERE chat_id = %s", (chat_id,)
        )


def job_row(pool, chat_id="c1"):
    with pool.connection() as c:
        return c.execute("SELECT * FROM memory.jobs WHERE chat_id = %s", (chat_id,)).fetchone()


def summary_row(pool, chat_id="c1"):
    with pool.connection() as c:
        return c.execute("SELECT * FROM memory.chat_summaries WHERE chat_id = %s", (chat_id,)).fetchone()


def test_not_due_yet(client, pool, embedder, settings):
    post_turn(client, 1)
    assert worker.run_one(pool, FakeLLM([summary_json()]), embedder, settings) is None


def test_end_to_end_summary_profile_and_job_deleted(client, pool, embedder, settings):
    post_turn(client, 2)
    make_due(pool)
    llm = FakeLLM([summary_json()])
    assert worker.run_one(pool, llm, embedder, settings) == "done"
    s = summary_row(pool)
    assert len(s["summary"].split()) <= 60 and s["topics"] == ["dijkstra"] and s["last_message_id"] == "a1"
    assert s["message_count"] == 4 and s["embedding"] is not None
    assert job_row(pool) is None
    with pool.connection() as c:
        prof = c.execute("SELECT weak_spots FROM memory.learner_profile WHERE user_id='u1'").fetchone()
    assert prof["weak_spots"][0]["concept"] == "dijkstra relaxation order"

    # Next turn only carries the new messages, and the summarizer gets the previous summary to merge.
    post_turn(client, 3)
    assert [m["id"] for m in job_row(pool)["payload"]["messages"]] == ["u2", "a2"]
    make_due(pool)
    llm2 = FakeLLM([summary_json(summary="Merged summary.")])
    assert worker.run_one(pool, llm2, embedder, settings) == "done"
    assert '"previous_summary": {"summary": "The user practiced Dijkstra' in llm2.prompts[0][1]["content"]
    assert summary_row(pool)["message_count"] == 6


def test_claim_delete_race_newer_turn_keeps_job_pending(client, pool, embedder, settings):
    post_turn(client, 1)
    make_due(pool)
    job = worker.claim(pool)
    assert job_row(pool)["status"] == "running"
    post_turn(client, 2)  # a newer turn arrives while the LLM call is in flight
    assert worker.process(pool, FakeLLM([summary_json()]), embedder, settings, job) == "done-newer-pending"
    row = job_row(pool)
    assert row is not None and row["status"] == "pending" and row["attempts"] == 1
    # The re-queued job skips what was already summarized.
    make_due(pool)
    llm = FakeLLM([summary_json()])
    assert worker.run_one(pool, llm, embedder, settings) == "done"
    assert '"Dijkstra question 1"' in llm.prompts[0][1]["content"]
    assert '"Dijkstra question 0"' not in llm.prompts[0][1]["content"]


def test_busy_llm_requeues_until_max_wait(client, pool, embedder, settings):
    post_turn(client, 1)
    make_due(pool)
    llm = FakeLLM([summary_json()])
    llm.is_busy = True
    assert worker.run_one(pool, llm, embedder, settings) == "llm-busy"
    row = job_row(pool)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert 25 <= (row["run_after"] - row["updated_at"]).total_seconds() <= 31
    # Past max-wait it runs anyway (the second llama.cpp slot serves background work).
    with pool.connection() as c:
        c.execute("UPDATE memory.jobs SET first_pending_at = first_pending_at - interval '1000 seconds'")
    make_due(pool)
    assert worker.run_one(pool, llm, embedder, settings) == "done"


def test_unhealthy_llm_requeues(client, pool, embedder, settings):
    post_turn(client, 1)
    make_due(pool)
    llm = FakeLLM()
    llm.is_healthy = False
    assert worker.run_one(pool, llm, embedder, settings) == "llm-unhealthy"
    assert job_row(pool)["status"] == "pending"


def test_failures_back_off_then_die(client, pool, embedder, settings):
    post_turn(client, 1)
    llm = FakeLLM(["bad"] * 20)
    delays = []
    for attempt in range(1, settings.worker_max_attempts + 1):
        make_due(pool)
        outcome = worker.run_one(pool, llm, embedder, settings)
        row = job_row(pool)
        if attempt < settings.worker_max_attempts:
            assert outcome == "retry" and row["attempts"] == attempt and row["last_error"]
            delays.append(round((row["run_after"] - row["updated_at"]).total_seconds()))
        else:
            assert outcome == "dead" and row["status"] == "dead"
    assert delays == [30, 60, 120, 240]
    assert worker.backoff_seconds(20, 1800) == 1800
    # A new turn revives a dead job with a fresh attempt budget.
    post_turn(client, 2)
    row = job_row(pool)
    assert row["status"] == "pending" and row["attempts"] == 0 and row["last_error"] is None


def test_stress_twenty_turns_single_run(client, pool, embedder, settings):
    for n in range(1, 21):
        post_turn(client, n)
    with pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM memory.jobs").fetchone()["n"] == 1
    row = job_row(pool)
    assert row["run_after"] <= row["first_pending_at"] + timedelta(seconds=settings.memory_max_wait_seconds)
    make_due(pool)
    llm = FakeLLM([summary_json()])
    assert worker.run_one(pool, llm, embedder, settings) == "done"
    assert worker.run_one(pool, llm, embedder, settings) is None
    assert len(llm.prompts) == 1


def test_stale_running_reset_and_retention(client, pool, embedder, settings):
    post_turn(client, 1)
    make_due(pool)
    worker.claim(pool)
    with pool.connection() as c:
        c.execute("UPDATE memory.jobs SET updated_at = now() - interval '2 hours'")
        c.execute(
            "INSERT INTO memory.chat_summaries (chat_id, user_id, summary, updated_at) "
            "VALUES ('old', 'u1', 's', now() - interval '40 days')"
        )
    assert worker.reset_stale(pool, settings) == 1
    assert worker.purge_retention(pool, settings) == 0  # retention 0 = keep forever
    settings.memory_retention_days = 30
    assert worker.purge_retention(pool, settings) == 1


def test_reconcile_skipped_without_access(pool, settings):
    assert worker.reconcile(pool, settings) is None
    settings.reconcile_database_url = "postgresql://nobody:nothing@127.0.0.1:1/none"
    assert worker.reconcile(pool, settings) is None


def test_reconcile_removes_summaries_and_jobs_of_deleted_chats(client, pool, settings):
    import psycopg

    from .conftest import db_url

    url = db_url()
    with psycopg.connect(url, autocommit=True) as c:  # stand-in for Open WebUI's chat table
        c.execute("DROP TABLE IF EXISTS public.chat")
        c.execute("CREATE TABLE public.chat (id text PRIMARY KEY)")
        c.execute("INSERT INTO public.chat VALUES ('alive')")
    with pool.connection() as c:
        c.execute(
            "INSERT INTO memory.chat_summaries (chat_id, user_id, summary) VALUES ('alive','u1','s'), ('gone','u1','s')"
        )
    post_turn(client, 1, chat_id="gone-pending")
    settings.reconcile_database_url = url
    assert worker.reconcile(pool, settings) == 2
    with pool.connection() as c:
        left = {
            r["chat_id"]
            for r in c.execute("SELECT chat_id FROM memory.chat_summaries UNION SELECT chat_id FROM memory.jobs")
        }
    assert left == {"alive"}
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP TABLE public.chat")
