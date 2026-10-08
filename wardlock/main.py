"""FastAPI application entry point.

Serves /health and /sessions (P3). Later phases mount the MCP server (P6), /webhooks (P8)
and /admin/api (P9) here.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine

from wardlock import sessions
from wardlock.db import get_engine, init_db, make_session_factory


def create_app(engine: Engine | None = None) -> FastAPI:
    """Build the app. Tests pass an in-memory engine; the server uses DATABASE_URL.

    The database is only touched when the app starts, never at import time, so
    importing this module opens no database file and reads no settings.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db_engine = engine or get_engine()
        init_db(db_engine)
        app.state.session_factory = make_session_factory(db_engine)
        yield

    app = FastAPI(title="Wardlock", version="0.1.0", lifespan=lifespan)
    app.include_router(sessions.router)
    app.add_api_route("/health", health, methods=["GET"])
    return app


def health() -> dict[str, str]:
    """Liveness check for local runs and Render.

    It deliberately touches neither the database nor PayPal: it answers "is the
    process up?", not "is every dependency healthy?".
    """
    return {"status": "ok"}


app = create_app()
