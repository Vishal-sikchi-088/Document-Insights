"""Application entrypoint and composition root.

This is the only module that wires concrete infrastructure (Mongo/Redis
clients) into the app. Everything downstream — routers, services,
repositories — receives what it needs via `Depends()` and never imports a
client directly, so swapping infrastructure (e.g. for tests) never touches
route or business logic.

CORS and other browser-facing security headers are intentionally omitted:
this API has no browser client in scope for this exercise, so a permissive
CORS policy would be an unused attack surface and a restrictive one would
need an origin list nobody has specified yet. Worth revisiting the moment
an actual frontend origin exists.
"""
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.config import get_settings
from app.db.mongo import create_indexes, create_mongo_client
from app.db.redis import create_redis_client
from app.logging_config import configure_logging
from app.routers import health

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

    app.include_router(health.router)

    return app


app = create_app()
