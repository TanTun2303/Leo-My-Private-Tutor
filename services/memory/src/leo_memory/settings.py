"""Runtime configuration, read from environment variables (see compose.yaml)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    reconcile_database_url: str = ""
    leo_memory_token: str

    llm_base_url: str = "http://leo-llm:8080/v1"
    llm_api_key: str = ""
    llm_model: str = "leo"
    embed_base_url: str = "http://leo-embed:8080/v1"
    embed_api_key: str = ""
    embed_dim: int = 1024

    memory_idle_seconds: int = 120
    memory_max_wait_seconds: int = 900
    memory_summary_max_words: int = 60
    memory_recall_top_k: int = 3
    memory_max_inject_chars: int = 1500
    memory_max_input_chars: int = 24000
    memory_retention_days: int = 0

    tz: str = "UTC"
    leo_migrations_dir: Path = Path(__file__).resolve().parents[2] / "migrations"

    # Worker tuning (not in .env; defaults follow the spec).
    worker_poll_seconds: float = 5.0
    worker_busy_requeue_seconds: int = 30
    worker_max_attempts: int = 5
    worker_backoff_cap_seconds: int = 1800
    worker_stale_running_seconds: int = 1800


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
