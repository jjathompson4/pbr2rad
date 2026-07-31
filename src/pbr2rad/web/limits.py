"""Lightweight in-memory rate limiting for the web API.

A dependency-free sliding-window limiter keyed by client IP, in two tiers:

* ``rate_limit`` — heavy endpoints (uploads, conversions): 20/min.
* ``rate_limit_light`` — cheap browse endpoints (search, info, download,
  preview): 60/min, enough for fast typing + tile clicking without ever
  bothering a human, while still capping a scripted loop.

Suitable for a single-instance deployment (the expected free-tier shape).
If you ever run more than one instance behind a load balancer, swap this
for a shared store (Redis) — per-instance counters won't coordinate.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

WINDOW_SECONDS = 60.0

# Prune idle-IP entries every N checks so the per-IP dict can't grow forever.
_PRUNE_EVERY = 256


def _make_limiter(max_requests: int):
    hits: dict[str, deque[float]] = defaultdict(deque)
    counter = 0

    def dependency(request: Request) -> None:
        nonlocal counter
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        cutoff = now - WINDOW_SECONDS

        dq = hits[ip]
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) >= max_requests:
            retry = int(WINDOW_SECONDS - (now - dq[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests. Try again in ~{retry}s.",
                headers={"Retry-After": str(retry)},
            )

        dq.append(now)

        # Opportunistic cleanup: every _PRUNE_EVERY checks, drop IPs whose
        # entire window has lapsed so idle clients don't accumulate forever.
        counter += 1
        if counter % _PRUNE_EVERY == 0:
            stale = [k for k, q in hits.items() if not q or q[-1] < cutoff]
            for k in stale:
                hits.pop(k, None)

    return dependency


rate_limit = _make_limiter(20)         # conversions / uploads
rate_limit_light = _make_limiter(60)   # search / info / download / preview
