"""Client for leo-embed (llama-server --embedding, OpenAI-compatible /v1/embeddings)."""

from __future__ import annotations

import httpx


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    def __init__(self, base_url: str, api_key: str, dim: int, transport: httpx.BaseTransport | None = None) -> None:
        self.dim = dim
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=httpx.Timeout(30.0, connect=5.0),
            transport=transport,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            r = self._http.post(f"{self.base_url}/embeddings", json={"model": "leo-embed", "input": texts})
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise EmbeddingError(str(e)) from e
        vecs = [d["embedding"] for d in data]
        if len(vecs) != len(texts) or any(len(v) != self.dim for v in vecs):
            raise EmbeddingError(f"expected {len(texts)} vectors of dim {self.dim}")
        return vecs

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def close(self) -> None:
        self._http.close()
