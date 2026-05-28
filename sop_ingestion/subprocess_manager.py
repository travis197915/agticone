"""Spawn isolated OS processes for ingestion jobs (master Celery dispatcher).

The Celery worker only calls ``spawn_ingestion_subprocess``; LangGraph runs in
child processes so crashes/OOM in one job do not take down the master worker.
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
_JOB_RUNNER_MODULE = "sop_ingestion.worker.job_runner"

_lock = threading.Lock()
_active: dict[str, subprocess.Popen] = {}


def max_subprocesses() -> int:
    raw = os.environ.get("MAX_PIPELINE_SUBPROCESSES", "10")
    try:
        n = int(raw)
    except ValueError:
        n = 10
    return max(1, n)


def _reap_exited() -> None:
    """Remove finished children from the active map."""
    for job_id, proc in list(_active.items()):
        if proc.poll() is not None:
            code = proc.returncode
            log.info(
                "Subprocess exited  job=%s  pid=%s  returncode=%s",
                job_id,
                proc.pid,
                code,
            )
            del _active[job_id]


def _wait_for_slot() -> None:
    """Block until fewer than MAX_PIPELINE_SUBPROCESSES children are running."""
    limit = max_subprocesses()
    poll_interval = float(os.environ.get("PIPELINE_SUBPROCESS_POLL_SEC", "0.5"))
    while True:
        with _lock:
            _reap_exited()
            if len(_active) < limit:
                return
            running = len(_active)
        log.info(
            "Subprocess slot full (%s/%s); waiting before spawning next job",
            running,
            limit,
        )
        time.sleep(poll_interval)


def active_subprocess_snapshot() -> dict:
    """For health/debug: job_id -> pid and returncode (None if still running)."""
    with _lock:
        _reap_exited()
        return {
            job_id: {"pid": proc.pid, "returncode": proc.poll()}
            for job_id, proc in _active.items()
        }


def spawn_ingestion_subprocess(job_id: str) -> int:
    """Wait for a slot, spawn job_runner, return child PID. Does not wait for completion."""
    _wait_for_slot()

    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    base = str(_BASE_DIR)
    env["PYTHONPATH"] = base if not existing_pp else f"{base}{os.pathsep}{existing_pp}"

    cmd = [sys.executable, "-m", _JOB_RUNNER_MODULE, job_id]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_BASE_DIR),
            env=env,
            stdout=None,
            stderr=None,
        )
    except OSError as exc:
        log.exception("Failed to spawn subprocess for job %s", job_id)
        raise RuntimeError(f"subprocess spawn failed: {exc}") from exc

    with _lock:
        _active[job_id] = proc

    log.info(
        "Spawned ingestion subprocess  job=%s  pid=%s  active=%s/%s",
        job_id,
        proc.pid,
        len(_active),
        max_subprocesses(),
    )
    return proc.pid
