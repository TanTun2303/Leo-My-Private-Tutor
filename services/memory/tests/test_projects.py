"""Projects (Open WebUI folders): memory is isolated per project; '' is the global scope."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from leo_memory import worker
from leo_memory.api import create_app

from .conftest import TOKEN, FakeLLM, db_url, summary_json

H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(settings, pool, embedder):
    with TestClient(create_app(settings, embedder=embedder, pool=pool)) as c:
        yield c


def turn(client, chat_id, project_id="", text="Dijkstra relaxation question"):
    r = client.post(
        "/v1/events/turn",
        headers=H,
        json={
            "chat_id": chat_id,
            "user_id": "u1",
            "title": chat_id,
            "project_id": project_id,
            "messages": [
                {"id": f"{chat_id}-u", "role": "user", "content": text},
                {"id": f"{chat_id}-a", "role": "assistant", "content": "answer"},
            ],
        },
    )
    assert r.status_code == 202


def run_due(pool, embedder, settings, llm):
    with pool.connection() as c:
        c.execute("UPDATE memory.jobs SET run_after = clock_timestamp() - interval '1 second'")
    while worker.run_one(pool, llm, embedder, settings) is not None:
        pass


def recall(client, q, project_id=""):
    return client.get("/v1/recall", headers=H, params={"user_id": "u1", "q": q, "project_id": project_id}).json()


def test_summaries_facts_and_profile_are_scoped(client, pool, embedder, settings):
    turn(client, "global-chat")
    turn(client, "alpha-chat", project_id="alpha")
    llm = FakeLLM([summary_json(weak_spots=["global weakness"]), summary_json(weak_spots=["alpha weakness"])])
    run_due(pool, embedder, settings, llm)
    with pool.connection() as c:
        rows = {
            r["chat_id"]: r["project_id"] for r in c.execute("SELECT chat_id, project_id FROM memory.chat_summaries")
        }
        profiles = {
            r["project_id"]: r["weak_spots"][0]["concept"]
            for r in c.execute("SELECT project_id, weak_spots FROM memory.learner_profile")
        }
    assert rows == {"global-chat": "", "alpha-chat": "alpha"}
    assert profiles == {"": "global weakness", "alpha": "alpha weakness"}

    client.post("/v1/facts", headers=H, json={"user_id": "u1", "text": "Alpha exam is Friday", "project_id": "alpha"})
    client.post("/v1/facts", headers=H, json={"user_id": "u1", "text": "Global note about graphs"})

    g = recall(client, "Dijkstra relaxation again?")
    a = recall(client, "Dijkstra relaxation again?", "alpha")
    b = recall(client, "Dijkstra relaxation again?", "beta")
    ids = lambda r, t: {i["id"] for i in r["items"] if i["type"] == t}  # noqa: E731
    assert ids(g, "summary") == {"global-chat"} and "alpha" not in g["block"].lower()
    assert ids(a, "summary") == {"alpha-chat"} and "Alpha exam is Friday" in a["block"]
    assert "Global note" not in a["block"] and "global weakness" not in a["block"]
    assert "alpha weakness" in a["block"]
    assert b["block"] == "" and b["items"] == []


def test_chat_moved_into_project_follows_it(client, pool, embedder, settings):
    turn(client, "c1")
    run_due(pool, embedder, settings, FakeLLM([summary_json()]))
    with pool.connection() as c:
        c.execute("UPDATE memory.chat_summaries SET last_message_id = NULL")
    turn(client, "c1", project_id="alpha", text="more Dijkstra")
    run_due(pool, embedder, settings, FakeLLM([summary_json()]))
    with pool.connection() as c:
        assert (
            c.execute("SELECT project_id FROM memory.chat_summaries WHERE chat_id='c1'").fetchone()["project_id"]
            == "alpha"
        )


def test_forget_and_export_respect_scope(client, pool):
    client.post("/v1/facts", headers=H, json={"user_id": "u1", "text": "cake in alpha", "project_id": "alpha"})
    client.post("/v1/facts", headers=H, json={"user_id": "u1", "text": "cake globally"})
    out = client.post("/v1/forget", headers=H, json={"user_id": "u1", "query": "cake", "project_id": "alpha"}).json()
    assert [f["text"] for f in out["facts"]] == ["cake in alpha"]
    everything = client.get("/v1/users/u1/export", headers=H).json()
    alpha = client.get("/v1/users/u1/export", headers=H, params={"project_id": "alpha"}).json()
    assert [f["text"] for f in everything["facts"]] == ["cake globally"] and alpha["facts"] == []


def test_wipe_project(client, pool):
    client.post("/v1/facts", headers=H, json={"user_id": "u1", "text": "alpha fact", "project_id": "alpha"})
    client.post("/v1/facts", headers=H, json={"user_id": "u1", "text": "global fact"})
    assert client.delete("/v1/projects/alpha", headers=H).json()["facts"] == 1
    assert client.delete("/v1/projects/", headers=H).status_code in (404, 405)
    assert [f["text"] for f in client.get("/v1/facts", headers=H, params={"user_id": "u1"}).json()] == ["global fact"]


def test_reconcile_removes_deleted_projects(client, pool, settings):
    url = db_url()
    with psycopg.connect(url, autocommit=True) as c:  # stand-ins for Open WebUI's tables
        c.execute("DROP TABLE IF EXISTS public.chat, public.folder")
        c.execute("CREATE TABLE public.chat (id text PRIMARY KEY)")
        c.execute("CREATE TABLE public.folder (id text PRIMARY KEY)")
        c.execute("INSERT INTO public.folder VALUES ('kept')")
    for pid in ("kept", "deleted"):
        client.post("/v1/facts", headers=H, json={"user_id": "u1", "text": f"{pid} fact", "project_id": pid})
    client.post("/v1/facts", headers=H, json={"user_id": "u1", "text": "global fact"})
    settings.reconcile_database_url = url
    assert worker.reconcile(pool, settings) == 1
    facts = {f["text"] for f in client.get("/v1/facts", headers=H, params={"user_id": "u1"}).json()}
    assert facts == {"kept fact", "global fact"}
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP TABLE public.chat, public.folder")
