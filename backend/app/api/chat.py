"""RAG chat endpoints (docs/API.md §4 + §7 SSE protocol)."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..core.errors import bad_request
from ..core.utils import load_json
from ..db import SessionLocal, get_db
from ..deps import get_current_user
from ..models import ChatMessage
from ..schemas import (
    ChatHistoryOut,
    ChatIn,
    ChatMessageOut,
    CitationOut,
    OkOut,
)
from ._common import get_recording_or_404

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _citations_from(raw: str | None) -> list[CitationOut]:
    data = load_json(raw, [])
    citations: list[CitationOut] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                citations.append(CitationOut.model_validate(item))
            except Exception:  # skip malformed citation rows
                continue
    return citations


@router.get("/{recording_id}/chat", response_model=ChatHistoryOut)
def get_chat_history(recording_id: int, db: Session = Depends(get_db)):
    get_recording_or_404(db, recording_id)
    rows = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.recording_id == recording_id)
            .order_by(ChatMessage.id)
        ).all()
    )
    messages: list[ChatMessageOut] = []
    for row in rows:
        role = row.role if row.role in ("user", "assistant") else "assistant"
        messages.append(
            ChatMessageOut(
                id=row.id,
                role=role,
                content=row.content or "",
                citations=_citations_from(row.citations_json),
                created_at=row.created_at,
            )
        )
    return ChatHistoryOut(messages=messages)


@router.delete("/{recording_id}/chat", response_model=OkOut)
def clear_chat_history(recording_id: int, db: Session = Depends(get_db)):
    get_recording_or_404(db, recording_id)
    db.execute(delete(ChatMessage).where(ChatMessage.recording_id == recording_id))
    db.commit()
    return OkOut()


@router.post("/{recording_id}/chat")
async def chat_stream(
    recording_id: int,
    payload: ChatIn,
    db: Session = Depends(get_db),
):
    recording = get_recording_or_404(db, recording_id)
    message = payload.message.strip()
    if not message:
        raise bad_request("消息不能为空", "VALIDATION_ERROR")

    async def event_source():
        try:
            from ..services.chat import stream_chat
        except Exception:  # pragma: no cover - services/chat.py lands with backend-ai
            logger.exception("services.chat 不可用，无法回答 recording=%s", recording_id)
            yield _sse({"type": "error", "message": "AI 问答服务尚未就绪，请稍后重试"})
            return
        try:
            async for event in stream_chat(SessionLocal, recording.id, message):
                if isinstance(event, dict):
                    yield _sse(event)
        except asyncio.CancelledError:
            logger.info("chat stream cancelled by client recording=%s", recording_id)
            raise
        except Exception as exc:  # provider failures must reach the UI as SSE events
            logger.exception("chat stream failed recording=%s", recording_id)
            yield _sse({"type": "error", "message": f"生成回答失败：{exc}"})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
