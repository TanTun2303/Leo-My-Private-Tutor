from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from leo_memory.api import create_app

from .conftest import TOKEN

H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(settings, pool, embedder):
    with TestClient(create_app(settings, embedder=embedder, pool=pool)) as c:
        yield c


def turn(chat_id="c1", user="u1", n=2, **kw):
    msgs = []
    for i in range(n):
        msgs.append({"id": f"{chat_id}-u{i}", "role": "user", "content": f"question {i} about Dijkstra"})
        msgs.append({"id": f"{chat_id}-a{i}", "role": "assistant", "content": f"answer {i}"})
    return {"chat_id": chat_id, "user_id": user, "title": "Dijkstra practice", "messages": msgs, **kw}


def jobs(pool):
    with pool.connection() as c:
        return c.execute("SELECT * FROM memory.jobs ORDER BY chat_id").fetchall()


def test_health_needs_no_auth(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_auth_required(client, headers):
    assert client.post("/v1/events/turn", json=turn(), headers=headers).status_code == 401
    assert client.get("/v1/recall", params={"user_id": "u1"}, headers=headers).status_code == 401
    assert client.delete("/v1/users/u1", headers=headers).status_code == 401


def test_turn_queues_sanitized_job(client, pool):
    body = turn()
    body["messages"].insert(0, {"role": "system", "content": "SYSTEM PROMPT"})
    body["messages"][-1]["content"] = "<think>hidden</think>visible answer"
    r = client.post("/v1/events/turn", json=body, headers=H)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    (job,) = jobs(pool)
    texts = [m["content"] for m in job["payload"]["messages"]]
    assert "SYSTEM PROMPT" not in texts and "visible answer" in texts and not any("hidden" in t for t in texts)
    assert job["status"] == "pending"
    idle = (job["run_after"] - job["updated_at"]).total_seconds()
    assert 119 <= idle <= 121


@pytest.mark.parametrize("chat_id", [None, "local:abc", "temporary:abc", "channel:abc"])
def test_temporary_chats_skipped(client, pool, chat_id):
    r = client.post("/v1/events/turn", json=turn(chat_id=chat_id), headers=H)
    assert r.json()["status"].startswith("skipped") and jobs(pool) == []


def test_memory_disabled_skipped(client, pool):
    r = client.post("/v1/events/turn", json=turn(memory_enabled=False), headers=H)
    assert r.json()["status"] == "skipped: memory disabled" and jobs(pool) == []


def test_debounce_twenty_turns_one_job_capped_by_max_wait(client, pool):
    for i in range(1, 21):
        assert client.post("/v1/events/turn", json=turn(n=i), headers=H).status_code == 202
    (job,) = jobs(pool)
    assert len([m for m in job["payload"]["messages"] if m["role"] == "user"]) == 20
    assert job["run_after"] <= job["first_pending_at"] + timedelta(seconds=900)
    # Simulate a chat that has been busy for 850 s: the next turn must not push run_after past max-wait.
    with pool.connection() as c:
        c.execute("UPDATE memory.jobs SET first_pending_at = first_pending_at - interval '850 seconds'")
    client.post("/v1/events/turn", json=turn(n=21), headers=H)
    (job,) = jobs(pool)
    wait = (job["run_after"] - job["first_pending_at"]).total_seconds()
    assert 899 <= wait <= 901


def test_only_new_messages_after_summary(client, pool):
    with pool.connection() as c:
        c.execute("""INSERT INTO memory.chat_summaries (chat_id, user_id, summary, last_message_id)
                     VALUES ('c1', 'u1', 'old', 'c1-a1')""")
    client.post("/v1/events/turn", json=turn(n=3), headers=H)
    (job,) = jobs(pool)
    assert [m["id"] for m in job["payload"]["messages"]] == ["c1-u2", "c1-a2"]


def test_chat_of_other_user_rejected(client, pool):
    with pool.connection() as c:
        c.execute("INSERT INTO memory.chat_summaries (chat_id, user_id, summary) VALUES ('c1', 'u2', 's')")
    assert "another user" in client.post("/v1/events/turn", json=turn(), headers=H).json()["status"]


def test_facts_crud_forget_export_and_wipe(client, pool):
    r = client.post("/v1/facts", json={"user_id": "u1", "text": "Algorithms exam on 2026-11-12"}, headers=H)
    assert r.status_code == 201
    fid = r.json()["id"]
    client.post("/v1/facts", json={"user_id": "u1", "text": "Likes cake"}, headers=H)
    assert client.post("/v1/facts", json={"user_id": "u1", "text": "x" * 301}, headers=H).status_code == 422
    assert len(client.get("/v1/facts", params={"user_id": "u1"}, headers=H).json()) == 2
    assert client.delete(f"/v1/facts/{fid}", params={"user_id": "u2"}, headers=H).status_code == 404
    assert client.delete(f"/v1/facts/{fid}", params={"user_id": "u1"}, headers=H).status_code == 200

    with pool.connection() as c:
        c.execute(
            "INSERT INTO memory.chat_summaries (chat_id, user_id, title, summary) VALUES "
            "('c1','u1','Cake chat','The user asked about cake'), ('c2','u1','Graphs','Dijkstra')"
        )
    out = client.post("/v1/forget", json={"user_id": "u1", "query": "cake"}, headers=H).json()
    assert [f["text"] for f in out["facts"]] == ["Likes cake"] and [s["chat_id"] for s in out["summaries"]] == ["c1"]

    exp = client.get("/v1/users/u1/export", headers=H).json()
    assert [s["chat_id"] for s in exp["summaries"]] == ["c2"] and exp["facts"] == []
    assert "embedding" not in exp["summaries"][0]

    client.post("/v1/events/turn", json=turn(chat_id="c3"), headers=H)
    wiped = client.delete("/v1/users/u1", headers=H).json()
    assert wiped["chat_summaries"] == 1 and wiped["jobs"] == 1
    assert client.get("/v1/users/u1/export", headers=H).json()["summaries"] == []


def test_forget_chat_and_summary_route(client, pool):
    with pool.connection() as c:
        c.execute("INSERT INTO memory.chat_summaries (chat_id, user_id, summary) VALUES ('c1', 'u1', 's')")
    assert client.get("/v1/chats/c1/summary", headers=H).json()["summary"] == "s"
    assert client.delete("/v1/chats/c1", headers=H).json()["summaries"] == 1
    assert client.get("/v1/chats/c1/summary", headers=H).status_code == 404


def test_recall_route(client, pool, embedder):
    import numpy as np

    with pool.connection() as c:
        c.execute(
            "INSERT INTO memory.chat_summaries (chat_id, user_id, title, summary, embedding) VALUES "
            "('c1','u1','Dijkstra practice','The user practiced Dijkstra relaxation', %s)",
            (np.array(embedder.vec("Dijkstra relaxation")),),
        )
    r = client.get("/v1/recall", params={"user_id": "u1", "q": "Dijkstra relaxation again?"}, headers=H).json()
    assert '"Dijkstra practice"' in r["block"] and len(r["block"]) <= 1500


def test_admin_reconcile_route(client):
    assert client.post("/v1/admin/reconcile").status_code == 401
    assert client.post("/v1/admin/reconcile", headers=H).json() == {"removed": None}  # no reconcile DB configured
