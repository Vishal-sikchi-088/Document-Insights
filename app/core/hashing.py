"""Content hashing used as the summary cache key.

SHA-256 over a faster non-cryptographic hash (xxhash, etc.) is a deliberate
choice: it's in the stdlib (no new dependency to justify), and collision
risk is a non-issue at the scale this exercise operates at.
"""
import hashlib


def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
