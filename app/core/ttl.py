"""Shared TTL math for Redis safety-net expirations.

Lives outside `app/core/dependencies.py` (a FastAPI-specific module) so
the worker process — which builds its own RateLimiterService/CacheService
instances outside of any `Depends()` graph — can use the exact same
formula instead of duplicating or drifting from it.
"""
from app.config import Settings


def compute_worst_case_job_lifetime_seconds(settings: Settings) -> int:
    """Upper bound on how long a single document might stay in-flight
    across every retry attempt (processing time plus doubling backoff).

    Used as the TTL safety net for the rate-limit entry and the cache
    processing lock: long enough that it never expires out from under a
    job that's still legitimately running, short enough to self-heal
    promptly if a worker crashes without releasing either one.
    """
    attempts = settings.worker_max_retries + 1
    total_processing_time = attempts * settings.worker_max_processing_seconds
    total_backoff_time = sum(
        settings.worker_retry_backoff_seconds * (2**attempt) for attempt in range(settings.worker_max_retries)
    )
    safety_buffer_seconds = 60
    return total_processing_time + total_backoff_time + safety_buffer_seconds
