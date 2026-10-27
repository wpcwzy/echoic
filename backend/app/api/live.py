"""Live transcription API (R-19, v0.3.10 GA) — docs/10 §4.1.

Routes (mounted at ``/api/live`` by ``app.main``):

* ``POST /sessions`` — create an active session (403 LIVE_DISABLED when off)
* ``GET  /sessions/{id}`` — session status + finalized segments
* ``GET  /sessions/{id}/segments`` — finalized segments only
* ``POST /sessions/{id}/stop`` — idempotent graceful stop
* ``POST /sessions/{id}/cancel`` — cancel/discard the session
* ``WS   /sessions/{id}/stream`` — duplex PCM16LE 16 kHz audio + JSON captions

HTTP endpoints require a logged-in owner (or admin); the WebSocket also accepts
``?token=<jwt>`` / ``Authorization: Bearer`` for non-browser clients.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, WebSocket
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import (
    LiveSegmentOut,
    LiveSessionCreateIn,
    LiveSessionDetailOut,
    LiveSessionOut,
)
from ..services import live as live_service

logger = logging.getLogger(__name__)

router = APIRouter()


def _session_out(session) -> LiveSessionOut:
    return LiveSessionOut.model_validate(live_service.session_out_payload(session))


def _segment_out(segment) -> LiveSegmentOut:
    return LiveSegmentOut.model_validate(live_service.segment_out_payload(segment))


@router.post("/sessions", response_model=LiveSessionOut, status_code=201)
def create_session(
    payload: LiveSessionCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> LiveSessionOut:
    session = live_service.create_live_session(
        db,
        user,
        language=payload.language,
        merge_mode=payload.merge_mode,
    )
    logger.info("live session %s created by user %s (provider=%s)", session.id, user.id, session.provider)
    return _session_out(session)


@router.get("/sessions/{session_id}", response_model=LiveSessionDetailOut)
def get_session(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> LiveSessionDetailOut:
    session = live_service.get_session_for_user(db, user, session_id)
    segments = [_segment_out(item) for item in live_service.list_live_segments(db, session_id)]
    return LiveSessionDetailOut(session=_session_out(session), segments=segments)


@router.get("/sessions/{session_id}/segments")
def get_session_segments(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    live_service.get_session_for_user(db, user, session_id)
    segments = [
        _segment_out(item).model_dump(mode="json")
        for item in live_service.list_live_segments(db, session_id)
    ]
    return {"segments": segments}


@router.post("/sessions/{session_id}/stop")
async def stop_session(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    live_service.get_session_for_user(db, user, session_id)
    result = await live_service.stop_live_session(session_id)
    if result is None:  # pragma: no cover - defensive, permission already checked
        result = {"status": "ended", "total_audio_ms": 0, "recording_id": None}
    return {"ok": True, **result}


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    live_service.get_session_for_user(db, user, session_id)
    result = await live_service.cancel_live_session(session_id)
    if result is None:  # pragma: no cover - defensive
        result = {"status": "cancelled", "total_audio_ms": 0, "recording_id": None}
    return {"ok": True, **result}


@router.websocket("/sessions/{session_id}/stream")
async def stream_session(websocket: WebSocket, session_id: int) -> None:
    await live_service.handle_ws_stream(websocket, session_id)
