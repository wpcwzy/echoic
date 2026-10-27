"""Live transcription session manager for R-19 (GA, v0.3.10).

Supports reconnect/resume of an active session over the same WebSocket URL: a
socket close without an explicit ``stop`` keeps the session ``active`` for
``RECONNECT_GRACE_SEC`` seconds and a reconnect continues the timeline from the
last received audio position (new upstream provider segment, offset timestamps,
no duplicated final segments). Explicit stop/cancel, max-duration, provider
failures and abandoned sessions are finalized as before.

Responsibilities
----------------
* HTTP-facing helpers: feature/provider resolution, session creation/lookup,
  segment listing, idempotent stop/cancel.
* WebSocket duplex endpoint logic (``handle_ws_stream``): authenticate via
  cookie/bearer/query token, enforce owner/admin + enabled + active, accept
  optional ``start`` JSON, consume binary PCM16LE 16 kHz mono frames, stream
  provider ``partial``/``final`` events back as JSON, persist final segments and
  finish cleanly with ``ready``/``error``/``done`` events.
* One in-process runtime per session (single-process deployment, no Redis).

Event protocol (docs/10 §4.1)::

    {"type":"ready"}
    {"type":"partial","text":"…","start_ms":0,"end_ms":1200}
    {"type":"final","text":"…","start_ms":0,"end_ms":2000,"speaker_label":null}
    {"type":"error","message":"…","code":"…"}
    {"type":"done","total_ms":120000}

Close codes: 4403 (auth/permission/live-disabled), 4404 (session not found),
4409 (not active / second producer), 4413 (audio backpressure), 1000 normal,
1011 provider failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid as uuid_lib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..core.errors import ApiError, ProviderError
from ..core.security import COOKIE_NAME, decode_access_token
from ..core.utils import utcnow
from ..db import SessionLocal
from ..deps import is_admin
from ..models import LiveSegment, LiveSession, User
from ..providers.realtime_dashscope import DEFAULT_MODEL as DASHSCOPE_DEFAULT_MODEL
from ..providers.realtime_dashscope import DashScopeRealtimeProvider
from ..providers.realtime_mock import LiveASRProvider, MockRealtimeProvider
from .settings_store import effective_realtime_config, load_ai_config

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_MS = 32  # PCM16LE mono @16 kHz
ALLOWED_PROVIDERS = {"mock", "dashscope_realtime"}
ALLOWED_MERGE_MODES = {"replace", "promote"}
DEFAULT_MERGE_MODE = "replace"
PENDING_AUDIO_LIMIT_MS = 10_000
AUDIO_QUEUE_MAX_FRAMES = 512
WS_CLOSE_MESSAGE = "实时转写会话已结束"

# v0.3.10 GA: reconnect/resume + abandoned-session cleanup
RECONNECT_GRACE_SEC = 120  # how long a disconnected active session stays resumable
STALE_SESSION_MARGIN_SEC = 300  # grace on top of ECHOIC_LIVE_MAX_MINUTES for stale rows
MAX_CONTIGUOUS_GAP_MS = 5_000  # clamp small inter-segment gaps so resumed finals stay contiguous


# --------------------------------------------------------------------- helpers
def _max_audio_ms() -> int:
    """Hard per-session audio cap (ECHOIC_LIVE_MAX_MINUTES)."""
    try:
        minutes = int(settings.live_max_minutes or 60)
    except (TypeError, ValueError):
        minutes = 60
    return max(1, minutes) * 60_000


def _max_pending_audio_ms() -> int:
    """Bounded audio queue (~10s): overflow closes the WS with 4413."""
    return PENDING_AUDIO_LIMIT_MS


def _default_language(db: Session) -> str:
    try:
        config = load_ai_config(db)
        return str(config.system.get("default_language") or config.asr.language or "zh")
    except Exception:  # noqa: BLE001 - language is cosmetic
        return "zh"


def _settings_merge_mode() -> str:
    value = str(settings.live_merge_mode or DEFAULT_MERGE_MODE).strip().lower()
    return value if value in ALLOWED_MERGE_MODES else DEFAULT_MERGE_MODE


def _reconnect_grace_sec() -> float:
    """Seconds a disconnected-but-active session stays resumable (tests monkeypatch this)."""
    return float(RECONNECT_GRACE_SEC)


def _stale_session_secs() -> int:
    """Age after which an active DB row without a live runtime is considered abandoned."""
    try:
        minutes = max(1, int(settings.live_max_minutes or 60))
    except (TypeError, ValueError):
        minutes = 60
    return int(minutes * 60 + STALE_SESSION_MARGIN_SEC)


def _session_active(session_id: int) -> bool:
    try:
        with SessionLocal() as db:
            status = db.scalar(select(LiveSession.status).where(LiveSession.id == session_id))
            return status == "active"
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to read live session %s status", session_id)
        return False


def _session_resume_state(session_id: int) -> tuple[int, int, int]:
    """(base_audio_ms, last_final_end_ms, final_segment_count) for reconnect resume."""
    try:
        with SessionLocal() as db:
            session = db.get(LiveSession, session_id)
            total = int(session.total_audio_ms or 0) if session is not None else 0
            rows = db.execute(
                select(LiveSegment.start_ms, LiveSegment.end_ms)
                .where(LiveSegment.session_id == session_id, LiveSegment.is_final.is_(True))
                .order_by(LiveSegment.idx)
            ).all()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to read resume state for live session %s", session_id)
        return 0, 0, 0
    last_end = max((int(row.end_ms or 0) for row in rows), default=0)
    base = max(total, last_end)
    return base, last_end, len(rows)


def cleanup_stale_sessions(db: Session | None = None, *, now: datetime | None = None) -> int:
    """Mark abandoned active sessions (no live runtime, started too long ago) as ended.

    Used opportunistically when a new session is created; safe to call manually
    from tools/tests. Returns the number of sessions cleaned up.
    """
    from datetime import timedelta

    current = now or utcnow()
    cutoff = current - timedelta(seconds=_stale_session_secs())
    own_session = db is None
    session = db or SessionLocal()
    cleaned = 0
    try:
        rows = session.scalars(
            select(LiveSession).where(
                LiveSession.status == "active",
                LiveSession.started_at.is_not(None),
                LiveSession.started_at < cutoff,
            )
        ).all()
        for row in rows:
            if row.id in _runtimes:  # attached or awaiting reconnect: the runtime owns it
                continue
            row.status = "ended"
            row.ended_at = current
            row.error = row.error or "会话长时间无活动，已自动结束"
            cleaned += 1
        if cleaned:
            session.commit()
            logger.info("cleaned up %s stale live session(s)", cleaned)
        return cleaned
    except Exception:  # pragma: no cover - defensive
        logger.exception("stale live session cleanup failed")
        if own_session:
            session.rollback()
        return 0
    finally:
        if own_session:
            session.close()


def resolve_live_provider(db: Session | None = None) -> tuple[str, str]:
    """Resolve ``(provider, model)`` from ECHOIC_LIVE_PROVIDER.

    ``auto`` → ``mock`` when the configured ASR provider is mock, otherwise
    ``dashscope_realtime`` (per docs/10 §0.1). Returns a model name too, so API
    and settings can display it.
    """
    raw = str(settings.live_provider or "auto").strip().lower()
    config = None
    if db is not None:
        try:
            config = load_ai_config(db)
        except Exception:  # noqa: BLE001 - provider resolution must not crash requests
            config = None
    effective = effective_realtime_config(config) if config is not None else None
    if raw in ("mock", "dashscope_realtime"):
        provider = raw
    else:
        if raw not in ("", "auto"):
            logger.warning("unknown ECHOIC_LIVE_PROVIDER=%r, falling back to auto", raw)
        provider = effective.provider if effective is not None else "mock"
    if provider == "mock":
        return "mock", "mock-realtime"
    model = effective.model if effective is not None else DASHSCOPE_DEFAULT_MODEL
    return provider, model or DASHSCOPE_DEFAULT_MODEL


def live_features(db: Session | None = None) -> dict[str, Any]:
    """Feature-flag block for ``GET /api/system/features`` (backend-api)."""
    provider, model = resolve_live_provider(db)
    try:
        max_minutes = max(1, int(settings.live_max_minutes or 60))
    except (TypeError, ValueError):
        max_minutes = 60
    return {
        "enabled": bool(settings.live_enabled),
        "provider": provider,
        "model": model,
        "max_minutes": max_minutes,
        "merge_mode": _settings_merge_mode(),
    }


def build_live_provider(
    db: Session,
    provider: str,
    model: str = "",
    *,
    language: str | None = None,
) -> LiveASRProvider:
    """Create the streaming provider for a session (no network yet)."""
    if provider == "mock":
        return MockRealtimeProvider(model=model or "mock-realtime", language=language or "zh")
    if provider == "dashscope_realtime":
        try:
            config = load_ai_config(db)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("无法读取实时转写配置，请先在设置中完成实时转写配置") from exc
        effective = effective_realtime_config(config)
        api_key = str(effective.api_key or "")
        if not api_key:
            raise ProviderError(
                "实时转写未配置 DashScope API Key：可在「设置 → AI 服务 → 实时转写」单独填写，或复用文件转写的 Key"
            )
        base_url = str(effective.base_url or "").strip()
        if not base_url or "/compatible-mode" in base_url:  # compatible-mode URL is not the WS endpoint
            base_url = ""
        return DashScopeRealtimeProvider(
            base_url=base_url,
            model=model or effective.model or DASHSCOPE_DEFAULT_MODEL,
            api_key=api_key,
            language=language or effective.language or "zh",
        )
    raise ProviderError(f"不支持的实时转写 provider：{provider}")


# --------------------------------------------------------------- HTTP helpers
def create_live_session(
    db: Session,
    user: User,
    *,
    language: str | None = None,
    merge_mode: str | None = None,
) -> LiveSession:
    """Create an active live session owned by ``user``."""
    if not settings.live_enabled:
        raise ApiError(403, "实时转写未启用（管理员可在设置或环境变量中开启）", "LIVE_DISABLED")
    try:
        cleanup_stale_sessions(db)
    except Exception:  # noqa: BLE001 - cleanup must never block session creation
        logger.warning("live session cleanup failed", exc_info=True)
    provider, model = resolve_live_provider(db)
    if provider == "dashscope_realtime":
        effective = effective_realtime_config(load_ai_config(db))
        if not effective.api_key:
            raise ApiError(
                400,
                "实时转写需要 DashScope API Key：请在「设置 → AI 服务 → 实时转写」填写，"
                "或复用文件转写的 Key",
                "LIVE_PROVIDER_ERROR",
            )
    requested_mode = str(merge_mode or "").strip().lower()
    resolved_mode = requested_mode if requested_mode in ALLOWED_MERGE_MODES else _settings_merge_mode()
    language_value = (language or _default_language(db) or "zh")[:16]
    session = LiveSession(
        uuid=str(uuid_lib.uuid4()),
        user_id=int(user.id),
        recording_id=None,
        status="active",
        provider=provider,
        model=model,
        language=language_value,
        merge_mode=resolved_mode,
        started_at=utcnow(),
        total_audio_ms=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    logger.info("live session %s created by user %s (provider=%s, merge=%s)", session.id, user.id, provider, resolved_mode)
    return session


def get_session_for_user(db: Session, user: User, session_id: int) -> LiveSession:
    """Owner/admin lookup; non-visible sessions are reported as 404."""
    session = db.get(LiveSession, session_id)
    if session is None:
        raise ApiError(404, "实时会话不存在", "NOT_FOUND")
    if not (is_admin(user) or session.user_id == user.id):
        raise ApiError(404, "实时会话不存在", "NOT_FOUND")
    return session


def list_live_segments(db: Session, session_id: int) -> list[LiveSegment]:
    return list(
        db.scalars(
            select(LiveSegment)
            .where(LiveSegment.session_id == session_id)
            .order_by(LiveSegment.idx, LiveSegment.id)
        ).all()
    )


def session_out_payload(session: LiveSession) -> dict[str, Any]:
    """Serialize a session for API responses (matches ``LiveSessionOut``)."""
    return {
        "id": session.id,
        "uuid": session.uuid,
        "status": session.status,
        "provider": session.provider or "",
        "model": session.model or "",
        "language": session.language or "zh",
        "merge_mode": session.merge_mode or DEFAULT_MERGE_MODE,
        "recording_id": session.recording_id,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "total_audio_ms": int(session.total_audio_ms or 0),
        "error": session.error,
        "ws_url": f"/api/live/sessions/{session.id}/stream",
    }


def segment_out_payload(segment: LiveSegment) -> dict[str, Any]:
    return {
        "id": segment.id,
        "idx": segment.idx,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "text": segment.text,
        "is_final": bool(segment.is_final),
        "speaker_label": segment.speaker_label,
    }


# ----------------------------------------------------------------- runtime map
@dataclass
class _LiveRuntime:
    session_id: int
    websocket: Any
    provider: LiveASRProvider
    max_audio_ms: int
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    audio_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=AUDIO_QUEUE_MAX_FRAMES))
    client_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stop_reason: str = "ended"
    error_message: str | None = None
    close_code: int = 1000
    total_audio_ms: int = 0
    pending_audio_ms: int = 0
    last_partial: dict[str, Any] | None = None
    last_final_end_ms: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)
    feeder_task: asyncio.Task | None = None
    pump_task: asyncio.Task | None = None
    reader_task: asyncio.Task | None = None
    closing: bool = False
    finalized: bool = False
    finalizer_started: bool = False
    # GA reconnect/resume bookkeeping (session-scoped, survives connection swaps)
    audio_base_ms: int = 0
    connection_index: int = 0
    detached: bool = False
    detaching: bool = False
    detached_event: asyncio.Event = field(default_factory=asyncio.Event)
    abandon_task: asyncio.Task | None = None


_runtimes: dict[int, _LiveRuntime] = {}


def active_session_ids() -> list[int]:
    return sorted(_runtimes)


# ------------------------------------------------------------- WS safe output
async def _send_json(runtime: _LiveRuntime, payload: dict[str, Any]) -> None:
    async with runtime.send_lock:
        try:
            await runtime.websocket.send_json(payload)
        except Exception:  # noqa: BLE001 - client may be gone; never crash the session
            logger.debug("live session %s: websocket send failed", runtime.session_id)


async def _send_json_raw(websocket: Any, payload: dict[str, Any]) -> None:
    try:
        await websocket.send_json(payload)
    except Exception:  # noqa: BLE001
        logger.debug("live websocket send failed before session start")


async def _close_ws(websocket: Any, code: int) -> None:
    try:
        await websocket.close(code=code)
    except Exception:  # noqa: BLE001
        logger.debug("live websocket close failed (code=%s)", code)


# --------------------------------------------------------------------- auth
def _ws_user_id(websocket: Any) -> int | None:
    token = ""
    try:
        token = websocket.cookies.get(COOKIE_NAME) or ""
    except Exception:  # noqa: BLE001 - defensive for non-browser clients
        token = ""
    if not token:
        try:
            token = websocket.query_params.get("token") or ""
        except Exception:  # noqa: BLE001
            token = ""
    if not token:
        header = websocket.headers.get("authorization", "") if websocket.headers else ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    return decode_access_token(token) if token else None


# -------------------------------------------------------------- persistence
def _persist_final_segment(runtime: _LiveRuntime, event: dict[str, Any]) -> dict[str, Any] | None:
    text = str(event.get("text") or "").strip()
    if not text:
        return None
    try:
        start = max(0, int(event.get("start_ms") or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        end = max(start + 100, int(event.get("end_ms") or start + 500))
    except (TypeError, ValueError):
        end = start + 500
    if start < runtime.last_final_end_ms:
        start = runtime.last_final_end_ms
    elif runtime.last_final_end_ms > 0 and start - runtime.last_final_end_ms <= MAX_CONTIGUOUS_GAP_MS:
        # Resume/connection hand-over: keep the persisted timeline contiguous
        # instead of leaving a small uncovered gap for audio already received.
        start = runtime.last_final_end_ms
    if end <= start:
        end = start + 200
    speaker_label = event.get("speaker_label")
    speaker_label = str(speaker_label)[:64] if speaker_label else None
    with SessionLocal() as db:
        max_idx = db.scalar(
            select(func.max(LiveSegment.idx)).where(LiveSegment.session_id == runtime.session_id)
        )
        idx = int(max_idx) + 1 if max_idx is not None else 0
        segment = LiveSegment(
            session_id=runtime.session_id,
            idx=idx,
            start_ms=start,
            end_ms=end,
            text=text,
            is_final=True,
            speaker_label=speaker_label,
        )
        db.add(segment)
        session = db.get(LiveSession, runtime.session_id)
        if session is not None and runtime.total_audio_ms > int(session.total_audio_ms or 0):
            session.total_audio_ms = int(runtime.total_audio_ms)
        db.commit()
        segment_id = int(segment.id)
    runtime.last_final_end_ms = end
    return {
        "segment_id": segment_id,
        "idx": idx,
        "start_ms": start,
        "end_ms": end,
        "text": text,
        "speaker_label": speaker_label,
    }


def _mark_session_failed(session_id: int, message: str) -> None:
    try:
        with SessionLocal() as db:
            session = db.get(LiveSession, session_id)
            if session is None:
                return
            session.status = "failed"
            session.ended_at = utcnow()
            session.error = (message or "实时转写失败")[:1000]
            db.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to mark live session %s as failed", session_id)


# --------------------------------------------------------------- ws internals
async def _client_reader(runtime: _LiveRuntime) -> None:
    websocket = runtime.websocket
    try:
        while True:
            message = await websocket.receive()
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                await runtime.client_queue.put({"kind": "disconnect"})
                return
            data = message.get("bytes")
            if data is not None:
                await runtime.client_queue.put({"kind": "bytes", "data": data})
                continue
            text = message.get("text")
            if text is not None:
                await runtime.client_queue.put({"kind": "text", "data": text})
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - client vanished mid-frame
        try:
            await runtime.client_queue.put({"kind": "disconnect"})
        except Exception:  # pragma: no cover
            pass


async def _audio_feeder(runtime: _LiveRuntime) -> None:
    while True:
        pcm = await runtime.audio_queue.get()
        if pcm is None:
            return
        try:
            await runtime.provider.send_audio(pcm)
        except ProviderError as exc:
            runtime.error_message = exc.message
            runtime.stop_reason = "failed"
            runtime.close_code = 1011
            runtime.stop_event.set()
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("live session %s: audio send failed", runtime.session_id)
            runtime.error_message = f"实时转写音频发送失败（{type(exc).__name__}）"
            runtime.stop_reason = "failed"
            runtime.close_code = 1011
            runtime.stop_event.set()
            return
        finally:
            runtime.pending_audio_ms = max(0, runtime.pending_audio_ms - (len(pcm) // BYTES_PER_MS))


def _offset_event(runtime: _LiveRuntime, event: dict[str, Any]) -> dict[str, Any]:
    """Add the resume base to provider-relative timestamps (GA reconnect continuity)."""
    base = int(runtime.audio_base_ms or 0)
    if not base:
        return dict(event)
    adjusted = dict(event)
    adjusted["start_ms"] = int(event.get("start_ms") or 0) + base
    adjusted["end_ms"] = int(event.get("end_ms") or 0) + base
    return adjusted


async def _pump_provider_events(runtime: _LiveRuntime) -> None:
    try:
        while True:
            event = await runtime.provider.events.get()
            if event is None:
                return
            event = _offset_event(runtime, event)
            event_type = event.get("type")
            if event_type == "partial":
                runtime.last_partial = dict(event)
                await _send_json(
                    runtime,
                    {
                        "type": "partial",
                        "text": str(event.get("text") or ""),
                        "start_ms": int(event.get("start_ms") or 0),
                        "end_ms": int(event.get("end_ms") or 0),
                        "speaker_label": event.get("speaker_label"),
                    },
                )
            elif event_type == "final":
                runtime.last_partial = None
                persisted = _persist_final_segment(runtime, event)
                if persisted is not None:
                    await _send_json(runtime, {"type": "final", **persisted})
            elif event_type == "error":
                runtime.error_message = str(event.get("message") or "实时转写服务异常")[:1000]
                runtime.stop_reason = "failed"
                runtime.close_code = 1011
                await _send_json(
                    runtime,
                    {
                        "type": "error",
                        "message": runtime.error_message,
                        "code": str(event.get("code") or "PROVIDER_ERROR"),
                    },
                )
                runtime.stop_event.set()
                return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - event pump must never crash the handler silently
        logger.exception("live session %s: provider event pump failed", runtime.session_id)
        runtime.error_message = "实时转写事件处理失败"
        runtime.stop_reason = "failed"
        runtime.close_code = 1011
        runtime.stop_event.set()


async def _next_client_item(runtime: _LiveRuntime) -> dict[str, Any] | None:
    get_task = asyncio.create_task(runtime.client_queue.get())
    stop_task = asyncio.create_task(runtime.stop_event.wait())
    pending: set[asyncio.Task] = {get_task, stop_task}
    try:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    if stop_task in done and not stop_task.cancelled():
        return None
    if get_task in done and not get_task.cancelled():
        return get_task.result()
    return None


async def _message_loop(runtime: _LiveRuntime) -> None:
    while True:
        item = await _next_client_item(runtime)
        if item is None:
            return
        kind = item.get("kind")
        if kind == "disconnect":
            # GA: an unexpected socket close keeps the session resumable; only an
            # explicit stop/max-duration/provider error finalizes it.
            runtime.stop_reason = "disconnect"
            return
        if kind == "bytes":
            pcm = item["data"]
            if not pcm:
                continue
            if len(pcm) % 2 != 0:
                await _send_json(
                    runtime,
                    {"type": "error", "message": "音频帧不是 16bit 对齐，已忽略该帧", "code": "BAD_FRAME"},
                )
                continue
            delta_ms = len(pcm) // BYTES_PER_MS
            runtime.pending_audio_ms += delta_ms
            if runtime.pending_audio_ms > _max_pending_audio_ms():
                runtime.stop_reason = "ended"
                runtime.error_message = "音频发送过快，实时转写缓冲区已满，请放慢发送速度后重试"
                runtime.close_code = 4413
                await _send_json(
                    runtime,
                    {"type": "error", "message": runtime.error_message, "code": "LIVE_BACKPRESSURE"},
                )
                return
            runtime.total_audio_ms += delta_ms
            if runtime.total_audio_ms > runtime.max_audio_ms:
                runtime.stop_reason = "ended"
                runtime.error_message = (
                    f"已达到实时转写单次上限（{max(1, int(settings.live_max_minutes or 60))} 分钟），已自动结束"
                )
                await _send_json(
                    runtime,
                    {"type": "error", "message": runtime.error_message, "code": "LIVE_MAX_DURATION"},
                )
                return
            try:
                runtime.audio_queue.put_nowait(pcm)
            except asyncio.QueueFull:
                runtime.stop_reason = "ended"
                runtime.error_message = "实时转写音频队列已满，请稍后重试"
                runtime.close_code = 4413
                await _send_json(
                    runtime,
                    {"type": "error", "message": runtime.error_message, "code": "LIVE_BACKPRESSURE"},
                )
                return
            continue
        # text control message
        try:
            payload = json.loads(item["data"])
        except (TypeError, ValueError):
            await _send_json(
                runtime, {"type": "error", "message": "无法解析控制消息（需要 JSON）", "code": "BAD_MESSAGE"}
            )
            continue
        message_type = str(payload.get("type") or "").strip().lower()
        if message_type == "stop":
            runtime.stop_reason = "ended"
            return
        if message_type == "start":
            sample_rate = payload.get("sample_rate", SAMPLE_RATE)
            channels = payload.get("channels", 1)
            try:
                sample_rate = int(sample_rate)
                channels = int(channels)
            except (TypeError, ValueError):
                sample_rate, channels = 0, 0
            if sample_rate not in (SAMPLE_RATE,) or channels != 1:
                runtime.stop_reason = "failed"
                runtime.error_message = "仅支持 16kHz 单声道 PCM16LE 音频，请检查采集端重采样设置"
                await _send_json(
                    runtime,
                    {"type": "error", "message": runtime.error_message, "code": "UNSUPPORTED_FORMAT"},
                )
                return
            continue
        if message_type in ("ping", "heartbeat"):
            if message_type == "ping":
                await _send_json(runtime, {"type": "pong", "ts": int(time.time() * 1000)})
            continue
        # Unknown control messages are ignored for forward compatibility.


def _promote_trailing_partial(runtime: _LiveRuntime) -> dict[str, Any] | None:
    partial = runtime.last_partial
    if not partial or not str(partial.get("text") or "").strip():
        return None
    event = dict(partial)
    event["end_ms"] = max(int(event.get("end_ms") or 0), int(runtime.total_audio_ms or 0))
    persisted = _persist_final_segment(runtime, event)
    runtime.last_partial = None
    return persisted


def _finalize_session_row(runtime: _LiveRuntime) -> tuple[str, int, int | None]:
    status = "ended"
    if runtime.stop_reason == "cancelled":
        status = "cancelled"
    elif runtime.stop_reason == "failed":
        status = "failed"
    total_ms = int(runtime.total_audio_ms or 0)
    recording_id: int | None = None
    try:
        with SessionLocal() as db:
            session = db.get(LiveSession, runtime.session_id)
            if session is None:
                return status, total_ms, None
            if session.status not in ("merged",):
                session.status = status
            session.ended_at = utcnow()
            session.total_audio_ms = max(int(session.total_audio_ms or 0), total_ms)
            total_ms = int(session.total_audio_ms)
            if runtime.error_message:
                session.error = str(runtime.error_message)[:1000]
            recording_id = session.recording_id
            db.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to finalize live session %s", runtime.session_id)
    return status, total_ms, recording_id


# Strong references to detached finalizers: when the ASGI server cancels the
# websocket handler on client disconnect we still want DB persistence / detach
# bookkeeping to finish (TestClient cancels the handler almost immediately after
# the disconnect frame).
_finalizer_tasks: set[asyncio.Task] = set()


def _cancel_abandon(runtime: _LiveRuntime) -> None:
    task = runtime.abandon_task
    runtime.abandon_task = None
    if task is not None and not task.done():
        task.cancel()


async def _close_provider(runtime: _LiveRuntime) -> None:
    provider = runtime.provider
    if provider is None:
        return
    try:
        await asyncio.wait_for(provider.close(), timeout=15)
    except Exception:  # noqa: BLE001 - provider close is best effort
        logger.debug("live session %s: provider close failed", runtime.session_id, exc_info=True)
    runtime.provider = None


def _store_total_audio(runtime: _LiveRuntime) -> None:
    """Persist the accumulated audio position (resume base survives restarts)."""
    try:
        with SessionLocal() as db:
            session = db.get(LiveSession, runtime.session_id)
            if session is not None and int(runtime.total_audio_ms or 0) > int(session.total_audio_ms or 0):
                session.total_audio_ms = int(runtime.total_audio_ms)
                db.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to store live audio position for session %s", runtime.session_id)


def _reset_connection(runtime: _LiveRuntime, websocket: Any, *, max_audio_ms: int | None = None) -> None:
    """Re-arm the per-connection runtime fields for a reconnect of the same session."""
    from ..config import settings as _settings  # local alias keeps test monkeypatching simple

    _ = _settings
    runtime.websocket = websocket
    if max_audio_ms is not None:
        runtime.max_audio_ms = int(max_audio_ms)
    runtime.audio_queue = asyncio.Queue(maxsize=AUDIO_QUEUE_MAX_FRAMES)
    runtime.client_queue = asyncio.Queue()
    runtime.send_lock = asyncio.Lock()
    runtime.stop_event = asyncio.Event()
    runtime.stop_reason = "ended"
    runtime.error_message = None
    runtime.close_code = 1000
    runtime.pending_audio_ms = 0
    runtime.last_partial = None
    runtime.closing = False
    runtime.finalized = False
    runtime.finalizer_started = False
    runtime.detaching = False
    runtime.detached = False
    runtime.detached_event = asyncio.Event()
    runtime.feeder_task = None
    runtime.pump_task = None
    runtime.reader_task = None


async def _flush_stream(runtime: _LiveRuntime, *, finalize: bool) -> None:
    """Drain the current connection's audio/provider into DB + client.

    ``finalize=True`` is an explicit stop/max-duration/provider-failure (flush the
    provider's remaining finals + persist any trailing partial). ``finalize=False``
    is a disconnect/detach: finish the upstream task without inventing finals, so
    the resumed connection can continue from the same audio position.
    """
    if runtime.closing:
        return
    runtime.closing = True
    # 1) release the feeder after all queued audio has been forwarded upstream
    try:
        runtime.audio_queue.put_nowait(None)
    except asyncio.QueueFull:
        try:
            runtime.audio_queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover
            pass
        try:
            runtime.audio_queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover
            pass
    if runtime.feeder_task is not None:
        try:
            await asyncio.wait_for(runtime.feeder_task, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            runtime.feeder_task.cancel()
            await asyncio.gather(runtime.feeder_task, return_exceptions=True)
    # 2) flush or simply close the upstream provider
    provider = runtime.provider
    if provider is not None:
        if finalize:
            try:
                await asyncio.wait_for(provider.finish(), timeout=60)
            except asyncio.TimeoutError:
                logger.warning("live session %s: provider finish timed out", runtime.session_id)
            except ProviderError as exc:
                runtime.error_message = runtime.error_message or exc.message
            except Exception:  # noqa: BLE001
                logger.exception("live session %s: provider finish failed", runtime.session_id)
        else:
            try:
                await asyncio.wait_for(provider.close(), timeout=15)
            except Exception:  # noqa: BLE001
                logger.debug("live session %s: provider close failed", runtime.session_id, exc_info=True)
    # 3) drain provider events
    if runtime.pump_task is not None:
        try:
            await asyncio.wait_for(runtime.pump_task, timeout=30)
        except asyncio.TimeoutError:
            runtime.pump_task.cancel()
            await asyncio.gather(runtime.pump_task, return_exceptions=True)
        runtime.pump_task = None
    runtime.feeder_task = None
    # 4) an explicit stop must not lose the last partial caption; a disconnect
    #    keeps it as partial (resumed connection will produce the final once)
    if finalize and runtime.stop_reason != "cancelled":
        persisted = _promote_trailing_partial(runtime)
        if persisted is not None and runtime.websocket is not None:
            await _send_json(runtime, {"type": "final", **persisted})
    runtime.last_partial = None
    _store_total_audio(runtime)


async def _end_connection(runtime: _LiveRuntime, *, detach: bool) -> None:
    """Finish the current connection: either detach for resume or finalize the session."""
    try:
        try:
            await _flush_stream(runtime, finalize=not detach)
        except Exception:  # noqa: BLE001 - never leave the session half-finalized
            logger.exception("live session %s: flush failed during connection end", runtime.session_id)
        if detach and not runtime.finalized and _session_active(runtime.session_id):
            runtime.detached = True
            runtime.detaching = False
            runtime.websocket = None
            runtime.detached_event.set()
            logger.info(
                "live session %s detached at %sms, resumable for %ss",
                runtime.session_id,
                runtime.total_audio_ms,
                int(_reconnect_grace_sec()),
            )
            _schedule_abandon(runtime)
            return
        status, total_ms, recording_id = _finalize_session_row(runtime)
        if runtime.websocket is not None:
            await _send_json(
                runtime,
                {"type": "done", "total_ms": total_ms, "status": status, "recording_id": recording_id},
            )
        runtime.finalized = True
        runtime.detached = False
        runtime.detaching = False
        if _runtimes.get(runtime.session_id) is runtime:
            _runtimes.pop(runtime.session_id, None)
        websocket = runtime.websocket
        runtime.websocket = None
        if websocket is not None:
            await _close_ws(websocket, runtime.close_code)
        runtime.done_event.set()
        logger.info(
            "live session %s finished (status=%s, audio=%sms, reason=%s)",
            runtime.session_id,
            status,
            runtime.total_audio_ms,
            runtime.stop_reason,
        )
    finally:
        if runtime.reader_task is not None:
            runtime.reader_task.cancel()
            await asyncio.gather(runtime.reader_task, return_exceptions=True)
            runtime.reader_task = None
        await _close_provider(runtime)
        runtime.detached_event.set()


def _schedule_end(runtime: _LiveRuntime, *, detach: bool) -> asyncio.Task | None:
    """Schedule (cancellation-shielded) connection end. Returns the task or None."""
    if runtime.finalized or runtime.finalizer_started:
        return None
    runtime.finalizer_started = True
    runtime.detaching = True
    task = asyncio.create_task(_end_connection(runtime, detach=detach))
    _finalizer_tasks.add(task)
    task.add_done_callback(_finalizer_tasks.discard)
    return task


def _schedule_abandon(runtime: _LiveRuntime) -> None:
    """After the reconnect grace period, end a session that was never resumed."""
    _cancel_abandon(runtime)
    task = asyncio.create_task(_abandon_after_grace(runtime))
    runtime.abandon_task = task
    _finalizer_tasks.add(task)
    task.add_done_callback(_finalizer_tasks.discard)


async def _abandon_after_grace(runtime: _LiveRuntime) -> None:
    try:
        await asyncio.sleep(_reconnect_grace_sec())
    except asyncio.CancelledError:
        return
    if not runtime.detached or _runtimes.get(runtime.session_id) is not runtime:
        return
    runtime.abandon_task = None
    runtime.detached = False
    runtime.finalizer_started = True
    runtime.stop_reason = "ended"
    runtime.error_message = runtime.error_message or "连接中断后超时未重连，实时转写会话已自动结束"
    logger.info("live session %s reconnect grace expired, ending", runtime.session_id)
    await _end_connection(runtime, detach=False)


# ------------------------------------------------------------------ WS handler
async def handle_ws_stream(websocket: Any, session_id: int) -> None:
    """Duplex live-transcription WebSocket endpoint (GA: resumable sessions).

    A socket close without an explicit ``{"type":"stop"}`` keeps the session
    ``active`` for ``RECONNECT_GRACE_SEC``; reconnecting to the same
    ``/api/live/sessions/{id}/stream`` URL resumes at the last audio position
    (continuous timestamps, no duplicated final segments). An explicit stop,
    cancel, max-duration or provider failure finalizes the session as before.
    """
    await websocket.accept()
    if not settings.live_enabled:
        await _close_ws(websocket, 4403)
        return
    user_id = _ws_user_id(websocket)
    if user_id is None:
        await _close_ws(websocket, 4403)
        return

    provider_name = "mock"
    model_name = ""
    language = "zh"
    max_audio_ms = _max_audio_ms()
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None or getattr(user, "disabled", False):
            await _close_ws(websocket, 4403)
            return
        session = db.get(LiveSession, session_id)
        if session is None:
            await _close_ws(websocket, 4404)
            return
        if not (is_admin(user) or session.user_id == user_id):
            await _close_ws(websocket, 4403)
            return
        if session.status != "active":
            await _close_ws(websocket, 4409)
            return
        provider_name = session.provider or "mock"
        model_name = session.model or ""
        language = session.language or "zh"

    # A previous connection may still be detaching (the socket just closed);
    # give it a short grace window so an immediate reconnect resumes instead of
    # being rejected as a second producer.
    runtime = _runtimes.get(session_id)
    if runtime is not None and not runtime.detached and not runtime.finalized:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.5, min(2.0, _reconnect_grace_sec()))
        while True:
            runtime = _runtimes.get(session_id)
            if runtime is None or runtime.detached or runtime.finalized:
                break
            if runtime.detaching:
                try:
                    await asyncio.wait_for(
                        runtime.detached_event.wait(),
                        timeout=max(0.05, deadline - loop.time()),
                    )
                except asyncio.TimeoutError:
                    pass
                runtime = _runtimes.get(session_id)
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.05)
    if runtime is not None and (not runtime.detached or runtime.finalized):
        await _close_ws(websocket, 4409)  # another producer is still attached
        return
    if not _session_active(session_id):
        await _close_ws(websocket, 4409)
        return

    resumed = runtime is not None
    if runtime is None:
        base_ms, last_end_ms, _ = _session_resume_state(session_id)
        runtime = _LiveRuntime(
            session_id=session_id,
            websocket=websocket,
            provider=None,
            max_audio_ms=max_audio_ms,
        )
        runtime.total_audio_ms = base_ms
        runtime.last_final_end_ms = last_end_ms
        runtime.detached_event = asyncio.Event()
    else:
        _cancel_abandon(runtime)
        base_ms = int(runtime.total_audio_ms or 0)
        _reset_connection(runtime, websocket, max_audio_ms=max_audio_ms)
    runtime.audio_base_ms = base_ms
    resumed = resumed or base_ms > 0

    # Build + open the upstream provider for this connection segment.
    provider: LiveASRProvider | None = None
    provider_error: str | None = None
    with SessionLocal() as db:
        try:
            provider = build_live_provider(db, provider_name, model_name, language=language)
        except ProviderError as exc:
            provider_error = exc.message
    if provider is None:
        message = provider_error or "实时转写服务不可用"
        await _send_json_raw(websocket, {"type": "error", "message": message, "code": "PROVIDER_ERROR"})
        _mark_session_failed(session_id, message)
        await _send_json_raw(
            websocket, {"type": "done", "total_ms": int(runtime.total_audio_ms or 0), "status": "failed"}
        )
        await _close_ws(websocket, 1011)
        return

    start_error: str | None = None
    try:
        await provider.start(language=language, resume_from_ms=base_ms)
    except ProviderError as exc:
        start_error = exc.message
    except Exception as exc:  # noqa: BLE001
        logger.exception("live session %s: provider start failed", session_id)
        start_error = f"实时转写服务启动失败（{type(exc).__name__}）"
    if start_error is not None:
        try:
            await asyncio.wait_for(provider.close(), timeout=15)
        except Exception:  # noqa: BLE001 - best effort
            pass
        await _send_json_raw(websocket, {"type": "error", "message": start_error, "code": "PROVIDER_ERROR"})
        _mark_session_failed(session_id, start_error)
        await _send_json_raw(
            websocket, {"type": "done", "total_ms": int(runtime.total_audio_ms or 0), "status": "failed"}
        )
        await _close_ws(websocket, 1011)
        return
    runtime.provider = provider

    # Re-check after the awaits: a stop/cancel or a competing producer may have won.
    current = _runtimes.get(session_id)
    if current is not None and current is not runtime:
        await _close_provider(runtime)
        await _close_ws(websocket, 4409)
        return
    if not _session_active(session_id):
        await _close_provider(runtime)
        await _close_ws(websocket, 4409)
        return

    _runtimes[session_id] = runtime
    runtime.connection_index += 1
    logger.info(
        "live session %s stream started (provider=%s, language=%s, resumed=%s, base=%sms, connection=%s)",
        session_id,
        provider_name,
        language,
        resumed,
        base_ms,
        runtime.connection_index,
    )
    await _send_json(
        runtime,
        {
            "type": "ready",
            "session_id": session_id,
            "sample_rate": SAMPLE_RATE,
            "format": "pcm_s16le",
            "max_ms": runtime.max_audio_ms,
            "resumed": resumed,
            "base_ms": int(base_ms),
            "total_ms": int(runtime.total_audio_ms or 0),
            "connection": int(runtime.connection_index),
        },
    )
    runtime.reader_task = asyncio.create_task(_client_reader(runtime))
    runtime.feeder_task = asyncio.create_task(_audio_feeder(runtime))
    runtime.pump_task = asyncio.create_task(_pump_provider_events(runtime))
    try:
        await _message_loop(runtime)
    except asyncio.CancelledError:
        # ASGI server cancelled the handler (client vanished): treat it as a
        # resumable disconnect unless an explicit stop/failure already won.
        if runtime.stop_reason not in ("cancelled", "failed"):
            runtime.stop_reason = "disconnect"
        raise
    except Exception:  # noqa: BLE001 - never leak a traceback to the client
        logger.exception("live session %s: message loop failed", session_id)
        runtime.stop_reason = "failed"
        runtime.error_message = runtime.error_message or "实时转写处理失败"
        runtime.close_code = 1011
    finally:
        detach = runtime.stop_reason == "disconnect" and _session_active(session_id)
        ender = _schedule_end(runtime, detach=detach)
        if ender is not None:
            try:
                await asyncio.shield(ender)
            except asyncio.CancelledError:
                # The detached ender keeps persisting/detaching in the background.
                logger.info(
                    "live session %s: handler cancelled, connection end continues in background",
                    session_id,
                )
                raise


# ------------------------------------------------------------- HTTP lifecycle
async def stop_live_session(session_id: int, *, cancel: bool = False) -> dict[str, Any] | None:
    """Idempotently finish a session (attached, detached/resumable, or DB-only)."""
    reason = "cancelled" if cancel else "ended"
    runtime = _runtimes.get(session_id)
    if runtime is not None and runtime.detaching and not runtime.detached and not runtime.finalized:
        try:
            await asyncio.wait_for(runtime.detached_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass
        runtime = _runtimes.get(session_id)
    if runtime is not None and runtime.detached and not runtime.finalized:
        # Disconnected but still resumable: end it now instead of waiting for grace.
        runtime.stop_reason = reason
        if cancel:
            runtime.error_message = None
        _cancel_abandon(runtime)
        ender = _schedule_end(runtime, detach=False)
        if ender is not None:
            try:
                await asyncio.wait_for(asyncio.shield(ender), timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning("live session %s: stop timed out on detached runtime", session_id)
            try:
                await asyncio.wait_for(runtime.done_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
    elif runtime is not None and not runtime.finalized:
        runtime.stop_reason = reason
        if cancel:
            runtime.error_message = None
        if not cancel:
            runtime.close_code = 1000
        runtime.stop_event.set()
        try:
            await asyncio.wait_for(runtime.done_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("live session %s: stop request timed out waiting for stream cleanup", session_id)
    with SessionLocal() as db:
        session = db.get(LiveSession, session_id)
        if session is None:
            return None
        if session.status == "active":
            session.status = "cancelled" if cancel else "ended"
            session.ended_at = utcnow()
            db.commit()
        return {
            "status": session.status,
            "total_audio_ms": int(session.total_audio_ms or 0),
            "recording_id": session.recording_id,
        }


async def cancel_live_session(session_id: int) -> dict[str, Any] | None:
    return await stop_live_session(session_id, cancel=True)


__all__ = [
    "ALLOWED_MERGE_MODES",
    "ALLOWED_PROVIDERS",
    "BYTES_PER_MS",
    "SAMPLE_RATE",
    "active_session_ids",
    "build_live_provider",
    "cancel_live_session",
    "cleanup_stale_sessions",
    "create_live_session",
    "get_session_for_user",
    "handle_ws_stream",
    "list_live_segments",
    "live_features",
    "RECONNECT_GRACE_SEC",
    "resolve_live_provider",
    "segment_out_payload",
    "session_out_payload",
    "stop_live_session",
]
