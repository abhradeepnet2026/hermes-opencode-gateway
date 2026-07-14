"""Server-Sent Events helpers for streaming chat completions.

OpenAI's streaming format is a series of `data: <json>\n\n` events,
terminated by a `data: [DONE]\n\n` sentinel. We build a generator
that translates opencode events into OpenAI-format chunks.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, AsyncGenerator

from fastapi.responses import StreamingResponse

log = logging.getLogger("gateway.streaming")


def _sse(data: str) -> bytes:
    """Format one SSE frame."""
    return f"data: {data}\n\n".encode("utf-8")


def sse_chunk(payload: dict) -> bytes:
    """Serialise a dict as one SSE `data:` frame."""
    return _sse(json.dumps(payload, ensure_ascii=False))


def sse_done() -> bytes:
    """Terminal sentinel."""
    return b"data: [DONE]\n\n"


def sse_error(message: str, *, code: str = "upstream_error", type_: str = "upstream_error") -> bytes:
    """An SSE frame carrying an OpenAI-style error object.

    OpenAI's streaming spec doesn't formally define error frames mid-stream,
    but most clients accept a `data:` frame with an `error` field. We also
    close the connection right after.
    """
    payload = {
        "error": {
            "message": message,
            "type": type_,
            "param": None,
            "code": code,
        }
    }
    return _sse(json.dumps(payload, ensure_ascii=False))


def streaming_response(generator: AsyncGenerator[bytes, None]) -> StreamingResponse:
    """Wrap an async generator into a FastAPI StreamingResponse."""
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering if behind proxy
        },
    )


async def identity_async_iter() -> AsyncIterator[bytes]:
    """Empty async iterator (used as a no-op fallback)."""
    if False:  # pragma: no cover
        yield b""
