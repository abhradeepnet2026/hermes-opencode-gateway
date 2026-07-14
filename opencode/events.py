"""OpenCode CLI event dataclasses.

The `opencode run --format json` command emits newline-delimited JSON objects.
Each object has the shape:

    {"type": "<event_type>", "timestamp": <ms>, "sessionID": "...", ...payload}

Observed event types (from opencode source `packages/opencode/src/cli/cmd/run.ts`):
  - text         : a completed text segment  (part.text)
  - reasoning    : a completed reasoning block (part.text)
  - tool_use     : a tool call finished        (part.tool, part.state)
  - step_start   : agent step started          (part)
  - step_finish  : agent step finished         (part)
  - error        : session-level error         (error)

The session ends when the internal `session.status` event becomes `idle`.
That terminal event is NOT emitted as JSON; the process simply exits.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class OpenCodeEvent:
    """Parsed NDJSON event from `opencode run --format json`."""

    type: str
    timestamp: int = 0
    session_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    # Convenience accessors for common payload fields
    @property
    def text(self) -> str:
        """Text content for `text` / `reasoning` events."""
        part = self.raw.get("part") or {}
        return str(part.get("text", ""))

    @property
    def tool_name(self) -> str:
        part = self.raw.get("part") or {}
        return str(part.get("tool", ""))

    @property
    def tool_state(self) -> Dict[str, Any]:
        part = self.raw.get("part") or {}
        return dict(part.get("state", {})) if isinstance(part.get("state"), dict) else {}

    @property
    def error_payload(self) -> Dict[str, Any]:
        err = self.raw.get("error")
        return dict(err) if isinstance(err, dict) else {"message": str(err)}

    @property
    def error_message(self) -> str:
        """Human-readable error message extracted from an `error` event."""
        err = self.error_payload
        # opencode errors look like {"name": "...", "data": {"message": "..."}}
        data = err.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        if err.get("message"):
            return str(err["message"])
        if err.get("name"):
            return str(err["name"])
        return json.dumps(err)

    @classmethod
    def from_line(cls, line: str) -> Optional["OpenCodeEvent"]:
        """Parse one stdout line into an event. Returns None for blank lines."""
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # opencode occasionally prints non-JSON banners (e.g. share URLs).
            # Treat them as opaque text events so we never lose data.
            return cls(type="raw", raw={"line": line})
        if not isinstance(obj, dict):
            return None
        return cls(
            type=str(obj.get("type", "unknown")),
            timestamp=int(obj.get("timestamp", 0) or 0),
            session_id=str(obj.get("sessionID", "")),
            raw=obj,
        )
