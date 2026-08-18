"""Central mapping from domain exceptions to HTTP responses.

Registered once against the app in `app/main.py`. Keeping the mapping
here means "rate limit exceeded is a 429" is decided in exactly one
place, not re-decided by whichever router happens to raise it.
"""
import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.core.exceptions import DocumentNotFoundError, RateLimitExceededError

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DocumentNotFoundError)
    async def handle_not_found(request: Request, exc: DocumentNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.exception_handler(RateLimitExceededError)
    async def handle_rate_limit_exceeded(request: Request, exc: RateLimitExceededError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_429_TOO_MANY_REQUESTS, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Last line of defense: anything reaching here is a bug, not an
        # expected domain condition, so it's logged with a full traceback
        # and turned into an opaque 500 rather than leaking internals to
        # the client or, worse, being swallowed somewhere upstream.
        logger.exception("unhandled exception", extra={"path": request.url.path})
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "internal server error"},
        )
