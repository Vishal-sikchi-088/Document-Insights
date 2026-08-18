"""Application entrypoint and composition root.

This is the only module that wires concrete infrastructure (Mongo/Redis
clients) into the app. Everything downstream — routers, services,
repositories — receives what it needs via `Depends()` and never imports a
client directly, so swapping infrastructure (e.g. for tests) never touches
route or business logic.

CORS is off by default for the same reason it was originally omitted
entirely: this API has no browser client in production scope, so a
permissive policy would be unused attack surface. The one exception is
`tools/api_tester.html` (see that file) — a local, file-opened manual
testing page — which needs *some* CORS allowance to reach this API from
a `file://` origin at all. That allowance is gated to
`ENVIRONMENT=development` so it never applies to a real deployment.
"""
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.error_handlers import register_exception_handlers
from app.db.mongo import create_indexes, create_mongo_client
from app.db.redis import create_redis_client
from app.logging_config import configure_logging
from app.routers import documents, health, users

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    mongo_client = create_mongo_client(settings.mongodb_uri)
    app.state.mongo_client = mongo_client
    app.state.db = mongo_client[settings.mongodb_db_name]
    await create_indexes(app.state.db)

    app.state.redis = create_redis_client(settings.redis_url)

    logger.info("startup complete", extra={"environment": settings.environment})
    try:
        yield
    finally:
        mongo_client.close()
        await app.state.redis.aclose()
        logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Document Insights API",
        description="Accepts documents, processes them asynchronously, and returns structured summaries.",
        version="0.1.0",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    if settings.environment == "development":
        # `allow_origins=["*"]` is safe here specifically because this
        # branch never runs in production and nothing on this API relies
        # on cookies/credentials — there's no session to leak cross-site.
        # A `file://`-opened page sends `Origin: null`, which a specific
        # origin allowlist can't match reliably across browsers, so a
        # wildcard is actually the more robust choice for this use case,
        # not a lazier one.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(users.router)

    return app


app = create_app()
