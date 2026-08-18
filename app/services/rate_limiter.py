"""Per-user concurrent-job rate limiting.

Backed by a Redis *set* of active document ids per user (`SADD` on
acquire, `SREM` on release) rather than a plain integer counter. A
counter only stays correct if every increment is matched by exactly one
decrement; a set is idempotent by construction — releasing a document_id
that was never acquired, or releasing it twice (e.g. a retried release
after a transient error), can't push the count negative or leave it
stuck above the true active count the way a mismatched counter could.
"""
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.redis_keys import rate_limit_key

logger = logging.getLogger(__name__)

# A plain SCARD followed by a separate SADD leaves a window where two
# concurrent requests both read a count under the limit before either
# adds its member, letting a user slip past the cap. Redis runs scripts
# single-threaded, so wrapping the check and the mutation in one script
# makes them atomic — no separate distributed lock required.
_TRY_ACQUIRE_SCRIPT = """
local active_count = redis.call('SCARD', KEYS[1])
if active_count >= tonumber(ARGV[2]) then
    return 0
end
redis.call('SADD', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""


class RateLimiterService:
    def __init__(self, client: Redis, *, max_active_jobs: int, entry_ttl_seconds: int):
        self._client = client
        self._max_active_jobs = max_active_jobs
        self._entry_ttl_seconds = entry_ttl_seconds
        self._try_acquire_script = client.register_script(_TRY_ACQUIRE_SCRIPT)

    async def try_acquire(self, user_id: str, document_id: str) -> bool:
        """Reserve a rate-limit slot for `document_id`.

        Returns False if the user already has `max_active_jobs` documents
        queued or processing. Fails *open* on a Redis outage: rate
        limiting is a protective measure, not a correctness guarantee, so
        an unreachable Redis is logged and the submission is allowed
        through rather than rejecting every request because a
        non-critical dependency is down.

        The set's TTL (refreshed on every acquire) is a safety net, not
        the primary release mechanism — `release()` is expected to fire
        on every terminal job state. It exists purely so a worker crash
        that skips the release can't lock a user out indefinitely.
        """
        try:
            result = await self._try_acquire_script(
                keys=[rate_limit_key(user_id)],
                args=[document_id, self._max_active_jobs, self._entry_ttl_seconds],
            )
            return bool(result)
        except RedisError:
            logger.warning("rate limiter unavailable, failing open", extra={"user_id": user_id})
            return True

    async def release(self, user_id: str, document_id: str) -> None:
        """Free a user's rate-limit slot once a job reaches a terminal
        state (completed/failed). Safe to call on a slot that was never
        acquired (the fail-open path above) or already released — `SREM`
        is a no-op either way.
        """
        try:
            await self._client.srem(rate_limit_key(user_id), document_id)
        except RedisError:
            logger.warning(
                "rate limiter unavailable while releasing slot; "
                "entry will self-heal via TTL instead",
                extra={"user_id": user_id},
            )
