"""System health / info / consume endpoints (docs/API.md §6, docs/08-P1 §4.4).

v0.2: ``/info``, ``/logs``, ``/seed-demo`` and the consume scan are admin-only;
``/health`` stays public.
"""

from __future__ import annotations

import inspect
import logging
import shutil
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..core.errors import ApiError
from ..core.utils import to_iso, utcnow
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import Chunk, Recording, TranscriptSegment, User
from ..schemas import FeaturesOut, HealthOut

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
    trash_info = None
    try:
        from ..services.trash import last_purge_info

        trash_info = last_purge_info()
    except Exception:  # pragma: no cover - defensive
        pass
    return {
        "embedded": embedded,
        "running": running,
        "queue_depth": queue_depth,
        "last_error": last_error,
        "last_trash_purge": trash_info,
    }


def _count_files(directory, *, exclude_suffix: str | None = None) -> int:
    try:
        if not directory.is_dir():
            return 0
        count = 0
        for child in directory.iterdir():
            if not child.is_file():
                continue
            if exclude_suffix and child.name.endswith(exclude_suffix):
                continue
            count += 1
        return count
    except OSError:
        return 0


def _fallback_consume_status() -> dict:
    consume_dir = settings.consume_path
    return {
        "enabled": bool(settings.consume_enabled),
        "dir": str(consume_dir),
        "interval_sec": max(5, int(settings.consume_interval_sec or 15)),
        "pending_files": _count_files(consume_dir),
        "failed_files": _count_files(consume_dir / "failed", exclude_suffix=".error.txt"),
        "last_scan": None,
    }


def consume_status() -> dict:
    """Consume-directory status; falls back to local filesystem inspection."""
    try:
        from ..services.consume import consume_status as service_status

        data = service_status()
        if isinstance(data, dict):
            return data
    except Exception:  # pragma: no cover - service lands with backend-ai
        logger.debug("consume_status 不可用，使用本地回退信息", exc_info=True)
    return _fallback_consume_status()


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
    try:
        from ..core.storage_backend import storage_status

        storage_state = storage_status()
    except Exception:  # pragma: no cover - health must never raise
        storage_state = {"backend": settings.storage_backend, "status": "degraded", "detail": ""}
    overall = (
        "ok"
        if db_status == "ok"
        and media == "ok"
        and ffmpeg == "ok"
        and storage_state.get("status") == "ok"
        else "degraded"
    )
    return HealthOut(
        status=overall,
        version=settings.version,
        db=db_status,
        media=media,
        ffmpeg=ffmpeg,
        worker="ok" if worker_ok else "stopped",
        storage=storage_state.get("status", "ok"),
        storage_backend=storage_state.get("backend", settings.storage_backend),
        storage_detail=str(storage_state.get("detail") or ""),
        demo_mode=_demo_mode(db),
        time=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


@router.get("/info")
def info(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
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
    recordings = int(
        db.scalar(
            select(func.count())
            .select_from(Recording)
            .where(Recording.deleted_at.is_(None))
        )
        or 0
    )
    trashed = int(
        db.scalar(
            select(func.count())
            .select_from(Recording)
            .where(Recording.deleted_at.is_not(None))
        )
        or 0
    )
    return {
        "version": settings.version,
        "data_dir": str(data_path),
        "disk": disk,
        "recordings": recordings,
        "trashed": trashed,
        "segments": int(db.scalar(select(func.count()).select_from(TranscriptSegment)) or 0),
        "chunks": int(db.scalar(select(func.count()).select_from(Chunk)) or 0),
        "worker": _worker_state(db),
        "consume": consume_status(),
    }


@router.get("/features", response_model=FeaturesOut)
def features(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Capability discovery for the frontend (docs/10 §6). Login required, not admin."""
    return FeaturesOut.model_validate(
        {
            "live": _live_feature_block(db),
            "vector": {
                "backend": _resolve_vector_backend(),
                "search_enabled": True,
                "embeddings_configured": _embeddings_configured(db),
            },
            "storage": {"backend": _storage_backend_name()},
            "trash": {"retention_days": max(1, int(settings.trash_retention_days or 30))},
        }
    )


def _live_feature_block(db: Session) -> dict:
    """Live capability block; prefer the live service's resolver when available."""
    try:
        from ..services import live as live_service

        payload = live_service.live_features(db)
        if isinstance(payload, dict):
            return payload
    except Exception:  # pragma: no cover - live service lands with backend-ai
        logger.debug("services.live 不可用，使用本地特性解析", exc_info=True)
    merge_mode = (
        settings.live_merge_mode
        if settings.live_merge_mode in ("replace", "promote")
        else "replace"
    )
    return {
        "enabled": bool(settings.live_enabled),
        "provider": _resolve_live_provider(db),
        "max_minutes": int(settings.live_max_minutes or 60),
        "merge_mode": merge_mode,
    }


def _storage_backend_name() -> str:
    try:
        from ..core.storage_backend import backend_name

        return backend_name()
    except Exception:  # pragma: no cover - storage backend is optional
        return settings.storage_backend or "local"


def _resolve_live_provider(db: Session) -> str:
    configured = (settings.live_provider or "auto").strip().lower()
    if configured in ("mock", "dashscope_realtime"):
        return configured
    # auto: follow the configured ASR provider (mock ASR → mock realtime).
    try:
        from ..services.settings_store import load_ai_config

        asr_provider = load_ai_config(db).asr.provider
    except Exception:
        asr_provider = settings.asr_provider
    if (asr_provider or "").startswith("dashscope"):
        return "dashscope_realtime"
    return "mock"


def _resolve_vector_backend() -> str:
    configured = (settings.vector_backend or "auto").strip().lower()
    if configured in ("numpy", "pgvector"):
        return configured
    # auto: pgvector is optional/experimental, numpy is always available.
    return "numpy"


def _embeddings_configured(db: Session) -> bool:
    try:
        from ..services.settings_store import load_ai_config

        provider = load_ai_config(db).embeddings.provider
    except Exception:
        provider = settings.embeddings_provider
    return (provider or "none").strip().lower() not in ("", "none")


@router.post("/consume/scan")
async def consume_scan(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    try:
        from ..services.consume import scan_now
    except Exception:  # concurrent development: service not landed yet
        raise ApiError(503, "consume 导入服务尚未就绪，请稍后重试", "SERVICE_UNAVAILABLE")
    try:
        payload = scan_now()
        if inspect.isawaitable(payload):
            payload = await payload
    except NotImplementedError:  # pragma: no cover - transitional stub
        raise ApiError(503, "consume 导入服务尚未就绪，请稍后重试", "SERVICE_UNAVAILABLE")
    except Exception as exc:  # catastrophic errors only; per-file failures are counted
        logger.exception("consume scan failed")
        raise ApiError(500, f"consume 扫描失败：{exc}", "CONSUME_ERROR")

    if not isinstance(payload, dict):
        payload = {}
    raw_result = payload.get("result")
    result_source = raw_result if isinstance(raw_result, dict) else payload
    result = {
        key: int(result_source.get(key) or 0)
        for key in ("scanned", "imported", "duplicates", "failed")
    }
    at = payload.get("at") or to_iso(utcnow())
    logger.info("consume scan done: %s", result)
    return {"ok": True, "result": result, "at": at}


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
