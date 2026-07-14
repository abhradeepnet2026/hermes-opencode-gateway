"""FastAPI routes implementing the OpenAI-compatible API surface.

Endpoints
---------
- GET  /health                       — liveness probe (no auth)
- GET  /v1/models                    — list opencode-discovered models
- POST /v1/chat/completions          — create a chat completion (stream or not)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api import (
    AuthenticationError,
    BadRequestError,
    GatewayError,
    InternalError,
    NotFoundError,
    RateLimitError,
    RateLimiter,
    TimeoutError,
    UpstreamError,
    verify_api_key,
)
from config import Settings
from opencode.client import OpenCodeClient, OpenCodeError, OpenCodeNotFoundError
from streaming import sse_chunk, sse_done, sse_error, streaming_response
from translator import (
    ChatMessage,
    build_completion_response,
    build_model_list_response,
    build_prompt,
    event_to_stream_chunks,
)

log = logging.getLogger("gateway.routes")

router = APIRouter()


# ---------------------------------------------------------------------
# Request models (permissive — we accept extra fields silently)
# ---------------------------------------------------------------------

class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False
    # Accepted but ignored — opencode manages its own agent loop.
    tools: Optional[List[Any]] = None
    tool_choice: Optional[Any] = None
    top_p: Optional[float] = None
    n: Optional[int] = None
    stop: Optional[Any] = None
    user: Optional[str] = None


# ---------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------

def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _client(request: Request) -> OpenCodeClient:
    return request.app.state.client


def _limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter


async def deps(
    request: Request,
    settings: Settings = Depends(_settings),
    limiter: RateLimiter = Depends(_limiter),
) -> None:
    """Combined auth + rate-limit gate applied to every protected route."""
    await verify_api_key(request, settings)
    await limiter.acquire(request)


# ---------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------

@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------

@router.get("/v1/models")
async def list_models(
    request: Request,
    _: None = Depends(deps),
    client: OpenCodeClient = Depends(_client),
):
    try:
        models = await client.list_models()
    except OpenCodeNotFoundError as e:
        raise InternalError(str(e), code="opencode_not_found")
    except OpenCodeError as e:
        raise UpstreamError(
            f"Failed to list models: {e}",
            code="opencode_models_failed",
        )
    return build_model_list_response(models)


# ---------------------------------------------------------------------
# /v1/chat/completions
# ---------------------------------------------------------------------

@router.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    _: None = Depends(deps),
    client: OpenCodeClient = Depends(_client),
    settings: Settings = Depends(_settings),
):
    # --- Validate & normalise request ---
    if not body.messages:
        raise BadRequestError("`messages` must be a non-empty array.", code="empty_messages")

    try:
        msgs = [ChatMessage.from_dict(m) for m in body.messages]
    except Exception as e:
        raise BadRequestError(f"Invalid messages payload: {e}", code="invalid_messages")

    # If Hermes sends only system messages, opencode has nothing to respond to.
    if not any(m.role == "user" for m in msgs):
        raise BadRequestError(
            "At least one user message is required.",
            code="no_user_message",
        )

    # Resolve model — accept either "provider/model" or bare "model".
    model = body.model or settings.opencode_default_model
    if "/" not in model:
        # Try to qualify with the default provider ("opencode") if the user
        # sent a bare model id like "big-pickle".
        model = f"opencode/{model}"

    agent = settings.opencode_default_agent or None
    prompt = build_prompt(msgs)

    if body.stream:
        return await _stream_completion(client, model=model, prompt=prompt, agent=agent)
    return await _complete(client, model=model, prompt=prompt, agent=agent)


# ---------------------------------------------------------------------
# Non-streaming handler
# ---------------------------------------------------------------------

async def _complete(client: OpenCodeClient, *, model: str, prompt: str, agent: Optional[str]) -> JSONResponse:
    try:
        result = await client.run(model=model, prompt=prompt, agent=agent)
    except OpenCodeNotFoundError as e:
        raise InternalError(str(e), code="opencode_not_found")
    except TimeoutError as e:
        raise e
    except OpenCodeError as e:
        raise UpstreamError(
            f"opencode run failed: {e}",
            code="opencode_run_failed",
        )
    except Exception as e:
        log.exception("unexpected error in _complete")
        raise InternalError(f"Internal error: {e}", code="internal_error")

    if result.error:
        # opencode returned an error event — surface it as a 502.
        raise UpstreamError(result.error, code="opencode_session_error")

    if not result.text:
        # No text and no error — most likely the model was silent.
        # We return an empty completion rather than failing.
        log.warning("opencode produced no text for model=%s session=%s", model, result.session_id)

    return JSONResponse(
        build_completion_response(
            text=result.text,
            model=model,
            session_id=result.session_id,
        )
    )


# ---------------------------------------------------------------------
# Streaming handler (SSE)
# ---------------------------------------------------------------------

async def _stream_completion(
    client: OpenCodeClient, *, model: str, prompt: str, agent: Optional[str]
) -> JSONResponse:
    """Return an SSE StreamingResponse that mirrors opencode's event stream."""

    async def generator():
        # First chunk: signal the role (per OpenAI convention).
        from translator import build_chunk
        yield sse_chunk(build_chunk(model=model, delta_role="assistant"))

        try:
            async for event in client.stream_run(model=model, prompt=prompt, agent=agent):
                if event.type == "error":
                    yield sse_error(event.error_message, code="opencode_session_error")
                    return
                for chunk in event_to_stream_chunks(event, model=model):
                    yield sse_chunk(chunk)
        except OpenCodeNotFoundError as e:
            yield sse_error(str(e), code="opencode_not_found", type_="internal_error")
        except TimeoutError as e:
            yield sse_error(str(e), code="timeout", type_="timeout")
        except OpenCodeError as e:
            yield sse_error(str(e), code="opencode_run_failed", type_="upstream_error")
        except Exception as e:  # noqa: BLE001
            log.exception("unexpected error in streaming generator")
            yield sse_error(f"Internal error: {e}", code="internal_error", type_="internal_error")

        # Final chunk: finish_reason=stop + terminal sentinel.
        from translator import build_chunk
        yield sse_chunk(build_chunk(model=model, finish_reason="stop"))
        yield sse_done()

    return streaming_response(generator())


# ---------------------------------------------------------------------
# Error → JSON mapping (registered at app startup)
# ---------------------------------------------------------------------

def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _handle_gateway_error(_: Request, exc: GatewayError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def _handle_unknown(_: Request, exc: Exception):
        log.exception("unhandled exception")
        err = InternalError(f"Unhandled error: {exc}", code="unhandled")
        return JSONResponse(status_code=500, content=err.to_payload())
