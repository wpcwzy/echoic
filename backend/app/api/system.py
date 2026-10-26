"""System health / info endpoints (docs/API.md §6)."""

from __future__ import annotations

import logging
import shutil
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import Chunk, Recording, TranscriptSegment, User
from ..schemas import HealthOut

logger = logging.getLogger(__name__)

router = APIRouter()

#: tiny in-memory tail buffer for GET /api/system/logs (installed on first use)
_LOG_BUFFER: deque[str] = deque(maxlen=2000)
_LOG_HANDLER_INSTALLED = False


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:  # pragma: no cover - never break logging
            pass


def _install_log_buffer() -> None:
    global _LOG_HANDLER_INSTALLED
    if _LOG_HANDLER_INSTALLED:
        return
    handler = _BufferHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    _LOG_HANDLER_INSTALLED = True


def _demo_mode(db: Session) -> bool:
    try:
        from ..services.settings_store import load_ai_config

        return load_ai_config(db).demo_mode
    except Exception:
        return False


def _worker_state(db: Session) -> dict:
    embedded = bool(settings.embedded_worker)
    running = False
    last_error = None
    try:
        from ..worker import worker_status

        status = worker_status() or {}
        running = bool(status.get("running"))
        last_error = status.get("last_error")
    except Exception:
        running = embedded
    queue_depth = 0
    try:
        from ..services.settings_store import queue_depth_safe

        queue_depth = int(queue_depth_safe(db) or 0)
    except Exception:
        pass
    return {
        "embedded": embedded,
        "running": running,
        "queue_depth": queue_depth,
        "last_error": last_error,
    }


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    db_status = "ok"
    try:
        db.execute(select(1))
    except Exception:
        db_status = "error"
    ffmpeg = "ok" if shutil.which(settings.ffmpeg) else "missing"
    media = "ok" if settings.media_path.exists() else "error"
    try:
        from ..worker import worker_status

        worker_ok = bool(worker_status().get("running")) or not settings.embedded_worker
    except Exception:
        worker_ok = True
    overall = "ok" if db_status == "ok" and media == "ok" and ffmpeg == "ok" else "degraded"
    return HealthOut(
        status=overall,
        version=settings.version,
        db=db_status,
        media=media,
        ffmpeg=ffmpeg,
        worker="ok" if worker_ok else "stopped",
        demo_mode=_demo_mode(db),
        time=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


@router.get("/info")
def info(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    data_path = settings.data_path
    try:
        usage = shutil.disk_usage(str(data_path))
        disk = {
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
        }
    except OSError:
        disk = {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0}
    return {
        "version": settings.version,
        "data_dir": str(data_path),
        "disk": disk,
        "recordings": int(db.scalar(select(func.count()).select_from(Recording)) or 0),
        "segments": int(db.scalar(select(func.count()).select_from(TranscriptSegment)) or 0),
        "chunks": int(db.scalar(select(func.count()).select_from(Chunk)) or 0),
        "worker": _worker_state(db),
    }


@router.post("/seed-demo")
def seed_demo(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    from ..services import demo as demo_service

    created = demo_service.seed_demo(db)
    return {"ok": True, "created": int(created or 0)}


@router.get("/logs")
def logs(
    lines: int = Query(200, ge=1, le=2000),
    _admin: User = Depends(require_admin),
):
    _install_log_buffer()
    tail = list(_LOG_BUFFER)[-lines:]
    return {"lines": tail, "count": len(tail)}
