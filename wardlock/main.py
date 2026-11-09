"""FastAPI application entry point.

Serves /health, /sessions (P3), the agent's MCP endpoint at /mcp (P6), /webhooks/paypal (P8)
and the console's /admin/api (P9).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import Engine
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from wardlock import admin, sessions, webhooks
from wardlock.config import Settings, get_settings
from wardlock.db import get_engine, init_db, make_session_factory
from wardlock.engine.policy import load_policy
from wardlock.executor import Executor
from wardlock.llm import LLMClient
from wardlock.mcp_tools import AgentServices, build_mcp_server
from wardlock.paypal.client import PayPalClient
from wardlock.reason_check import make_reason_checker

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60.0
CONSOLE_DIST = Path(__file__).resolve().parent.parent / "console" / "dist"  # built by `npm run build`


class _LateMount:
    """An ASGI app that is filled in at startup.

    The MCP app needs settings (its allowed hosts), and importing this module must read no
    settings, so the real app is only built in the lifespan. Until then: 503.
    """

    def __init__(self) -> None:
        self.app: Any = None

    async def __call__(self, scope, receive, send) -> None:
        if self.app is None:
            await PlainTextResponse("starting", status_code=503)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(
    engine: Engine | None = None,
    services: AgentServices | None = None,
    *,
    settings: Settings | None = None,
    sweep_interval: float | None = SWEEP_INTERVAL_SECONDS,
) -> FastAPI:
    """Build the app. Tests pass an in-memory engine, their own services and settings.

    Nothing touches the database, the settings or PayPal until the app starts.
    """
    mcp_mount = _LateMount()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config = settings or get_settings()
        db_engine = engine or get_engine()
        init_db(db_engine)
        app.state.session_factory = make_session_factory(db_engine)

        async with AsyncExitStack() as stack:
            agent = services or await _build_services(app.state.session_factory, config, stack)
            app.state.services = agent
            mcp = build_mcp_server(lambda: app.state.services)
            mcp_mount.app = mcp.streamable_http_app(
                streamable_http_path="/mcp",
                stateless_http=True,  # every request carries its own bearer token
                json_response=True,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=True,
                    allowed_hosts=config.mcp_allowed_hosts,
                    allowed_origins=config.mcp_allowed_origins,
                ),
            )
            # A mounted sub-app's own lifespan never runs, so start the MCP session manager here.
            await stack.enter_async_context(mcp.session_manager.run())
            if sweep_interval and agent.executor is not None:
                task = asyncio.create_task(_sweep_forever(agent.executor, sweep_interval))
                stack.callback(task.cancel)
            yield
        mcp_mount.app = None

    app = FastAPI(title="Wardlock", version="0.1.0", lifespan=lifespan)
    app.include_router(sessions.router)
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    app.add_api_route("/health", health, methods=["GET"])
    if CONSOLE_DIST.is_dir():  # the merchant console (P9); its API calls need the admin password
        app.add_api_route("/console", lambda: RedirectResponse("/console/"), methods=["GET"], include_in_schema=False)
        app.mount("/console", StaticFiles(directory=CONSOLE_DIST, html=True), name="console")
    app.mount("/", mcp_mount)  # after the routes above, so /health and /sessions match first
    return app


async def _build_services(session_factory, config: Settings, stack: AsyncExitStack) -> AgentServices:
    policy = load_policy()  # a broken policy.yaml stops startup on purpose
    try:
        paypal = await stack.enter_async_context(PayPalClient.from_settings(config))
    except ValueError as exc:  # missing credentials, or a non-sandbox URL without ALLOW_LIVE
        logger.warning("PayPal is not configured, so refund tools are disabled: %s", exc)
        return AgentServices(session_factory, policy, executor=None)
    llm = LLMClient.from_settings(config)
    if llm is None:
        logger.warning("No LLM configured: every refund the rules would approve will be HELD")
    else:
        stack.push_async_callback(llm.aclose)
    executor = Executor(session_factory, paypal, policy, make_reason_checker(llm),
                        hold_timeout=timedelta(minutes=config.hold_timeout_minutes))
    return AgentServices(session_factory, policy, executor, paypal=paypal)


async def _sweep_forever(executor: Executor, interval: float) -> None:
    """Expire stale holds and recover stuck requests, every `interval` seconds (SPEC §6)."""
    while True:
        await asyncio.sleep(interval)
        with contextlib.suppress(Exception):  # a failed sweep is retried next time
            executor.expire_stale_holds()
            executor.recover_stuck()


def health() -> dict[str, str]:
    """Liveness check for local runs and Render.

    It deliberately touches neither the database nor PayPal: it answers "is the
    process up?", not "is every dependency healthy?".
    """
    return {"status": "ok"}


app = create_app()
