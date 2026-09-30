"""Shared fixtures. DB tests need TEST_DATABASE_URL (a throwaway pgvector database)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest

from leo_memory.db import make_pool
from leo_memory.migrate import migrate
from leo_memory.settings import Settings

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
DIM = 1024
TOKEN = "test-token"

VOCAB = [
    "dijkstra",
    "relaxation",
    "graph",
    "shortest",
    "heap",
    "master",
    "theorem",
    "recurrence",
    "merge",
    "sort",
    "probability",
    "bayes",
    "exam",
    "proof",
    "induction",
    "matrix",
    "cake",
    "recipe",
]


def unit(v: np.ndarray) -> list[float]:
    n = float(np.linalg.norm(v))
    return (v / n).tolist() if n else v.tolist()


def axis_vec(*weights: tuple[int, float]) -> list[float]:
    v = np.zeros(DIM)
    for i, w in weights:
        v[i] = w
    return unit(v)


class FakeEmbedder:
    """Bag-of-keywords embedding: related texts share axes. Deterministic."""

    dim = DIM

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def vec(self, text: str) -> list[float]:
        words = re.findall(r"[a-z]+", text.lower())
        v = np.zeros(DIM)
        v[DIM - 1] = 0.05  # tiny shared component: no zero vectors
        for w in words:
            for i, k in enumerate(VOCAB):
                if w.startswith(k):
                    v[i] += 1.0
        return unit(v)

    def embed(self, texts: list[str]) -> list[list[float]]:
        from leo_memory.embeddings import EmbeddingError

        self.calls += 1
        if self.fail:
            raise EmbeddingError("fake failure")
        return [self.vec(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class FakeLLM:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.prompts: list[list[dict[str, str]]] = []
        self.is_healthy = True
        self.is_busy = False

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any], max_tokens: int = 500) -> str:
        self.prompts.append(messages)
        if not self.responses:
            raise RuntimeError("no fake response left")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def healthy(self) -> bool:
        return self.is_healthy

    def busy(self) -> bool:
        return self.is_busy


def summary_json(**kw: Any) -> str:
    base = {
        "summary": "The user practiced Dijkstra and confused relaxation with visiting order.",
        "key_points": ["Exam on 2026-11-12"],
        "topics": ["dijkstra"],
        "weak_spots": ["dijkstra relaxation order"],
        "mastered": [],
        "open_questions": ["negative edges"],
    }
    base.update(kw)
    return json.dumps(base)


def db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    return url


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=os.environ.get("TEST_DATABASE_URL", "postgresql://unused/unused"),
        reconcile_database_url="",
        leo_memory_token=TOKEN,
        leo_migrations_dir=MIGRATIONS,
        memory_idle_seconds=120,
        memory_max_wait_seconds=900,
        memory_summary_max_words=60,
        memory_recall_top_k=3,
        memory_max_inject_chars=1500,
        memory_max_input_chars=24000,
        tz="Asia/Seoul",
    )


@pytest.fixture
def pool(settings: Settings) -> Iterator[Any]:
    url = db_url()
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS memory CASCADE")
    migrate(url, MIGRATIONS)
    p = make_pool(url, max_size=4)
    yield p
    p.close()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()
