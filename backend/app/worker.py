"""Background worker: embedded daemon thread + standalone entry point.

Embedded mode is started/stopped from the FastAPI lifespan (``main.py``); the
standalone mode is ``python -m app.worker``. Both poll the DB job queue, claim
one job at a time and run :func:`app.services.pipeline.process_recording`.

Public contract (kept stable for main.py / settings_store.py):

    start_embedded_worker() -> None
    stop_embedded_worker() -> None
    worker_status() -> dict  # {"running", "queue_depth", "last_error", ...}
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from sqlalchemy import select

from .config import settings
from .core.utils import utcnow
from .db import SessionLocal, init_db
from .models import Job, JobStatus
from .services.queue import claim_next_job, queue_depth, reset_stale_tasks

logger = logging.getLogger(__name__)

_state: dict = {
    "running": False,
    "last_error": None,
    "processed": 0,
    "failed": 0,
    "current": None,
    "started_at": None,
}
_stop_event = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _depth() -> int:
    try:
        with SessionLocal() as db:
            return queue_depth(db)
    except Exception:  # pragma: no cover - DB may be booting
        return 0


def _mark_job_failed(job_id: int, message: str) -> None:
    try:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is not None and job.status == JobStatus.RUNNING:
                job.status = JobStatus.FAILED
                job.error = message[:1000]
                job.stage = "failed"
                job.finished_at = utcnow()
                db.commit()
    except Exception:  # pragma: no cover
        logger.exception("worker failed to mark job %s as failed", job_id)


def _process_one(job_id: int, recording_id: int, from_stage: str) -> None:
    from .services.pipeline import process_recording

    try:
        asyncio.run(process_recording(recording_id, from_stage))
        _state["processed"] = int(_state.get("processed") or 0) + 1
    except Exception as exc:  # noqa: BLE001 - worker thread must survive everything
        message = f"{type(exc).__name__}: {exc}"
        _state["last_error"] = message[:500]
        _state["failed"] = int(_state.get("failed") or 0) + 1
        logger.exception("worker failed on job %s (recording %s)", job_id, recording_id)
        _mark_job_failed(job_id, "处理任务异常终止：" + str(exc)[:200])


def _maybe_consume_scan() -> None:
    """Periodic consume-directory import (R-22): at most once per interval."""
    try:
        from .services.consume import maybe_scan

        maybe_scan()
    except Exception:  # noqa: BLE001 - auto-import must never stop job processing
        logger.exception("consume scan hook failed")


def _worker_loop() -> None:
    logger.info("embedded worker started")
    _state["running"] = True
    _state["started_at"] = utcnow().isoformat()
    while not _stop_event.is_set():
        _maybe_consume_scan()
        try:
            claimed = claim_next_job()
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = f"claim failed: {exc}"[:500]
            logger.exception("worker failed to claim a job")
            _stop_event.wait(1.5)
            continue
        if claimed is None:
            _stop_event.wait(1.5)
            continue
        job_id, recording_id, from_stage = claimed
        _state["current"] = {"job_id": job_id, "recording_id": recording_id, "from": from_stage}
        try:
            _process_one(job_id, recording_id, from_stage)
        finally:
            _state["current"] = None
            _state["queue_depth"] = _depth()
            _stop_event.wait(1.0)
    _state["running"] = False
    logger.info("embedded worker stopped")


def start_embedded_worker() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _state["running"] = True
        _thread = threading.Thread(target=_worker_loop, name="echoic-worker", daemon=True)
        _thread.start()
        logger.info("embedded worker thread launched")


def stop_embedded_worker() -> None:
    global _thread
    with _lock:
        _stop_event.set()
        thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=10)
        if thread.is_alive():  # pragma: no cover - shutdown race
            logger.warning("embedded worker did not stop within 10s")
    with _lock:
        _thread = None
    _state["running"] = False
    logger.info("embedded worker stopped")


def worker_status() -> dict:
    current = _state.get("current")
    try:
        from .services.consume import last_scan_info

        last_consume_scan = last_scan_info()
    except Exception:  # pragma: no cover - status must stay safe before boot
        last_consume_scan = None
    return {
        "running": bool(_state.get("running")),
        "queue_depth": _depth(),
        "last_error": _state.get("last_error"),
        "processed": int(_state.get("processed") or 0),
        "failed": int(_state.get("failed") or 0),
        "current": dict(current) if isinstance(current, dict) else None,
        "embedded": bool(settings.embedded_worker),
        "started_at": _state.get("started_at"),
        "last_consume_scan": last_consume_scan,
    }


def run_forever() -> None:
    """Blocking loop used by ``python -m app.worker``."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    init_db()
    reset_stale_tasks()
    logger.info("standalone worker starting (data dir: %s)", settings.data_path)
    try:
        while True:
            _maybe_consume_scan()
            claimed = claim_next_job()
            if claimed is None:
                time.sleep(1.5)
                continue
            job_id, recording_id, from_stage = claimed
            _state["current"] = {"job_id": job_id, "recording_id": recording_id, "from": from_stage}
            _process_one(job_id, recording_id, from_stage)
            _state["current"] = None
    except KeyboardInterrupt:
        logger.info("standalone worker interrupted, exiting")
    finally:
        _state["running"] = False


if __name__ == "__main__":  # pragma: no cover - process entry point
    run_forever()


__all__ = [
    "run_forever",
    "start_embedded_worker",
    "stop_embedded_worker",
    "worker_status",
]
