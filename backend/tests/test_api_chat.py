"""API tests: chat history + SSE streaming (docs/API.md §4/§7)."""

from __future__ import annotations

import sys
import types

import pytest

from app.db import SessionLocal
from tests.test_api_helpers import make_chat_message, seed_ready_recording


def install_fake_stream(monkeypatch, events=None, error=None):
    """Patch ``app.services.chat.stream_chat`` (owned by backend-ai) with a fake."""
    calls: list[tuple] = []

    async def fake_stream(db_factory, recording_id, message):
        calls.append((db_factory, recording_id, message))
        for event in events or []:
            yield event
        if error is not None:
            raise error

    try:
        import app.services.chat as chat_module  # type: ignore[import-not-found]
    except Exception:
        chat_module = types.ModuleType("app.services.chat")
        sys.modules["app.services.chat"] = chat_module
        import app.services as services_package

        setattr(services_package, "chat", chat_module)
    monkeypatch.setattr(chat_module, "stream_chat", fake_stream, raising=False)
    return calls


def _events(recording_id: int):
    return [
        {"type": "start", "message_id": 42},
        {"type": "delta", "text": "根据转录稿，"},
        {"type": "delta", "text": "交付节点有两个。"},
        {
            "type": "citations",
            "citations": [
                {
                    "recording_id": recording_id,
                    "segment_id": 12,
                    "start_ms": 385_000,
                    "end_ms": 392_000,
                    "speaker": "吴克强",
                    "text": "预计还有两周时间完成",
                }
            ],
        },
        {"type": "done", "message_id": 42},
    ]


def test_chat_sse_stream(auth_client, db, monkeypatch):
    recording = seed_ready_recording(db, with_transcript=True)
    calls = install_fake_stream(monkeypatch, events=_events(recording.id))

    response = auth_client.post(
        f"/api/recordings/{recording.id}/chat",
        json={"message": "这次会议定了哪些交付节点？"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    from tests.test_api_helpers import parse_sse

    events = parse_sse(response.text)
    assert [event["type"] for event in events] == [
        "start",
        "delta",
        "delta",
        "citations",
        "done",
    ]
    assert events[0]["message_id"] == 42
    assert events[1]["text"] + events[2]["text"] == "根据转录稿，交付节点有两个。"
    citation = events[3]["citations"][0]
    assert citation["recording_id"] == recording.id
    assert citation["start_ms"] == 385_000
    assert citation["speaker"] == "吴克强"

    assert len(calls) == 1
    db_factory, recording_id, message = calls[0]
    assert db_factory is SessionLocal
    assert recording_id == recording.id
    assert message == "这次会议定了哪些交付节点？"


def test_chat_sse_error_event(auth_client, db, monkeypatch):
    recording = seed_ready_recording(db)
    install_fake_stream(
        monkeypatch,
        events=[{"type": "start", "message_id": 7}, {"type": "error", "message": "服务商不可用"}],
    )
    response = auth_client.post(
        f"/api/recordings/{recording.id}/chat", json={"message": "你好"}
    )
    assert response.status_code == 200
    from tests.test_api_helpers import parse_sse

    events = parse_sse(response.text)
    assert events[-1] == {"type": "error", "message": "服务商不可用"}


def test_chat_sse_stream_exception_becomes_error_event(auth_client, db, monkeypatch):
    recording = seed_ready_recording(db)
    install_fake_stream(
        monkeypatch, events=[{"type": "start", "message_id": 1}], error=RuntimeError("boom")
    )
    response = auth_client.post(
        f"/api/recordings/{recording.id}/chat", json={"message": "你好"}
    )
    assert response.status_code == 200
    from tests.test_api_helpers import parse_sse

    events = parse_sse(response.text)
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "error"
    assert "生成回答失败" in events[-1]["message"]


def test_chat_missing_recording(auth_client, monkeypatch):
    install_fake_stream(monkeypatch, events=[])
    response = auth_client.post("/api/recordings/9999/chat", json={"message": "你好"})
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_chat_message_validation(auth_client, db, monkeypatch):
    recording = seed_ready_recording(db)
    install_fake_stream(monkeypatch, events=[])
    response = auth_client.post(f"/api/recordings/{recording.id}/chat", json={"message": ""})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_chat_requires_auth(client):
    assert client.post("/api/recordings/1/chat", json={"message": "hi"}).status_code == 401
    assert client.get("/api/recordings/1/chat").status_code == 401
    assert client.delete("/api/recordings/1/chat").status_code == 401


def test_chat_history_and_clear(auth_client, db):
    recording = seed_ready_recording(db)
    make_chat_message(
        db,
        recording,
        role="user",
        content="这次会议定了哪些交付节点？",
    )
    make_chat_message(
        db,
        recording,
        role="assistant",
        content="交付节点有两个。",
        citations=[
            {
                "recording_id": recording.id,
                "segment_id": 1,
                "start_ms": 1000,
                "end_ms": 2000,
                "speaker": "吴克强",
                "text": "两周后交付",
            }
        ],
    )
    history = auth_client.get(f"/api/recordings/{recording.id}/chat")
    assert history.status_code == 200
    messages = history.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["citations"][0]["start_ms"] == 1000
    assert messages[0]["created_at"]

    cleared = auth_client.delete(f"/api/recordings/{recording.id}/chat")
    assert cleared.status_code == 200
    assert cleared.json()["ok"] is True
    assert auth_client.get(f"/api/recordings/{recording.id}/chat").json()["messages"] == []


def test_chat_history_missing_recording(auth_client):
    assert auth_client.get("/api/recordings/9999/chat").status_code == 404
