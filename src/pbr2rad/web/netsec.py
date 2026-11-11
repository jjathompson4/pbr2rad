"""Who is really calling, and the response headers every page gets.

Production traffic reaches the app as visitor -> Cloudflare -> Fly proxy ->
uvicorn. Two things follow from that:

* ``X-Forwarded-For`` is whatever the caller typed (both proxies append to
  it), so it must never key the rate limiter. ``Fly-Client-IP`` is set by the
  Fly proxy to the address that connected to it and cannot be forged by the
  caller; when that address is a Cloudflare edge, ``CF-Connecting-IP`` is the
  visitor. Anything else connected to Fly directly and is its own client.
* The ``*.fly.dev`` / raw-IP route bypasses Cloudflare. With
  ``PBR2RAD_REQUIRE_CLOUDFLARE=1`` requests that did not come through a
  Cloudflare edge are refused (the health check excepted).
"""

from __future__ import annotations

import ipaddress
import os

from fastapi import Request

# https://www.cloudflare.com/ips-v4 and /ips-v6 (fetched 2026-11-11). They
# change rarely; override with PBR2RAD_CLOUDFLARE_CIDRS (comma-separated)
# if Cloudflare ever adds a range before this list is updated.
_CLOUDFLARE_CIDRS = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)

REQUIRE_CLOUDFLARE_ENV = "PBR2RAD_REQUIRE_CLOUDFLARE"
CLOUDFLARE_CIDRS_ENV = "PBR2RAD_CLOUDFLARE_CIDRS"

_networks_cache: tuple[str, tuple] | None = None


def _networks() -> tuple:
    global _networks_cache
    raw = os.environ.get(CLOUDFLARE_CIDRS_ENV, "")
    if _networks_cache is None or _networks_cache[0] != raw:
        cidrs = list(_CLOUDFLARE_CIDRS) + [c.strip() for c in raw.split(",") if c.strip()]
        nets = []
        for c in cidrs:
            try:
                nets.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                continue
        _networks_cache = (raw, tuple(nets))
    return _networks_cache[1]


def _parse_ip(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def is_cloudflare(ip) -> bool:
    if ip is None:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in _networks())


def came_through_cloudflare(request: Request) -> bool:
    """False only for a request the Fly proxy says came from a non-Cloudflare
    address. Without ``Fly-Client-IP`` (no Fly proxy in front: local runs,
    tests) there is nothing to judge by, and the request is let through: a
    caller cannot strip a header the proxy adds, so this opens nothing."""
    raw = request.headers.get("fly-client-ip")
    if raw is None:
        return True
    return is_cloudflare(_parse_ip(raw))


def client_ip(request: Request) -> str:
    """The address to rate-limit on. Never taken from X-Forwarded-For."""
    fly_ip = _parse_ip(request.headers.get("fly-client-ip"))
    if fly_ip is not None:
        if is_cloudflare(fly_ip):
            visitor = _parse_ip(request.headers.get("cf-connecting-ip"))
            if visitor is not None:
                return str(visitor)
        return str(fly_ip)
    # No Fly proxy in front (local dev, tests): the socket peer.
    return request.client.host if request.client else "unknown"


def cloudflare_required() -> bool:
    return os.environ.get(REQUIRE_CLOUDFLARE_ENV) == "1"


# The page loads only its own script and stylesheet; images come from the two
# texture sites' CDNs (and data: URIs for generated map thumbnails).
# static.cloudflareinsights.com / cloudflareinsights.com: Cloudflare Web
# Analytics, when it is switched on for the zone. 'unsafe-inline' for styles
# only (the markup uses style attributes); scripts get no inline allowance.
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self' https://static.cloudflareinsights.com",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https:",
    "connect-src 'self' https://cloudflareinsights.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Strict-Transport-Security": "max-age=31536000",
    "Cross-Origin-Opener-Policy": "same-origin",
}

# FastAPI's interactive docs pull their script from a CDN and use an inline
# one; they are off in production (PBR2RAD_DISABLE_DOCS=1), and exempt from
# the CSP when left on for local development.
_CSP_EXEMPT_PREFIXES = ("/docs", "/redoc")


def apply_security_headers(path: str, headers) -> None:
    for k, v in SECURITY_HEADERS.items():
        headers.setdefault(k, v)
    if not path.startswith(_CSP_EXEMPT_PREFIXES):
        headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
