from datetime import UTC, datetime, timedelta

import numpy as np
from psycopg.types.json import Jsonb

from leo_memory.recall import is_trivial, recall, render, score

from .conftest import FakeEmbedder

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


def add_summary(c, emb: FakeEmbedder, chat_id, text, days_old=0, user="u1", title=None, vec=None, open_q=()):
    c.execute(
        """INSERT INTO memory.chat_summaries (chat_id, user_id, title, summary, open_questions, embedding, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            chat_id,
            user,
            title or chat_id,
            text,
            Jsonb(list(open_q)),
            np.array(vec if vec is not None else emb.vec(text)),
            NOW - timedelta(days=days_old),
        ),
    )


def test_score_formula():
    assert abs(score(1.0, 0) - 1.0) < 1e-9
    assert score(0.5, 30) < score(0.5, 0)


def test_trivial_detection():
    assert is_trivial("hi") and is_trivial("thanks!") and is_trivial("안녕하세요") and is_trivial("ok")
    assert not is_trivial("What was I struggling with last time?")


def test_ranking_threshold_exclusion_and_user_isolation(pool, embedder):
    with pool.connection() as c:
        add_summary(
            c, embedder, "c-dijk", "Dijkstra relaxation on a graph with a heap", days_old=5, open_q=["negative edges"]
        )
        add_summary(c, embedder, "c-dijk-old", "Dijkstra shortest graph relaxation practice", days_old=200)
        add_summary(c, embedder, "c-cake", "cake recipe", days_old=0)
        add_summary(c, embedder, "c-current", "Dijkstra relaxation graph heap", days_old=0)
        add_summary(c, embedder, "c-other-user", "Dijkstra relaxation graph heap", user="u2")
        r = recall(
            c, embedder, "u1", "Help me with Dijkstra relaxation on this graph", "c-current", 3, 1500, "Asia/Seoul", NOW
        )
    ids = [i.id for i in r.items if i.type == "summary"]
    assert ids[0] == "c-dijk"  # similar and recent beats similar but old
    assert "c-cake" not in ids  # below the similarity threshold
    assert "c-current" not in ids and "c-other-user" not in ids
    assert "open: negative edges." in r.block and ".." not in r.block and r.block.startswith("<leo_memory>")


def test_top_k_limit(pool, embedder):
    with pool.connection() as c:
        for i in range(6):
            add_summary(c, embedder, f"c{i}", "merge sort recurrence master theorem", days_old=i)
        r = recall(c, embedder, "u1", "solve the merge sort recurrence", None, 3, 1500, "UTC", NOW)
    assert [i.id for i in r.items if i.type == "summary"] == ["c0", "c1", "c2"]


def test_trivial_message_uses_recency_without_embedding(pool, embedder):
    with pool.connection() as c:
        add_summary(c, embedder, "old", "cake recipe", days_old=10)
        add_summary(c, embedder, "new", "probability bayes", days_old=1)
        r = recall(c, embedder, "u1", "hi", None, 1, 1500, "UTC", NOW)
    assert embedder.calls == 0
    assert [i.id for i in r.items if i.type == "summary"] == ["new"]


def test_embedding_failure_falls_back_to_recency(pool, embedder):
    embedder.fail = True
    with pool.connection() as c:
        add_summary(c, FakeEmbedder(), "a", "graph", days_old=1)
        r = recall(c, embedder, "u1", "explain Dijkstra relaxation please", None, 3, 1500, "UTC", NOW)
    assert [i.id for i in r.items if i.type == "summary"] == ["a"]


def test_facts_nearest_plus_recent_and_profile(pool, embedder):
    with pool.connection() as c:
        for i, t in enumerate(["Algorithms exam on 2026-11-12", "Prefers proofs before code", "Likes cake"]):
            c.execute(
                "INSERT INTO memory.facts (user_id, text, embedding, created_at) VALUES ('u1', %s, %s, %s)",
                (t, np.array(embedder.vec(t)), NOW - timedelta(days=10 - i)),
            )
        c.execute(
            "INSERT INTO memory.learner_profile (user_id, weak_spots, mastered) VALUES ('u1', %s, %s)",
            (
                Jsonb(
                    [
                        {"concept": "dijkstra relaxation order", "count": 3, "last_seen": NOW.isoformat()},
                        {"concept": "master theorem case 2", "count": 1, "last_seen": NOW.isoformat()},
                    ]
                ),
                Jsonb([{"concept": "big-o notation", "count": 1, "last_seen": NOW.isoformat()}]),
            ),
        )
        r = recall(c, embedder, "u1", "When is my exam again?", None, 3, 1500, "UTC", NOW)
    facts = [i.text for i in r.items if i.type == "fact"]
    assert "Algorithms exam on 2026-11-12" in facts  # nearest
    assert "Likes cake" in facts and "Prefers proofs before code" in facts  # 2 most recent
    assert "Profile — weak: dijkstra relaxation order (×3), master theorem case 2 · strong: big-o notation" in r.block
    assert "Pinned: " in r.block


def test_budget_is_respected(pool, embedder):
    with pool.connection() as c:
        for i in range(3):
            add_summary(c, embedder, f"c{i}", "Dijkstra graph relaxation " + "detail " * 70, days_old=i)
        for budget in (1500, 600, 300, 120):
            r = recall(c, embedder, "u1", "Dijkstra graph relaxation question", None, 3, budget, "UTC", NOW)
            assert len(r.block) <= budget


def test_render_priority_and_empty():
    assert render("", [], [], 1500) == ""
    block = render("Profile — weak: x", ['- 2026-09-20 "T": s.'], ["Exam soon"], 1500)
    lines = block.splitlines()
    assert lines[1].startswith("Profile") and lines[2] == "Past sessions:" and lines[-2].startswith("Pinned:")
    tiny = render("Profile — weak: x", ["- " + "s" * 500], ["Exam soon"], 80)
    assert "Past sessions" not in tiny and "Profile" in tiny


def test_past_reference_fills_with_recent_sessions(pool, embedder):
    with pool.connection() as c:
        add_summary(c, embedder, "recent", "probability bayes", days_old=1)
        add_summary(c, embedder, "older", "cake recipe", days_old=3)
        add_summary(c, embedder, "dijk", "Dijkstra relaxation graph", days_old=20)
        plain = recall(c, embedder, "u1", "Tell me something interesting please", None, 3, 1500, "UTC", NOW)
        past = recall(c, embedder, "u1", "What was I struggling with last time?", None, 3, 1500, "UTC", NOW)
        topical = recall(c, embedder, "u1", "Remember my Dijkstra relaxation issue?", None, 2, 1500, "UTC", NOW)
    assert [i.id for i in plain.items if i.type == "summary"] == []
    assert [i.id for i in past.items if i.type == "summary"] == ["recent", "older", "dijk"]
    assert [i.id for i in topical.items if i.type == "summary"] == ["dijk", "recent"]  # semantic match first


def test_refers_to_past():
    from leo_memory.recall import refers_to_past

    assert refers_to_past("What did we do last time?") and refers_to_past("우리가 지난번에 뭘 공부했지?")
    assert not refers_to_past("Explain the master theorem")
