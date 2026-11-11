"""Temp directory manager for web jobs.

Each conversion job gets a UUID-named directory under a shared temp root.
A background cleanup task removes directories older than ``MAX_AGE_SECONDS``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

log = logging.getLogger("pbr2rad.web")

MAX_AGE_SECONDS = 30 * 60  # 30 minutes
CLEANUP_INTERVAL = 5 * 60  # check every 5 minutes

_root: Path | None = None

# Job ids are 12 hex characters (new_job). Anything else in a URL is not a
# job: refuse it before it is joined onto a path ("..", for one).
_JOB_ID_RE = re.compile(r"[0-9a-f]{12}")

# Ceiling for everything under the jobs root (uploads + outputs of every live
# job). Past it new jobs are refused until the sweeper frees space, so a
# script cannot fill the disk. $PBR2RAD_JOBS_MAX_MB overrides; 0 disables.
DEFAULT_JOBS_MAX_MB = 3072
_USAGE_MEMO_SECONDS = 5.0
_usage_memo = (0.0, 0)


class JobsFull(Exception):
    """The jobs directory is over its size budget."""


def valid_job_id(job_id: str) -> bool:
    return isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id) is not None


def jobs_budget_bytes() -> int:
    raw = os.environ.get("PBR2RAD_JOBS_MAX_MB")
    try:
        mb = int(raw) if raw not in (None, "") else DEFAULT_JOBS_MAX_MB
    except ValueError:
        mb = DEFAULT_JOBS_MAX_MB
    return max(0, mb) << 20


def jobs_usage_bytes(*, refresh: bool = False) -> int:
    """Bytes under the jobs root, memoised for a few seconds."""
    global _usage_memo
    now = time.monotonic()
    if not refresh and now - _usage_memo[0] < _USAGE_MEMO_SECONDS:
        return _usage_memo[1]
    total = 0
    for dirpath, _dirs, names in os.walk(get_root()):
        for name in names:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            # Cached source maps are hardlinked into jobs: count a file once
            # it is ours alone, not the shared cache's.
            if st.st_nlink <= 1:
                total += st.st_size
    _usage_memo = (now, total)
    return total


def get_root() -> Path:
    """Return the shared temp root, creating it if needed."""
    global _root
    if _root is None or not _root.exists():
        _root = Path(tempfile.mkdtemp(prefix="pbr2rad_web_"))
    return _root


def new_job() -> tuple[str, Path, Path]:
    """Create a new job with upload and output directories.

    Returns ``(job_id, upload_dir, output_dir)``. Raises ``JobsFull`` when
    the jobs directory is over its size budget.
    """
    budget = jobs_budget_bytes()
    if budget and jobs_usage_bytes() > budget:
        raise JobsFull()
    job_id = uuid.uuid4().hex[:12]
    root = get_root()
    job_dir = root / job_id
    upload_dir = job_dir / "upload"
    output_dir = job_dir / "output"
    upload_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    return job_id, upload_dir, output_dir


def get_output_dir(job_id: str) -> Path | None:
    """Return the output directory for a job, or None if expired/missing."""
    if not valid_job_id(job_id):
        return None
    root = get_root()
    out = root / job_id / "output"
    return out if out.is_dir() else None


def get_job_dir(job_id: str) -> Path | None:
    """Return the job root directory, or None if expired/missing."""
    if not valid_job_id(job_id):
        return None
    root = get_root()
    d = root / job_id
    return d if d.is_dir() else None


JOB_STATE_FILE = "job.json"


def touch_job(job_id: str) -> None:
    """Restart a job's expiry clock (the sweeper keys off the job dir mtime)."""
    d = get_job_dir(job_id)
    if d is not None:
        try:
            os.utime(d, None)
        except OSError:
            pass


def write_job_state(job_id: str, state: dict, *, name: str = JOB_STATE_FILE) -> None:
    """Persist what a job was converted from/with so it can be re-rendered
    (``name`` picks another state file in the job root, e.g. preview inputs)."""
    d = get_job_dir(job_id)
    if d is None:
        return
    (d / name).write_text(json.dumps(state, indent=2), encoding="utf-8")


def read_job_state(job_id: str, *, name: str = JOB_STATE_FILE) -> dict | None:
    d = get_job_dir(job_id)
    if d is None:
        return None
    try:
        data = json.loads((d / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cleanup() -> int:
    """Remove job directories older than ``MAX_AGE_SECONDS``. Returns count removed."""
    root = get_root()
    now = time.time()
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for d in entries:
        try:
            expired = d.is_dir() and (now - d.stat().st_mtime) > MAX_AGE_SECONDS
        except OSError:
            continue  # vanished between iterdir() and stat() — someone else's problem
        if expired:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def discard_job(job_id: str) -> None:
    """Delete a job's directory now (it has nothing anyone will come back for)."""
    d = get_job_dir(job_id)
    if d is not None:
        shutil.rmtree(d, ignore_errors=True)


def teardown() -> None:
    """Remove all temp directories. Called on app shutdown."""
    global _root
    if _root is not None and _root.exists():
        shutil.rmtree(_root, ignore_errors=True)
    _root = None


async def cleanup_loop() -> None:
    """Background cleanup task — runs until cancelled.

    Each sweep runs in a worker thread (rmtree over multi-hundred-MB job
    dirs is real I/O) and is individually exception-guarded: nobody awaits
    this task, so an escaped exception would silently kill cleanup for the
    life of the process and let the disk fill.
    """
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            removed = await asyncio.to_thread(cleanup)
            if removed:
                log.info("temp cleanup: removed %d expired job dir(s)", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("temp cleanup sweep failed; will retry next interval")
