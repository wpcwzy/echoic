"""Mock RAG chat tests (SSE event dicts, citations, persistence, errors)."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.db import Base, SessionLocal, init_db
from app.models import ChatMessage, Recording, TranscriptSegment
from app.providers.demo_fixtures import demo_recordings, load_demo_data
from app.services.chat import stream_chat
from app.services.demo import seed_demo


@pytest.fixture()
def database(db):
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    yield db


@pytest.fixture()
def demo_library(database, monkeypatch):
    if load_demo_data() is None:
        pytest.skip("app/demo_data.json 不可用")
    # Audio synthesis is not needed for chat tests and would slow them down.
    monkeypatch.setattr("app.services.demo._generate_demo_audio", lambda recording, generate: None)
    created = seed_demo(database)
    if not created:
        pytest.skip("demo data already present / could not seed")
    with SessionLocal() as db:
        recording = db.scalars(
            select(Recording).where(Recording.title.like("TraceOpt%")).order_by(Recording.id)
        ).first()
    assert recording is not None
    return int(recording.id)


async def _collect(db_factory, recording_id: int, question: str) -> list[dict]:
    events: list[dict] = []
    async for event in stream_chat(db_factory, recording_id, question):
        events.append(event)
    return events


async def test_stream_chat_events_citations_and_persistence(demo_library):
    recording_id = demo_library
    events = await _collect(SessionLocal, recording_id, "这次会议定了哪些交付节点？")
    types = [event["type"] for event in events]
    assert types[0] == "start"
    assert "delta" in types
    assert "done" in types
    assert types[-1] == "done"

    start = events[0]
    assert isinstance(start["message_id"], int) and start["message_id"] > 0
    done = events[-1]
    assert done["message_id"] == start["message_id"]

    answer = "".join(event["text"] for event in events if event["type"] == "delta")
    assert answer.strip()
    assert "两周" in answer or "三周" in answer

    citation_events = [event for event in events if event["type"] == "citations"]
    assert len(citation_events) == 1
    citations = citation_events[0]["citations"]
    assert 3 <= len(citations) <= 5
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        duration_ms = int(recording.duration_ms)
        messages = db.scalars(
            select(ChatMessage)
            .where(ChatMessage.recording_id == recording_id)
            .order_by(ChatMessage.id)
        ).all()
        assert len(messages) == 2
        user_message, assistant_message = messages
        assert user_message.role == "user"
        assert user_message.content == "这次会议定了哪些交付节点？"
        assert assistant_message.role == "assistant"
        assert assistant_message.content == answer
        assert assistant_message.citations_json
        stored_citations = json.loads(assistant_message.citations_json)
        assert stored_citations == citations

    for citation in citations:
        assert citation["recording_id"] == recording_id
        assert 0 <= citation["start_ms"] < citation["end_ms"] <= duration_ms
        assert citation["speaker"]
        assert citation["text"]
        assert len(citation["text"]) <= 120 or citation["text"].endswith("…")


async def test_stream_chat_keeps_history(demo_library):
    recording_id = demo_library
    first = await _collect(SessionLocal, recording_id, "ICPE论文怎么分工？")
    assert first[-1]["type"] == "done"
    second = await _collect(SessionLocal, recording_id, "实验评估维度定了没？")
    assert second[-1]["type"] == "done"
    with SessionLocal() as db:
        messages = db.scalars(
            select(ChatMessage)
            .where(ChatMessage.recording_id == recording_id)
            .order_by(ChatMessage.id)
        ).all()
    assert [message.role for message in messages] == ["user", "assistant", "user", "assistant"]
    assert all(message.content.strip() for message in messages[:-1])


async def test_stream_chat_error_for_unfinished_recording(database):
    unique = uuid.uuid4().hex
    with SessionLocal() as db:
        recording = Recording(
            uuid=unique,
            title="处理中的录音",
            status="processing",
            storage_dir=f"media/{unique}",
        )
        db.add(recording)
        db.commit()
        recording_id = int(recording.id)
    events = await _collect(SessionLocal, recording_id, "讲的是什么？")
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "尚未处理完成" in events[0]["message"]


async def test_stream_chat_error_for_missing_recording(database):
    events = await _collect(SessionLocal, 999_999, "讲的是什么？")
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "不存在" in events[0]["message"]


def test_demo_chat_examples_cover_expected_question():
    """The fixture must contain an answer mentioning the demo delivery timeline."""
    data = load_demo_data() or {}
    recordings = data.get("recordings") or []
    assert recordings, "demo_data.json 应包含演示录音"
    traceopt = recordings[0]
    answers = list((traceopt.get("chat_examples") or {}).values())
    assert any("两周" in answer or "三周" in answer for answer in answers)
