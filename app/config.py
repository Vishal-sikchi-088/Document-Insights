"""Centralized application settings.

Everything the app needs to know about its environment lives here and
nowhere else — no module reaches for `os.environ` directly. That keeps
`.env.example` as the single source of truth for what's configurable,
and lets tests override settings by constructing a `Settings(...)`
instance instead of mutating process environment variables.
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "document-insights-api"
    environment: Literal["development", "production"] = "development"
    log_level: str = "INFO"

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db_name: str = "document_insights"

    redis_url: str = "redis://localhost:6379/0"
    redis_queue_key: str = "document_insights:queue:documents"

    # Max documents a single user may have in queued/processing state at once.
    rate_limit_max_active_jobs: int = 3

    # How long a completed summary stays cached against its content hash.
    cache_ttl_seconds: int = 86400

    worker_min_processing_seconds: int = 10
    worker_max_processing_seconds: int = 30
    worker_failure_rate: float = 0.1
    worker_max_retries: int = 2
    worker_retry_backoff_seconds: int = 5

    pagination_default_page_size: int = 20
    pagination_max_page_size: int = 100


@lru_cache
def get_settings() -> Settings:
    # lru_cache turns this into a process-wide singleton without the
    # boilerplate of a module-level global that has to be imported everywhere.
    return Settings()
