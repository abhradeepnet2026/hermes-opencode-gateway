"""OpenCode CLI subprocess client.

Wraps `opencode run --format json` and `opencode models --verbose`,
exposing a clean async API to the rest of the gateway.

Design notes
------------
- We launch a fresh `opencode run` process per request. opencode internally
  boots an in-process server per invocation, so there's no shared state to
  manage. A future optimisation could keep a long-lived `opencode serve`
  process and talk to its HTTP API instead.
- stdout is read line-by-line and each line is parsed as NDJSON via
  `OpenCodeEvent.from_line`. stderr is captured separately for diagnostics.
- A hard timeout (configurable) kills the process to protect the gateway
  from hung upstream providers.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, List, Optional

from config import Settings
from opencode.events import OpenCodeEvent

log = logging.getLogger("gateway.opencode.client")


class OpenCodeError(Exception):
    """Raised when the opencode CLI fails in a structured way."""

    def __init__(self, message: str, *, stderr: str = "", exit_code: Optional[int] = None):
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


class OpenCodeNotFoundError(RuntimeError):
    """The opencode binary cannot be located on PATH."""


@dataclass
class OpenCodeResult:
    """Aggregated result of a non-streaming `opencode run` call."""

    text: str
    session_id: str
    events: List[OpenCodeEvent]
    error: Optional[str] = None


class OpenCodeClient:
    """Async wrapper around the `opencode` CLI."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._bin: Optional[str] = None

    # ------------------------------------------------------------------
    # Binary resolution
    # ------------------------------------------------------------------
    def resolve_binary(self) -> str:
        """Locate the opencode binary, raising if missing."""
        if self._bin:
            return self._bin
        path = shutil.which(self.settings.opencode_bin) or (
            self.settings.opencode_bin
            if Path(self.settings.opencode_bin).is_absolute()
            else None
        )
        if not path:
            raise OpenCodeNotFoundError(
                f"opencode binary not found: {self.settings.opencode_bin!r}. "
                "Install it via `npm install -g opencode-ai` or set OPENCODE_BIN."
            )
        self._bin = path
        return path

    # ------------------------------------------------------------------
    # `opencode run`
    # ------------------------------------------------------------------
    def _build_run_command(
        self, *, model: str, prompt: str, agent: Optional[str]
    ) -> List[str]:
        """Construct the argv for `opencode run --format json`."""
        cmd = [
            self.resolve_binary(),
            "run",
            "--model", model,
            "--format", "json",
        ]
        if agent:
            cmd += ["--agent", agent]
        # Auto-approve non-dangerous permissions so non-interactive runs don't
        # hang on permission prompts. opencode's `run` subcommand already
        # denies question/plan permissions by default; --auto covers the rest.
        cmd += ["--auto"]
        cmd += self.settings.extra_flags
        cmd += [prompt]
        return cmd

    async def stream_run(
        self,
        *,
        model: str,
        prompt: str,
        agent: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> AsyncIterator[OpenCodeEvent]:
        """Run opencode and yield events as they arrive on stdout.

        Yields `OpenCodeEvent` instances (one per NDJSON line). When the
        process exits, the iterator stops. If the process exits with a
        non-zero code or emits an `error` event, the relevant event is
        still yielded before stopping; callers should inspect `event.type`.
        """
        cmd = self._build_run_command(model=model, prompt=prompt, agent=agent)
        timeout = timeout or self.settings.opencode_timeout
        workdir = self.settings.workdir

        log.debug("launching opencode: %s (cwd=%s, timeout=%ss)", cmd, workdir, timeout)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir) if workdir else None,
            env=None,  # inherit
        )

        try:
            # We read stdout line-by-line as soon as bytes arrive.
            # stderr is drained concurrently to avoid pipe deadlock.
            stderr_chunks: List[str] = []
            drained_stderr = asyncio.create_task(self._drain_stderr(proc, stderr_chunks))

            assert proc.stdout is not None
            try:
                while True:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=timeout
                    )
                    if not line_bytes:
                        break
                    event = OpenCodeEvent.from_line(line_bytes.decode("utf-8", "replace"))
                    if event is not None:
                        yield event
            except asyncio.TimeoutError as exc:
                raise OpenCodeError(
                    f"opencode run timed out after {timeout}s",
                    stderr="".join(stderr_chunks),
                ) from exc

            await drained_stderr
            return_code = await proc.wait()
            if return_code not in (0, None):
                stderr_text = "".join(stderr_chunks).strip()
                # If we already yielded an error event, don't double-report.
                raise OpenCodeError(
                    f"opencode run exited with code {return_code}",
                    stderr=stderr_text,
                    exit_code=return_code,
                )
        except BaseException:
            # Make sure we never leak a process if the consumer cancels.
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise

    async def run(
        self,
        *,
        model: str,
        prompt: str,
        agent: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> OpenCodeResult:
        """Run opencode to completion and return aggregated text + events."""
        text_parts: List[str] = []
        events: List[OpenCodeEvent] = []
        session_id = ""
        error_msg: Optional[str] = None

        async for event in self.stream_run(
            model=model, prompt=prompt, agent=agent, timeout=timeout
        ):
            events.append(event)
            if event.session_id and not session_id:
                session_id = event.session_id
            if event.type == "text":
                if event.text:
                    text_parts.append(event.text)
            elif event.type == "raw":
                # Non-JSON banner line; keep as text if it doesn't look like noise.
                line = str(event.raw.get("line", "")).strip()
                if line and not line.startswith("~") and not line.startswith("!"):
                    text_parts.append(line)
            elif event.type == "error":
                error_msg = event.error_message

        return OpenCodeResult(
            text="\n".join(text_parts),
            session_id=session_id,
            events=events,
            error=error_msg,
        )

    # ------------------------------------------------------------------
    # `opencode models`
    # ------------------------------------------------------------------
    async def list_models(self, *, refresh: bool = False) -> List[dict]:
        """Return a list of model metadata dicts (parsed from `--verbose`).

        Each dict has keys: id, provider, name, family, status, context,
        output, capabilities, cost. We normalise the schema to be flat
        and OpenAI-friendly so the translator can map it cleanly.
        """
        cmd = [self.resolve_binary(), "models", "--verbose"]
        if refresh:
            cmd.append("--refresh")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.settings.workdir) if self.settings.workdir else None,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=60
        )
        if proc.returncode != 0:
            raise OpenCodeError(
                f"opencode models exited with code {proc.returncode}",
                stderr=stderr_bytes.decode("utf-8", "replace"),
                exit_code=proc.returncode,
            )

        return self._parse_models_verbose(stdout_bytes.decode("utf-8", "replace"))

    @staticmethod
    def _parse_models_verbose(output: str) -> List[dict]:
        """Parse the `opencode models --verbose` output format.

        Format (per model):
            <provider>/<model_id>
            { ...JSON metadata object... }

        Entries may or may not be separated by blank lines, so we use
        brace-matching to delimit each JSON object rather than relying
        on blank-line boundaries.
        """
        models: List[dict] = []
        lines = output.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # Header line looks like "opencode/big-pickle".
            # We require it to (a) contain "/", (b) not start with "{" and
            # (c) be a single token (no spaces) to avoid false positives.
            if line and "/" in line and not line.startswith("{") and " " not in line:
                provider, _, model_id = line.partition("/")
                i += 1
                # Collect lines until the JSON object's braces balance.
                json_buf: List[str] = []
                depth = 0
                started = False
                while i < len(lines):
                    nxt = lines[i]
                    stripped = nxt.strip()
                    # If we haven't started a JSON object yet, skip blanks.
                    if not started:
                        if stripped == "":
                            i += 1
                            continue
                        if not stripped.startswith("{"):
                            # Next model's header came with no JSON body.
                            break
                    if "{" in stripped:
                        depth += stripped.count("{")
                        started = True
                    if "}" in stripped:
                        depth -= stripped.count("}")
                    json_buf.append(nxt)
                    i += 1
                    if started and depth <= 0:
                        break
                raw_meta: dict = {}
                if json_buf:
                    try:
                        raw_meta = json.loads("\n".join(json_buf))
                    except json.JSONDecodeError:
                        raw_meta = {}
                models.append(
                    OpenCodeClient._normalise_model(
                        provider=provider, model_id=model_id, meta=raw_meta, header=line
                    )
                )
            else:
                i += 1
        return models

    @staticmethod
    def _normalise_model(*, provider: str, model_id: str, meta: dict, header: str) -> dict:
        """Flatten opencode's verbose model schema into a friendly dict."""
        limit = meta.get("limit") or {}
        caps = meta.get("capabilities") or {}
        cost = meta.get("cost") or {}
        return {
            "id": header,  # full provider/model id, e.g. "opencode/big-pickle"
            "provider": provider,
            "model_id": model_id,
            "name": meta.get("name") or header,
            "family": meta.get("family") or "",
            "status": meta.get("status") or "active",
            "context_window": limit.get("context"),
            "max_input": limit.get("input"),
            "max_output": limit.get("output"),
            "supports_temperature": bool(caps.get("temperature")),
            "supports_reasoning": bool(caps.get("reasoning")),
            "supports_tool_calls": bool(caps.get("toolcall")),
            "supports_attachments": bool(caps.get("attachment")),
            "input_cost_per_1k": cost.get("input"),
            "output_cost_per_1k": cost.get("output"),
            "release_date": meta.get("release_date"),
            "raw": meta,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _drain_stderr(self, proc: asyncio.subprocess.Process, sink: List[str]) -> None:
        """Continuously read stderr into `sink` so the pipe never blocks."""
        if proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            sink.append(chunk.decode("utf-8", "replace"))


# Eager import for json used in static parser
import json  # noqa: E402  (placed here to keep the public API at the top)
