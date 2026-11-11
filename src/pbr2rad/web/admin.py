"""The operator's private stats page: ``GET /admin/stats``.

Off (404) unless ``PBR2RAD_ADMIN_TOKEN`` is set. With it set, the page asks
for HTTP Basic credentials: any username, the token as the password. Set it
as a Fly secret (``fly secrets set PBR2RAD_ADMIN_TOKEN=...``), never in
fly.toml.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from . import metrics
from .limits import _make_limiter

router = APIRouter()

# Its own small bucket: wrong passwords run out of tries quickly.
_admin_limit = _make_limiter(10)


def _require_admin(request: Request) -> None:
    token = os.environ.get(metrics.ADMIN_TOKEN_ENV) or ""
    if len(token) < 16:
        # Unset (or too short to be a secret): the page does not exist.
        raise HTTPException(404, "Not Found")
    supplied = ""
    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() == "basic":
        try:
            supplied = base64.b64decode(value, validate=True).decode("utf-8").partition(":")[2]
        except (binascii.Error, UnicodeDecodeError):
            supplied = ""
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(
            401, "Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="pbr2rad stats", charset="UTF-8"'},
        )


def _table(head: tuple[str, ...], rows) -> str:
    if not rows:
        return "<p class='none'>Nothing recorded yet.</p>"
    th = "".join(f"<th>{escape(h)}</th>" for h in head)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape('' if c is None else str(c))}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


_STYLE = """
body{font:14px/1.5 system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222;background:#fafafa}
h1{font-size:1.3rem} h2{font-size:1rem;margin-top:2rem}
table{border-collapse:collapse;width:100%;background:#fff}
th,td{text-align:left;padding:.3rem .6rem;border-bottom:1px solid #e3e3e3;font-variant-numeric:tabular-nums}
th{font-weight:600;color:#555}
.tiles{display:flex;gap:1rem;flex-wrap:wrap}
.tile{background:#fff;border:1px solid #e3e3e3;border-radius:6px;padding:.6rem 1rem;min-width:8rem}
.tile b{display:block;font-size:1.5rem}
.none,.note{color:#777} nav a{margin-right:.8rem}
@media (prefers-color-scheme: dark){body{background:#18181a;color:#ddd}table,.tile{background:#222}th,td,.tile{border-color:#333}th{color:#aaa}a{color:#9cf}}
"""


@router.get("/admin/stats", response_class=HTMLResponse, include_in_schema=False,
            dependencies=[Depends(_admin_limit), Depends(_require_admin)])
async def stats(days: int = Query(default=30, ge=1, le=400)):
    data = metrics.summary(days)
    totals = data["totals"]
    tiles = "".join(
        f"<div class='tile'><b>{int(totals.get(k, 0))}</b>{escape(label)}</div>"
        for k, label in (("convert", "conversions"), ("rerender", "re-renders"), ("download", "downloads"))
    )
    ranges = "".join(f"<a href='/admin/stats?days={d}'>{d} days</a>" for d in (7, 30, 90, 365))
    error = f"<p class='note'>{escape(data['error'])}</p>" if data.get("error") else ""
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>pbr2rad stats</title><style>{_STYLE}</style></head><body>
<h1>pbr2rad usage, last {data['days']} days</h1>
<nav>{ranges}</nav>{error}
<p class="note">Counts of what the server did, in UTC. No visitor data is stored: no IP addresses,
no cookies, nothing about uploads beyond the fact that one happened. Page views and referrers are in
Cloudflare Web Analytics.</p>
<div class="tiles">{tiles}</div>
<h2>By day</h2>{_table(("Day", "Conversions ok", "Conversions failed", "Re-renders", "Downloads"), data["daily"])}
<h2>Conversions by source</h2>{_table(("Source", "Attempts", "Succeeded", "Average ms"), data["by_source"])}
<h2>Most converted catalog materials</h2>{_table(("Source", "Asset", "Conversions"), data["top_assets"])}
<h2>Preview outcome of successful jobs</h2>{_table(("Preview status", "Jobs"), data["previews"])}
<h2>Failures</h2>{_table(("Status", "Detail", "Count"), data["failures"])}
</body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})
