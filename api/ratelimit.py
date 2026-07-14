"""Simple in-memory token-bucket rate limiter.

One bucket per API key (or per remote IP when auth is disabled).
Refilled at `rate_limit_rpm / 60` tokens per second, with a maximum
burst capacity of `rate_limit_burst`.

This is intentionally lightweight — no Redis, no persistence. Good enough
for a single-instance local gateway. For multi-process deployments swap
in a Redis-backed limiter with the same interface.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict

from fastapi import Request

from api.errors import RateLimitError
from config import Settings


@dataclass
class Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._buckets: Dict[str, Bucket] = defaultdict(lambda: Bucket(
            tokens=float(settings.gateway_rate_limit_burst),
            last_refill=time.monotonic(),
        ))
        self._lock = asyncio.Lock()

    def _identify(self, request: Request) -> str:
        # Prefer client's API key identity when auth is on, else fall back to IP.
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return f"key:{auth.split(None, 1)[1][:16]}"  # truncate for memory
        return f"ip:{request.client.host if request.client else 'unknown'}"

    async def acquire(self, request: Request) -> None:
        if self.settings.gateway_rate_limit_rpm <= 0:
            return  # disabled

        ident = self._identify(request)
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets[ident]
            # Refill proportional to elapsed time.
            elapsed = max(0.0, now - bucket.last_refill)
            refill_rate = self.settings.gateway_rate_limit_rpm / 60.0
            bucket.tokens = min(
                float(self.settings.gateway_rate_limit_burst),
                bucket.tokens + elapsed * refill_rate,
            )
            bucket.last_refill = now

            if bucket.tokens < 1.0:
                raise RateLimitError(
                    "Rate limit exceeded. Try again in a few seconds.",
                    code="rate_limit_exceeded",
                )
            bucket.tokens -= 1.0
