"""Live transcription HTTP + WebSocket API tests (R-19 Beta, mock provider).

No network: ``ECHOIC_LIVE_PROVIDER`` is forced to ``mock`` and the deterministic
``MockRealtimeProvider`` is used. ``TestClient.websocket_connect`` streams binary
PCM16LE silence frames; ``queue.enqueue`` / pipeline are not involved.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from fastapi.websockets import WebSocketDisconnect
from sqlalchemy import select

from app.config import settings
from app.core.errors import ProviderError
from app.core.security import create_access_token
from app.core.utils import utcnow
from app.db import SessionLocal
from app.models import LiveSegment, LiveSession, User
from app.services import live as live_service
from app.services.live import cleanup_stale_sessions

PCM_FRAME = b"\x00\x00" * 1600  # 3200 bytes = 100 ms @ 16 kHz mono PCM16LE


# --------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _reset_live_runtime():
    live_service._runtimes.clear()
    yield
    # Drop any runtime left behind by a failing test (portal is closing anyway).
    live_service._runtimes.clear()


@pytest.fixture()
def database(db):
    from app.db import Base, init_db

    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    yield db


@pytest.fixture()
def live_on(monkeypatch):
    monkeypatch.setattr(settings, "live_enabled", True)
    monkeypatch.setattr(settings, "live_provider", "mock")
    monkeypatch.setattr(settings, "live_merge_mode", "replace")
    monkeypatch.setattr(settings, "live_max_minutes", 60)
    yield


def _drop_client_login(client) -> None:
    """Remove the admin cookie/header so per-request member auth is actually used."""
    try:
        client.cookies.delete("echoic_token")
    except Exception:  # pragma: no cover - cookie jar variations
        client.cookies.clear()
    client.headers.pop("Authorization", None)


def _token_of(auth_client) -> str:
    header = auth_client.headers.get("Authorization")
    assert header and header.lower().startswith("bearer ")
    return header[7:].strip()


def _create_session(auth_client, token: str | None = None, **payload) -> dict:
    response = auth_client.post("/api/live/sessions", json=payload or {})
    assert response.status_code == 201, response.text
    return response.json()


def _ws_url(session_id: int, token: str) -> str:
    return f"/api/live/sessions/{session_id}/stream?token={token}"


def _drain_until(ws, predicate, limit: int = 400) -> list[dict]:
    events: list[dict] = []
    for _ in range(limit):
        event = ws.receive_json()
        events.append(event)
        if predicate(event):
            return events
    raise AssertionError(f"event not received, got: {[e.get('type') for e in events][-20:]}")


def _create_member(username: str) -> tuple[int, str]:
    with SessionLocal() as db:
        user = User(
            username=username,
            password_hash="not-used",
            display_name=username,
            is_admin=False,
            role="member",
            disabled=False,
        )
        db.add(user)
        db.commit()
        user_id = int(user.id)
    return user_id, create_access_token(user_id)


def _finals(db, session_id: int) -> list[LiveSegment]:
    return list(
        db.scalars(
            select(LiveSegment)
            .where(LiveSegment.session_id == session_id)
            .order_by(LiveSegment.idx)
        ).all()
    )


# ------------------------------------------------------------------ HTTP tests
def test_create_session_shape(auth_client, live_on):
    session = _create_session(auth_client, language="zh")
    assert session["status"] == "active"
    assert session["provider"] == "mock"
    assert session["model"] == "mock-realtime"
    assert session["merge_mode"] == "replace"
    assert session["ws_url"] == f"/api/live/sessions/{session['id']}/stream"
    assert session["uuid"]
    assert session["started_at"]
    assert session["total_audio_ms"] == 0

    token = _token_of(auth_client)
    detail = auth_client.get(f"/api/live/sessions/{session['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["session"]["id"] == session["id"]
    assert body["segments"] == []
    segments = auth_client.get(f"/api/live/sessions/{session['id']}/segments")
    assert segments.status_code == 200
    assert segments.json() == {"segments": []}


def test_create_session_promote_mode(auth_client, live_on):
    session = _create_session(auth_client, merge_mode="promote")
    assert session["merge_mode"] == "promote"
    invalid = auth_client.post("/api/live/sessions", json={"merge_mode": "keep_live"})
    assert invalid.status_code == 422


def test_live_disabled_returns_403(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "live_enabled", False)
    response = auth_client.post("/api/live/sessions", json={})
    assert response.status_code == 403
    assert response.json()["code"] == "LIVE_DISABLED"
    assert "实时" in response.json()["detail"]


def test_session_not_visible_to_other_member_admin_sees_all(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    _, member_token = _create_member("member-a")
    member_headers = {"Authorization": f"Bearer {member_token}"}
    # The login cookie takes precedence in get_current_user; drop it so the
    # per-request member bearer token is what the endpoint sees.
    auth_client.cookies.delete("echoic_token")
    response = auth_client.get(f"/api/live/sessions/{session['id']}", headers=member_headers)
    assert response.status_code == 404
    response = auth_client.post(f"/api/live/sessions/{session['id']}/stop", headers=member_headers)
    assert response.status_code == 404
    # The admin can still access it.
    assert auth_client.get(f"/api/live/sessions/{session['id']}").status_code == 200


def test_stop_is_idempotent_without_stream(auth_client, live_on):
    session = _create_session(auth_client)
    first = auth_client.post(f"/api/live/sessions/{session['id']}/stop")
    assert first.status_code == 200
    assert first.json() == {"ok": True, "status": "ended", "total_audio_ms": 0, "recording_id": None}
    second = auth_client.post(f"/api/live/sessions/{session['id']}/stop")
    assert second.status_code == 200
    assert second.json()["status"] == "ended"
    detail = auth_client.get(f"/api/live/sessions/{session['id']}").json()
    assert detail["session"]["status"] == "ended"
    assert detail["session"]["ended_at"]


# --------------------------------------------------------------- WS happy path
def test_websocket_stream_ready_partial_final_done(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    session_id = session["id"]
    events: list[dict] = []
    with auth_client.websocket_connect(_ws_url(session_id, token)) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["sample_rate"] == 16000
        assert ready["format"] == "pcm_s16le"
        events.append(ready)
        for _ in range(40):  # 4000 ms of silence
            ws.send_bytes(PCM_FRAME)
        received = _drain_until(ws, lambda event: event["type"] == "final")
        events.extend(received)
        ws.send_json({"type": "stop"})
        events.extend(_drain_until(ws, lambda event: event["type"] == "done"))

    types = [event["type"] for event in events]
    assert types[0] == "ready"
    assert "partial" in types
    assert types[-1] == "done"
    assert types.count("final") >= 1
    partials = [event for event in events if event["type"] == "partial"]
    for partial in partials:
        assert partial["text"]
        assert 0 <= partial["start_ms"] < partial["end_ms"]
    finals = [event for event in events if event["type"] == "final"]
    for final in finals:
        assert final["text"]
        assert final["segment_id"] > 0
        assert final["start_ms"] < final["end_ms"]
    done = events[-1]
    assert done["total_ms"] == 4000
    assert done["status"] == "ended"

    with SessionLocal() as db:
        row = db.get(LiveSession, session_id)
        assert row.status == "ended"
        assert row.total_audio_ms == 4000
        assert row.ended_at is not None
        segments = _finals(db, session_id)
        assert len(segments) == len(finals)
        assert all(segment.is_final for segment in segments)
        assert [segment.idx for segment in segments] == list(range(len(segments)))
        assert segments[0].start_ms == 0
        assert segments[-1].end_ms > 0

    detail = auth_client.get(f"/api/live/sessions/{session_id}").json()
    assert detail["session"]["status"] == "ended"
    assert len(detail["segments"]) == len(finals)
    segments_payload = auth_client.get(f"/api/live/sessions/{session_id}/segments").json()
    assert len(segments_payload["segments"]) == len(finals)


def test_websocket_cookie_auth(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    with auth_client.websocket_connect(
        f"/api/live/sessions/{session['id']}/stream",
        headers={"cookie": f"echoic_token={token}"},
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["type"] == "done"


def test_websocket_honours_start_message(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "start", "sample_rate": 16000, "channels": 1})
        ws.send_bytes(PCM_FRAME)
        ws.send_json({"type": "stop"})
        events = _drain_until(ws, lambda event: event["type"] == "done")
    assert not any(event["type"] == "error" for event in events)


def test_member_can_create_and_stream_own_session(auth_client, live_on):
    _, member_token = _create_member("member-b")
    headers = {"Authorization": f"Bearer {member_token}"}
    _drop_client_login(auth_client)
    response = auth_client.post("/api/live/sessions", json={}, headers=headers)
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    with auth_client.websocket_connect(_ws_url(session_id, member_token)) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(PCM_FRAME)
        ws.send_json({"type": "stop"})
        _drain_until(ws, lambda event: event["type"] == "done")


# --------------------------------------------------------------- WS auth errors
def test_websocket_requires_token(auth_client, live_on):
    session = _create_session(auth_client)
    _drop_client_login(auth_client)
    with auth_client.websocket_connect(f"/api/live/sessions/{session['id']}/stream") as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4403


def test_websocket_non_owner_rejected(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    _, member_token = _create_member("member-c")
    auth_client.cookies.delete("echoic_token")
    with auth_client.websocket_connect(_ws_url(session["id"], member_token)) as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4403


def test_websocket_unknown_session(auth_client, live_on):
    token = _token_of(auth_client)
    with auth_client.websocket_connect(_ws_url(999_999, token)) as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4404


def test_websocket_live_disabled_closes_4403(auth_client, live_on, monkeypatch):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    monkeypatch.setattr(settings, "live_enabled", False)
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4403


def test_second_producer_rejected_and_first_still_works(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as first:
        assert first.receive_json()["type"] == "ready"
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with auth_client.websocket_connect(_ws_url(session["id"], token)) as second:
                second.receive_json()
        assert excinfo.value.code == 4409
        first.send_json({"type": "stop"})
        assert first.receive_json()["type"] == "done"


def test_stream_after_ended_rejected(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    assert auth_client.post(f"/api/live/sessions/{session['id']}/stop").status_code == 200
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4409


# ------------------------------------------------------------------ edge cases
def test_max_duration_guard(auth_client, live_on, monkeypatch):
    monkeypatch.setattr(live_service, "_max_audio_ms", lambda: 2000)
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as ws:
        assert ws.receive_json()["type"] == "ready"
        for _ in range(30):  # 3000 ms > 2000 ms cap
            ws.send_bytes(PCM_FRAME)
        events = _drain_until(ws, lambda event: event["type"] == "done")
    errors = [event for event in events if event["type"] == "error"]
    assert errors and errors[0]["code"] == "LIVE_MAX_DURATION"
    done = events[-1]
    assert done["status"] == "ended"
    assert done["total_ms"] >= 2000
    with SessionLocal() as db:
        row = db.get(LiveSession, session["id"])
        assert row.status == "ended"
        assert "上限" in (row.error or "")


def test_backpressure_guard(auth_client, live_on, monkeypatch):
    monkeypatch.setattr(live_service, "_max_pending_audio_ms", lambda: 50)
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(PCM_FRAME)  # 100 ms queued while the limit is 50 ms
        events = _drain_until(ws, lambda event: event["type"] == "done")
        errors = [event for event in events if event["type"] == "error"]
        assert errors and errors[0]["code"] == "LIVE_BACKPRESSURE"
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4413
    with SessionLocal() as db:
        assert db.get(LiveSession, session["id"]).status == "ended"


def test_bad_frame_alignment_is_non_fatal(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\x00")  # odd byte length
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "BAD_FRAME"
        ws.send_bytes(PCM_FRAME)
        ws.send_json({"type": "stop"})
        events = _drain_until(ws, lambda event: event["type"] == "done")
    assert events[-1]["status"] == "ended"


def test_unsupported_sample_rate_fails_session(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "start", "sample_rate": 8000, "channels": 1})
        events = _drain_until(ws, lambda event: event["type"] == "done")
    errors = [event for event in events if event["type"] == "error"]
    assert errors and errors[0]["code"] == "UNSUPPORTED_FORMAT"
    with SessionLocal() as db:
        row = db.get(LiveSession, session["id"])
        assert row.status == "failed"
        assert row.error


def test_cancel_endpoint_marks_session_cancelled(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(PCM_FRAME)
        cancel = auth_client.post(f"/api/live/sessions/{session['id']}/cancel")
        assert cancel.status_code == 200, cancel.text
        body = cancel.json()
        assert body["ok"] is True
        assert body["status"] == "cancelled"
        events = _drain_until(ws, lambda event: event["type"] == "done")
    assert events[-1]["status"] == "cancelled"
    detail = auth_client.get(f"/api/live/sessions/{session['id']}").json()
    assert detail["session"]["status"] == "cancelled"
    # Cancel is idempotent too.
    again = auth_client.post(f"/api/live/sessions/{session['id']}/cancel")
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"


def _wait_detached(session_id: int, timeout: float = 5.0) -> None:
    """Wait for the server side to release the previous WS and become resumable."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        runtime = live_service._runtimes.get(session_id)
        if runtime is None or runtime.detached:
            return
        time.sleep(0.02)
    raise AssertionError("live session was never detached for resume")


def _db_finals(session_id: int) -> list[LiveSegment]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(LiveSegment)
                .where(LiveSegment.session_id == session_id)
                .order_by(LiveSegment.idx)
            ).all()
        )


def test_disconnect_keeps_session_resumable_and_resume_continues(auth_client, live_on):
    """GA: socket close without stop keeps the session; reconnect resumes with continuous finals."""
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    session_id = session["id"]
    url = _ws_url(session_id, token)

    with auth_client.websocket_connect(url) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["resumed"] is False
        assert ready["connection"] == 1
        for _ in range(40):  # 4000 ms, first final at 3200 ms
            ws.send_bytes(PCM_FRAME)
        first_events = _drain_until(ws, lambda event: event["type"] == "final")
    first_finals = [event for event in first_events if event["type"] == "final"]
    assert first_finals

    _wait_detached(session_id)
    detail = auth_client.get(f"/api/live/sessions/{session_id}").json()
    assert detail["session"]["status"] == "active", "disconnect must stay resumable during grace"
    assert detail["session"]["ended_at"] is None

    with auth_client.websocket_connect(url) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["resumed"] is True
        assert ready["base_ms"] >= 3200
        assert ready["connection"] == 2
        for _ in range(40):
            ws.send_bytes(PCM_FRAME)
        resumed_events = _drain_until(ws, lambda event: event["type"] == "final")
        # The resumed final may be clamped back to the previous segment end for
        # contiguity, but it must cover audio beyond the resume base.
        assert any(
            event["end_ms"] > ready["base_ms"]
            for event in resumed_events
            if event["type"] == "final"
        )
        ws.send_json({"type": "stop"})
        done = _drain_until(ws, lambda event: event["type"] == "done")[-1]

    assert done["status"] == "ended"
    assert done["total_ms"] == 8000
    segments = _db_finals(session_id)
    assert len(segments) >= 3
    assert len({segment.text for segment in segments}) == len(segments), "no duplicated captions"
    assert [segment.idx for segment in segments] == list(range(len(segments)))
    for previous, current in zip(segments, segments[1:]):
        assert current.start_ms == previous.end_ms, "resumed finals must be contiguous"
        assert current.end_ms > current.start_ms
    detail = auth_client.get(f"/api/live/sessions/{session_id}").json()
    assert detail["session"]["status"] == "ended"


def test_disconnect_grace_expiry_ends_session(auth_client, live_on, monkeypatch):
    monkeypatch.setattr(live_service, "_reconnect_grace_sec", lambda: 0.2)
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    session_id = session["id"]
    with auth_client.websocket_connect(_ws_url(session_id, token)) as ws:
        assert ws.receive_json()["type"] == "ready"
        for _ in range(10):  # 1000 ms
            ws.send_bytes(PCM_FRAME)
        # Sending is fire-and-forget: on slow CI runners the client context can
        # close before every queued frame is consumed, dropping audio (e.g. only
        # 700 ms counted). The mock provider emits a partial every 800 ms, so
        # waiting for that partial proves the server consumed at least 800 ms;
        # the remaining frames are then either processed or dropped on
        # disconnect, which is why total_audio_ms is bounded rather than exact.
        _drain_until(
            ws,
            lambda event: event["type"] == "partial" and int(event.get("end_ms") or 0) >= 800,
        )

    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        detail = auth_client.get(f"/api/live/sessions/{session_id}").json()
        status = detail["session"]["status"]
        if status != "active":
            break
        time.sleep(0.05)
    assert status == "ended"
    # At least the 800 ms partial was consumed; trailing frames may be dropped
    # by the disconnect race (never more than the 1000 ms actually sent).
    assert 800 <= detail["session"]["total_audio_ms"] <= 1000
    assert "超时未重连" in (detail["session"]["error"] or "")


def test_stop_while_detached_ends_immediately(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    session_id = session["id"]
    with auth_client.websocket_connect(_ws_url(session_id, token)) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(PCM_FRAME)
    _wait_detached(session_id)

    response = auth_client.post(f"/api/live/sessions/{session_id}/stop")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["status"] == "ended"
    # Second stop is idempotent.
    again = auth_client.post(f"/api/live/sessions/{session_id}/stop")
    assert again.json()["status"] == "ended"
    detail = auth_client.get(f"/api/live/sessions/{session_id}").json()
    assert detail["session"]["status"] == "ended"


def test_heartbeat_ping_pong(auth_client, live_on):
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "ping"})
        received_pong = False
        for _ in range(20):
            event = ws.receive_json()
            if event["type"] == "pong":
                received_pong = True
                break
        assert received_pong
        ws.send_json({"type": "heartbeat"})  # ignored, no error
        ws.send_bytes(PCM_FRAME)
        ws.send_json({"type": "stop"})
        events = _drain_until(ws, lambda event: event["type"] == "done")
    assert not any(event["type"] == "error" for event in events)


class _FailingProvider:
    """Test double: provider that fails to open upstream."""

    name = "mock"
    model = "mock-realtime"

    def __init__(self) -> None:
        asyncio_queue = __import__("asyncio").Queue()
        self.events = asyncio_queue

    async def start(self, *, language: str = "", resume_from_ms: int = 0) -> None:
        raise ProviderError("模拟实时服务不可用", provider="mock")

    async def send_audio(self, pcm: bytes) -> None:
        return None

    async def finish(self) -> None:
        return None

    async def close(self) -> None:
        return None


def test_provider_start_error_marks_session_failed(auth_client, live_on, monkeypatch):
    monkeypatch.setattr(live_service, "build_live_provider", lambda *args, **kwargs: _FailingProvider())
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    with auth_client.websocket_connect(_ws_url(session["id"], token)) as ws:
        events = []
        for _ in range(10):
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "done":
                break
    assert events[0]["type"] == "error"
    assert events[0]["code"] == "PROVIDER_ERROR"
    assert events[-1]["type"] == "done" and events[-1]["status"] == "failed"
    detail = auth_client.get(f"/api/live/sessions/{session['id']}").json()
    assert detail["session"]["status"] == "failed"
    assert "模拟实时服务不可用" in (detail["session"]["error"] or "")


def test_stale_active_session_cleanup(database, live_on):
    with SessionLocal() as db:
        stale = LiveSession(
            uuid="stale-session-0001",
            user_id=None,
            recording_id=None,
            status="active",
            provider="mock",
            model="mock-realtime",
            language="zh",
            merge_mode="replace",
            started_at=utcnow() - timedelta(hours=3),
        )
        fresh = LiveSession(
            uuid="fresh-session-0001",
            user_id=None,
            recording_id=None,
            status="active",
            provider="mock",
            model="mock-realtime",
            language="zh",
            merge_mode="replace",
            started_at=utcnow(),
        )
        db.add_all([stale, fresh])
        db.commit()
        stale_id, fresh_id = int(stale.id), int(fresh.id)

    assert cleanup_stale_sessions() >= 1
    with SessionLocal() as db:
        assert db.get(LiveSession, stale_id).status == "ended"
        assert db.get(LiveSession, stale_id).ended_at is not None
        assert db.get(LiveSession, fresh_id).status == "active"
    assert cleanup_stale_sessions() == 0


def test_reconnect_without_runtime_resumes_from_db(auth_client, live_on):
    """Process-restart style resume: DB state is enough to continue timestamps."""
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    session_id = session["id"]
    with SessionLocal() as db:
        db.add(
            LiveSegment(
                session_id=session_id,
                idx=0,
                start_ms=0,
                end_ms=3000,
                text="已完成的实时稿第一句。",
                is_final=True,
            )
        )
        live = db.get(LiveSession, session_id)
        live.total_audio_ms = 3000
        db.commit()

    with auth_client.websocket_connect(_ws_url(session_id, token)) as ws:
        ready = ws.receive_json()
        assert ready["resumed"] is True
        assert ready["base_ms"] == 3000
        for _ in range(40):
            ws.send_bytes(PCM_FRAME)
        ws.send_json({"type": "stop"})
        _drain_until(ws, lambda event: event["type"] == "done")
    segments = _db_finals(session_id)
    assert segments[0].text == "已完成的实时稿第一句。"
    assert segments[1].start_ms == segments[0].end_ms


def test_immediate_reconnect_resumes_without_manual_wait(auth_client, live_on):
    """Frontends reconnect immediately after a socket drop: no artificial sleep needed."""
    token = _token_of(auth_client)
    session = _create_session(auth_client)
    url = _ws_url(session["id"], token)
    with auth_client.websocket_connect(url) as ws:
        assert ws.receive_json()["type"] == "ready"
        for _ in range(20):  # 2000 ms (no final yet)
            ws.send_bytes(PCM_FRAME)
    # Reconnect right away; the server waits for the old connection to detach.
    with auth_client.websocket_connect(url) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["resumed"] is True
        assert ready["base_ms"] >= 2000
        ws.send_json({"type": "stop"})
        done = _drain_until(ws, lambda event: event["type"] == "done")[-1]
    assert done["status"] == "ended"
    detail = auth_client.get(f"/api/live/sessions/{session['id']}").json()
    assert detail["session"]["status"] == "ended"
