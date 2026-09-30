from datetime import UTC, datetime, timedelta

from leo_memory.profile import MAX_ITEMS, merge_into_db, merge_profile, normalize, rank

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def test_normalize():
    assert normalize("  Master   Theorems. ") == "master theorem"
    assert normalize("Binary Heaps") == "binary heap"
    assert normalize("dynamic programming strategies") == "dynamic programming strategy"
    assert normalize("big-O analysis") == "big-o analysis"
    assert normalize("class") == "class"


def test_weak_spot_counts_and_last_seen():
    w, m = merge_profile([], [], ["Dijkstra relaxation order"], [], NOW)
    w, m = merge_profile(w, m, ["dijkstra relaxation order", "master theorem"], [], NOW)
    by = {i["concept"]: i for i in w}
    assert by["dijkstra relaxation order"]["count"] == 2
    assert by["master theorem"]["last_seen"] == NOW.isoformat()


def test_mastered_decrements_and_removes_weak():
    w, m = merge_profile([], [], ["recursion", "recursion"], [], NOW)
    w, m = merge_profile(w, m, [], ["Recursion"], NOW)
    assert w[0]["count"] == 1 and m[0]["concept"] == "recursion"
    w, m = merge_profile(w, m, [], ["recursion"], NOW)
    assert w == [] and m[0]["count"] == 2


def test_rank_decay_and_cap():
    old = {"concept": "old", "count": 4, "last_seen": (NOW - timedelta(days=60)).isoformat()}
    new = {"concept": "new", "count": 2, "last_seen": NOW.isoformat()}
    assert rank(old, NOW) == 1.0 and rank(new, NOW) == 2.0
    many = [f"concept {i:02d}" for i in range(40)]
    w, _ = merge_profile([old], [], many, [], NOW)
    assert len(w) == MAX_ITEMS and all(i["concept"] != "old" for i in w)


def test_merge_into_db(pool):
    with pool.connection() as c:
        merge_into_db(c, "u1", ["heaps"], [])
        merge_into_db(c, "u1", ["heap"], ["graphs"])
    with pool.connection() as c:
        row = c.execute("SELECT weak_spots, mastered FROM memory.learner_profile WHERE user_id='u1'").fetchone()
    assert row["weak_spots"][0] == {**row["weak_spots"][0], "concept": "heap", "count": 2}
    assert row["mastered"][0]["concept"] == "graph"
