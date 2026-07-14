"""Translate between OpenAI Chat Completions schema and opencode CLI I/O.

OpenAI → opencode
    messages: List[Message]  →  prompt: str   (role-tagged single string)
    model:    str             →  model: str    (passed through)

opencode → OpenAI (non-streaming)
    OpenCodeResult.text       →  choices[0].message.content
    OpenCodeResult.session_id →  id (prefixed)

opencode → OpenAI (streaming)
    Each `text` event becomes one chunk with delta.content = event.text.
    `error` events become an error chunk then we close the stream.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from opencode.events import OpenCodeEvent


# ---------------------------------------------------------------------
# Request-side models (subset of OpenAI schema that we accept)
# ---------------------------------------------------------------------

@dataclass
class ChatMessage:
    role: str
    content: str
    name: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChatMessage":
        # OpenAI allows content to be either a string or a list of content parts.
        content = d.get("content", "")
        if isinstance(content, list):
            # Concatenate text parts; ignore non-text parts for now.
            text_bits = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_bits.append(str(part.get("text", "")))
                elif isinstance(part, str):
                    text_bits.append(part)
            content = "\n".join(text_bits)
        elif content is None:
            content = ""
        return cls(
            role=str(d.get("role", "user")),
            content=str(content),
            name=d.get("name"),
        )


# ---------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------

_ROLE_LABELS = {
    "system": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
    "function": "Function",
}


def build_prompt(messages: Iterable[ChatMessage]) -> str:
    """Flatten OpenAI messages into a single prompt string for opencode.

    opencode's `run` subcommand takes one user-side message; we encode the
    full conversation history with role tags so the underlying model can
    still distinguish turns. This mirrors what most CLI chat wrappers do.
    """
    parts: List[str] = []
    for msg in messages:
        label = _ROLE_LABELS.get(msg.role.lower(), msg.role.title())
        if msg.name:
            parts.append(f"[{label} ({msg.name})]")
        else:
            parts.append(f"[{label}]")
        parts.append(msg.content.strip())
        parts.append("")  # blank line between turns
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------

def _new_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _now() -> int:
    return int(time.time())


def build_completion_response(
    *,
    text: str,
    model: str,
    session_id: str = "",
    finish_reason: str = "stop",
) -> Dict[str, Any]:
    """Build a non-streaming OpenAI ChatCompletion response object."""
    return {
        "id": _new_id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "system_fingerprint": f"opencode:{session_id}" if session_id else "opencode",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            # opencode doesn't report token usage in the JSON event stream.
            # We report zeros; Hermes doesn't depend on these for correctness.
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def build_chunk(
    *,
    model: str,
    session_id: str = "",
    delta_content: Optional[str] = None,
    delta_role: Optional[str] = None,
    finish_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a single SSE chunk for a streaming chat completion."""
    delta: Dict[str, Any] = {}
    if delta_role is not None:
        delta["role"] = delta_role
    if delta_content is not None:
        delta["content"] = delta_content
    return {
        "id": _new_id(),
        "object": "chat.completion.chunk",
        "created": _now(),
        "model": model,
        "system_fingerprint": f"opencode:{session_id}" if session_id else "opencode",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
    }


def build_model_list_response(models: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build an OpenAI-compatible /v1/models list response."""
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "created": _now(),
                "owned_by": m.get("provider") or "opencode",
                # Extra metadata we expose for Hermes UIs that consume it.
                "meta": {
                    "name": m.get("name"),
                    "family": m.get("family"),
                    "context_window": m.get("context_window"),
                    "max_output": m.get("max_output"),
                    "supports_tool_calls": m.get("supports_tool_calls"),
                    "supports_reasoning": m.get("supports_reasoning"),
                    "supports_temperature": m.get("supports_temperature"),
                    "release_date": m.get("release_date"),
                },
            }
            for m in models
        ],
    }


# ---------------------------------------------------------------------
# Event → chunk translation (for streaming)
# ---------------------------------------------------------------------

def event_to_stream_chunks(
    event: OpenCodeEvent, *, model: str
) -> List[Dict[str, Any]]:
    """Translate one opencode event into zero or more SSE chunks."""
    if event.type == "text" and event.text:
        return [build_chunk(model=model, session_id=event.session_id, delta_content=event.text)]
    if event.type == "reasoning" and event.text:
        # OpenAI streaming has no native "reasoning" channel; we surface it
        # as ordinary content prefixed with a marker so Hermes still shows it.
        return [
            build_chunk(
                model=model,
                session_id=event.session_id,
                delta_content=f"\n[reasoning] {event.text}\n",
            )
        ]
    if event.type == "raw":
        line = str(event.raw.get("line", "")).strip()
        if not line or line.startswith("~") or line.startswith("!"):
            return []
        return [build_chunk(model=model, session_id=event.session_id, delta_content=line + "\n")]
    # tool_use, step_start, step_finish, error: no content delta emitted.
    # Errors are handled by the streaming route (it raises / sends error chunk).
    return []
