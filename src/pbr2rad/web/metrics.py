"""Anonymous usage counters for the operator's stats page.

One SQLite file, one row per event. A row says what happened (a conversion,
a re-render, a download), from which source, which catalog asset, how it
ended and how long it took. It never holds an IP address, a user agent, a
cookie, an upload's filename or the name a visitor typed: uploads are
recorded as source ``"upload"`` with no asset.

Everything here is best-effort: a failed write is logged and dropped, never
surfaced to the request that caused it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

log = logging.getLogger("pbr2rad.web")

DATA_DIR_ENV = "PBR2RAD_DATA_DIR"
ADMIN_TOKEN_ENV = "PBR2RAD_ADMIN_TOKEN"
RETENTION_DAYS = 400
_PRUNE_EVERY = 500

_lock = threading.Lock()
_writes = 0
_ready_for: Path | None = None


def db_path() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    base = Path(override) if override else Path.home() / ".local" / "share" / "pbr2rad"
    return base / "metrics.sqlite"


def _connect() -> sqlite3.Connection:
    global _ready_for
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=2.0)
    if _ready_for != path:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " ts INTEGER NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,"
            " source TEXT, asset TEXT, status TEXT, ms INTEGER, detail TEXT)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS events_day ON events(day, kind)")
        conn.commit()
        _ready_for = path
    return conn


def record(
    kind: str,
    *,
    source: str | None = None,
    asset: str | None = None,
    status: str | None = None,
    ms: float | None = None,
    detail: str | None = None,
) -> None:
    """Append one event. Never raises."""
    global _writes
    try:
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        with _lock, closing(_connect()) as conn:
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                (int(now), day, kind, source, (asset or None) and str(asset)[:64],
                 status, None if ms is None else int(ms), (detail or None) and str(detail)[:64]),
            )
            _writes += 1
            if _writes % _PRUNE_EVERY == 0:
                conn.execute("DELETE FROM events WHERE ts < ?", (int(now) - RETENTION_DAYS * 86400,))
            conn.commit()
    except Exception:
        log.warning("metrics write failed", exc_info=True)


def summary(days: int = 30) -> dict:
    """Aggregates for the stats page over the last ``days`` days (UTC)."""
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1) * 86400))
    out: dict = {"days": days, "since": since, "daily": [], "by_source": [],
                 "top_assets": [], "failures": [], "previews": [], "totals": {}}
    try:
        with _lock, closing(_connect()) as conn:
            q = conn.execute
            out["totals"] = dict(q(
                "SELECT kind, COUNT(*) FROM events WHERE day >= ? GROUP BY kind", (since,)))
            out["daily"] = q(
                "SELECT day,"
                " SUM(kind='convert' AND status='ok'),"
                " SUM(kind='convert' AND status!='ok'),"
                " SUM(kind='rerender'), SUM(kind='download')"
                " FROM events WHERE day >= ? GROUP BY day ORDER BY day DESC", (since,)).fetchall()
            out["by_source"] = q(
                "SELECT COALESCE(source,'?'), COUNT(*), SUM(status='ok'), CAST(AVG(CASE WHEN status='ok' THEN ms END) AS INTEGER)"
                " FROM events WHERE day >= ? AND kind='convert' GROUP BY source ORDER BY 2 DESC",
                (since,)).fetchall()
            out["top_assets"] = q(
                "SELECT source, asset, COUNT(*) FROM events"
                " WHERE day >= ? AND kind='convert' AND asset IS NOT NULL"
                " GROUP BY source, asset ORDER BY 3 DESC LIMIT 25", (since,)).fetchall()
            out["failures"] = q(
                "SELECT COALESCE(status,'?'), COALESCE(detail,''), COUNT(*) FROM events"
                " WHERE day >= ? AND kind IN ('convert','rerender') AND status!='ok'"
                " GROUP BY status, detail ORDER BY 3 DESC LIMIT 25", (since,)).fetchall()
            out["previews"] = q(
                "SELECT COALESCE(detail,'?'), COUNT(*) FROM events"
                " WHERE day >= ? AND kind IN ('convert','rerender') AND status='ok'"
                " GROUP BY detail ORDER BY 2 DESC", (since,)).fetchall()
    except Exception:
        log.warning("metrics read failed", exc_info=True)
        out["error"] = "The metrics database could not be read."
    return out
