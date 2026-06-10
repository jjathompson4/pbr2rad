"""Lightweight in-memory rate limiting for the web API.

A dependency-free sliding-window limiter keyed by client IP. Suitable for a
single-instance deployment (the expected free-tier shape). If you ever run more
than one instance behind a load balancer, swap this for a shared store
(Redis) — per-instance counters won't coordinate.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

# Tunables — conservative defaults for a public free tool.
WINDOW_SECONDS = 60.0
MAX_REQUESTS_PER_WINDOW = 20

_hits: dict[str, deque[float]] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    """FastAPI dependency: raise 429 if the caller exceeds the window budget."""
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    dq = _hits[ip]

    # Drop timestamps outside the window.
    cutoff = now - WINDOW_SECONDS
    while dq and dq[0] < cutoff:
        dq.popleft()

    if len(dq) >= MAX_REQUESTS_PER_WINDOW:
        retry = int(WINDOW_SECONDS - (now - dq[0])) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Try again in ~{retry}s.",
            headers={"Retry-After": str(retry)},
        )

    dq.append(now)

    # Opportunistic cleanup so idle IPs don't accumulate forever.
    if not dq:
        _hits.pop(ip, None)
