"""FastAPI application factory and entry point.

Run with:
    uvicorn main:app --host 127.0.0.1 --port 8787 --reload
or:
    python main.py
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.ratelimit import RateLimiter
from api.routes import register_exception_handlers, router
from config import get_settings
from opencode.client import OpenCodeClient


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise shared state once per process."""
    settings = get_settings()
    _configure_logging(settings.gateway_log_level)
    log = logging.getLogger("gateway.main")

    app.state.settings = settings
    app.state.client = OpenCodeClient(settings)
    app.state.limiter = RateLimiter(settings)

    log.info(
        "Hermes ↔ OpenCode gateway starting on %s:%s (auth=%s, rpm=%d)",
        settings.gateway_host,
        settings.gateway_port,
        "on" if settings.auth_enabled else "off",
        settings.gateway_rate_limit_rpm,
    )
    try:
        bin_path = app.state.client.resolve_binary()
        log.info("opencode binary resolved to %s", bin_path)
    except Exception as e:  # noqa: BLE001
        log.warning("could not resolve opencode binary at startup: %s", e)

    yield
    log.info("gateway shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Hermes ↔ OpenCode Gateway",
        description=(
            "An OpenAI-compatible API bridge that translates requests into "
            "`opencode` CLI invocations, exposing free local models to any "
            "OpenAI-compatible client (such as Hermes Agent)."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    register_exception_handlers(app)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.gateway_host,
        port=settings.gateway_port,
        log_level=settings.gateway_log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
