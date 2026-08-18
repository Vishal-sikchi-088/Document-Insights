"""Centralized Redis key naming.

Every service builds its keys through here rather than inlining format
strings — otherwise it's easy for two call sites to construct a subtly
different key for what's meant to be the same logical entity (e.g. one
site forgetting the prefix) and silently miss each other.
"""

_PREFIX = "document_insights"


def rate_limit_key(user_id: str) -> str:
    return f"{_PREFIX}:ratelimit:user:{user_id}:active"


def cache_key(content_hash: str) -> str:
    return f"{_PREFIX}:cache:content:{content_hash}"


def lock_key(content_hash: str) -> str:
    return f"{_PREFIX}:lock:content:{content_hash}"


def waiters_key(content_hash: str) -> str:
    return f"{_PREFIX}:waiters:content:{content_hash}"
