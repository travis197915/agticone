"""Spawn isolated OS processes for execution batches (master Celery dispatcher).

The Celery worker only calls ``spawn_execution_subprocess``; the LangGraph
pipeline runs in a child process so an OOM or hang in one batch does not
take down the master worker. Mirrors ``sop_ingestion.subprocess_manager``.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parents[1]
_RUNNER_MODULE = "execution_app.worker.batch_runner"

_lock = threading.Lock()
_active: dict[str, subprocess.Popen] = {}


def max_subprocesses() -> int:
    raw = os.environ.get("MAX_EXECUTION_SUBPROCESSES", "5")
    try:
        n = int(raw)
    except ValueError:
        n = 5
    return max(1, n)


def _reap_exited() -> None:
    for batch_id, proc in list(_active.items()):
        if proc.poll() is not None:
            log.info(
                "Execution subprocess exited  batch=%s  pid=%s  returncode=%s",
                batch_id, proc.pid, proc.returncode,
            )
            del _active[batch_id]


def _wait_for_slot() -> None:
    limit = max_subprocesses()
    poll_interval = float(os.environ.get("EXECUTION_SUBPROCESS_POLL_SEC", "0.5"))
    while True:
        with _lock:
            _reap_exited()
            if len(_active) < limit:
                return
            running = len(_active)
        log.info(
            "Execution subprocess slot full (%s/%s); waiting before spawning next batch",
            running, limit,
        )
        time.sleep(poll_interval)


def active_subprocess_snapshot() -> dict:
    with _lock:
        _reap_exited()
        return {
            batch_id: {"pid": proc.pid, "returncode": proc.poll()}
            for batch_id, proc in _active.items()
        }


def spawn_execution_subprocess(
    *,
    batch_id: str,
    xlsx_path: str,
    workflow_id: str,
    filename: str,
    claim_id_column: str | None,
    sheet_name: str | None,
) -> int:
    """Wait for a slot, spawn the batch runner subprocess, return its PID.

    Does not wait for completion — the master returns immediately, the
    caller (sync view) polls ``BatchExecutionRun.status`` for terminal
    state if it needs the result.
    """
    _wait_for_slot()

    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    base = str(_BASE_DIR)
    env["PYTHONPATH"] = base if not existing_pp else f"{base}{os.pathsep}{existing_pp}"

    cmd = [
        sys.executable, "-m", _RUNNER_MODULE,
        "--batch-id", batch_id,
        "--xlsx-path", xlsx_path,
        "--workflow-id", workflow_id,
        "--filename", filename,
    ]
    if claim_id_column:
        cmd += ["--claim-id-column", claim_id_column]
    if sheet_name:
        cmd += ["--sheet-name", sheet_name]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_BASE_DIR),
            env=env,
            stdout=None,
            stderr=None,
        )
    except OSError as exc:
        log.exception("Failed to spawn execution subprocess for batch %s", batch_id)
        raise RuntimeError(f"subprocess spawn failed: {exc}") from exc

    with _lock:
        _active[batch_id] = proc

    log.info(
        "Spawned execution subprocess  batch=%s  pid=%s  active=%s/%s",
        batch_id, proc.pid, len(_active), max_subprocesses(),
    )
    return proc.pid
