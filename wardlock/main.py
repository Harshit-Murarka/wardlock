"""FastAPI application entry point.

Serves /health, /sessions (P3), the agent's MCP endpoint at /mcp (P6), /webhooks/paypal (P8)
and the console's /admin/api (P9).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import Engine
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from wardlock import admin, sessions, webhooks
from wardlock.config import Settings, get_settings
from wardlock.db import get_engine, init_db, make_session_factory
from wardlock.engine.policy import load_policy
from wardlock.executor import Executor
from wardlock.landing import landing_html, mcp_page
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
        self.browser_page: str | None = None  # what a person opening /mcp in a browser sees

    async def __call__(self, scope, receive, send) -> None:
        if self.app is None:
            await PlainTextResponse("starting", status_code=503)(scope, receive, send)
            return
        if self.browser_page and _is_browser_visit(scope):
            await HTMLResponse(self.browser_page)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _is_browser_visit(scope) -> bool:
    """A plain GET of /mcp that wants HTML. MCP clients send Accept: text/event-stream, never this."""
    if scope["type"] != "http" or scope["method"] != "GET" or scope["path"].rstrip("/") != "/mcp":
        return False
    accept = dict(scope.get("headers") or []).get(b"accept", b"").decode("latin-1").lower()
    return "text/html" in accept and "text/event-stream" not in accept


class _RejectNulInUrl:
    """400 for any URL whose path or query contains a NUL character.

    Postgres text can't hold NUL, so an id like /admin/api/holds/hold_%00 would otherwise
    turn into a database error (500). No real Wardlock URL contains one.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and ("\x00" in scope.get("path", "")
                                        or b"%00" in scope.get("query_string", b"")):
            await PlainTextResponse("bad request", status_code=400)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# Sent with every response. The console signs in with a password and can approve refunds, so it
# must never be framed by another site (clickjacking) and may only run its own scripts.
_BASE_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
]
# 'unsafe-inline' is for styles only (AG Grid and the landing page set them inline); scripts are never inline.
_CSP = (b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        b"font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        b"frame-ancestors 'none'")
# /docs and /redoc load FastAPI's API explorer from a CDN, so they only get the framing rule.
_CSP_DOCS = b"frame-ancestors 'none'"
_DOCS_PATHS = ("/docs", "/redoc")


class _SecurityHeaders:
    """Adds the headers above to every HTTP response, including errors and the mounted apps."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        csp = _CSP_DOCS if scope.get("path", "").startswith(_DOCS_PATHS) else _CSP
        extra = [*_BASE_HEADERS, (b"content-security-policy", csp)]
        if _is_https(scope):
            extra.append((b"strict-transport-security", b"max-age=31536000"))
        if _is_console_page(scope.get("path", "")):
            # Always re-check the page itself, so a new deploy shows up at once. Its scripts and
            # styles have a content hash in their names and can be cached as long as you like.
            extra.append((b"cache-control", b"no-cache"))

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _value in message.get("headers", [])}
                message = {**message, "headers": [*message.get("headers", []),
                                                  *[(n, v) for n, v in extra if n not in present]]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _is_console_page(path: str) -> bool:
    return path.startswith("/console") and not path.startswith("/console/assets/")


def _is_https(scope) -> bool:
    """True behind Render's proxy too, which ends TLS and says so in X-Forwarded-Proto."""
    if scope.get("scheme") == "https":
        return True
    forwarded = dict(scope.get("headers") or []).get(b"x-forwarded-proto", b"")
    return forwarded.split(b",")[0].strip().lower() == b"https"


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
            mcp_mount.browser_page = mcp_page(await mcp.list_tools())
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
    app.include_router(admin.public_router)
    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/", landing, methods=["GET", "HEAD"], include_in_schema=False)  # HEAD: uptime checkers
    if CONSOLE_DIST.is_dir():  # the merchant console (P9); its API calls need the admin password
        app.add_api_route("/console", lambda: RedirectResponse("/console/"), methods=["GET"], include_in_schema=False)
        app.mount("/console", StaticFiles(directory=CONSOLE_DIST, html=True), name="console")
    app.mount("/", mcp_mount)  # after the routes above, so /health and /sessions match first
    app.add_middleware(_RejectNulInUrl)
    app.add_middleware(_SecurityHeaders)  # added last, so it wraps everything, the 400 above included
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
        # Each sweep on its own: if one fails, the other still runs. A failure is retried next time.
        for sweep in (executor.expire_stale_holds, executor.recover_stuck):
            try:
                sweep()
            except Exception:
                logger.warning("sweep %s failed; it runs again in %.0f s", sweep.__name__, interval, exc_info=True)


def health() -> dict[str, str]:
    """Liveness check for local runs and Render.

    It deliberately touches neither the database nor PayPal: it answers "is the
    process up?", not "is every dependency healthy?".
    """
    return {"status": "ok"}


def landing(settings: Settings = Depends(get_settings)) -> HTMLResponse:
    """A static page saying what this server is, so the bare URL isn't a 404."""
    return HTMLResponse(landing_html(public_console=settings.console_public_readonly))


app = create_app()
