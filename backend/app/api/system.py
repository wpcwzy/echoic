"""System router (stub; backend-api replaces with the full implementation)."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db, engine
from ..models import Recording, TranscriptSegment
from ..schemas import HealthOut

router = APIRouter()


def _demo_mode(db: Session) -> bool:
    try:
        from ..services.settings_store import load_ai_config

        return load_ai_config(db).demo_mode
    except Exception:
        return False


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

        worker = "ok" if settings.embedded_worker else "external"
        worker_ok = worker_status().get("running") or not settings.embedded_worker
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
