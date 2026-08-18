"""Domain enums shared across the models, repository, and service layers."""
from enum import Enum


class DocumentStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def active(cls) -> tuple["DocumentStatus", ...]:
        """Statuses that count against a user's concurrent-job rate limit.

        Defined here, next to the enum itself, rather than hardcoded inside
        the rate limiter — so the two can never quietly drift apart if a
        status is ever added.
        """
        return (cls.QUEUED, cls.PROCESSING)
