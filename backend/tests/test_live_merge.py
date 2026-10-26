"""Live-transcript merge helper tests (replace / promote / idempotency).

These tests build DB fixtures directly (no WebSocket, no pipeline) so the merge
contract that the Lead wires into ``services/pipeline.py`` is pinned down.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db import Base, SessionLocal, init_db
from app.models import (
    LiveSegment,
    LiveSession,
    Recording,
    Speaker,
    TranscriptSegment,
)
from app.services.live_merge import (
    LIVE_SPEAKER_LABEL,
    find_live_session_for_recording,
    merge_live_session_into_recording,
)


@pytest.fixture()
def database(db):
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    yield db


def _make_recording(*, with_offline_transcript: bool = True, status: str = "ready") -> int:
    unique = uuid.uuid4().hex
    with SessionLocal() as db:
        recording = Recording(
            uuid=unique,
            title="实时转写测试录音",
            status=status,
            stage="done",
            progress=100,
            storage_dir=f"media/{unique}",
            media_type="audio",
            original_filename="live.wav",
            original_ext=".wav",
            duration_ms=0,
            language="zh",
            source="browser_recording",
        )
        db.add(recording)
        db.commit()
        recording_id = int(recording.id)
        if with_offline_transcript:
            speaker = Speaker(
                recording_id=recording_id,
                label="说话人1",
                display_name="吴克强",
                color="#5B8FF9",
                is_auto=True,
            )
            db.add(speaker)
            db.flush()
            db.add(
                TranscriptSegment(
                    recording_id=recording_id,
                    idx=0,
                    speaker_id=speaker.id,
                    start_ms=0,
                    end_ms=2000,
                    text="离线转写的第一句话。",
                    confidence=0.9,
                )
            )
            db.add(
                TranscriptSegment(
                    recording_id=recording_id,
                    idx=1,
                    speaker_id=speaker.id,
                    start_ms=2000,
                    end_ms=4000,
                    text="离线转写第二句。",
                    confidence=0.9,
                )
            )
            db.commit()
    return recording_id


def _make_session(
    recording_id: int | None,
    *,
    merge_mode: str = "replace",
    status: str = "ended",
    provider: str = "mock",
    finals: int = 3,
) -> int:
    with SessionLocal() as db:
        session = LiveSession(
            uuid=str(uuid.uuid4()),
            user_id=None,
            recording_id=recording_id,
            status=status,
            provider=provider,
            model="mock-realtime",
            language="zh",
            merge_mode=merge_mode,
            total_audio_ms=finals * 1500,
        )
        db.add(session)
        db.flush()
        for index in range(finals):
            db.add(
                LiveSegment(
                    session_id=session.id,
                    idx=index,
                    start_ms=index * 1500,
                    end_ms=(index + 1) * 1500,
                    text=f"实时稿第{index + 1}句。",
                    is_final=True,
                )
            )
        # A partial row (should never be merged).
        db.add(
            LiveSegment(
                session_id=session.id,
                idx=finals,
                start_ms=finals * 1500,
                end_ms=finals * 1500 + 400,
                text="未确认的半句",
                is_final=False,
            )
        )
        db.commit()
        return int(session.id)


def _transcript(db, recording_id: int) -> list[TranscriptSegment]:
    return list(
        db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.recording_id == recording_id)
            .order_by(TranscriptSegment.idx)
        ).all()
    )


def _session_status(db, session_id: int) -> str:
    return db.get(LiveSession, session_id).status


# ---------------------------------------------------------------------- replace
def test_merge_replace_keeps_offline_transcript(database):
    recording_id = _make_recording(with_offline_transcript=True)
    session_id = _make_session(recording_id, merge_mode="replace")
    with SessionLocal() as db:
        before = [(segment.idx, segment.text) for segment in _transcript(db, recording_id)]

    outcome = merge_live_session_into_recording(recording_id, SessionLocal)

    assert outcome == "replace"
    with SessionLocal() as db:
        after = [(segment.idx, segment.text) for segment in _transcript(db, recording_id)]
        assert after == before, "replace mode must keep the offline transcript"
        session = db.get(LiveSession, session_id)
        assert session.status == "merged"
        assert session.ended_at is not None
        # Live transcript is still available for the detail page.
        live_segments = list(
            db.scalars(
                select(LiveSegment)
                .where(LiveSegment.session_id == session_id)
                .order_by(LiveSegment.idx)
            ).all()
        )
        assert len(live_segments) == 4  # 3 finals + 1 partial row kept for audit


def test_merge_uses_most_recent_bound_session(database):
    recording_id = _make_recording(with_offline_transcript=True)
    _make_session(recording_id, merge_mode="replace")
    newest = _make_session(recording_id, merge_mode="promote")
    assert merge_live_session_into_recording(recording_id, SessionLocal) == "promote"
    with SessionLocal() as db:
        session = find_live_session_for_recording(db, recording_id)
        assert session is not None and session.id == newest


# ---------------------------------------------------------------------- promote
def test_merge_promote_replaces_transcript_with_live_single_speaker(database):
    recording_id = _make_recording(with_offline_transcript=True)
    session_id = _make_session(recording_id, merge_mode="promote", finals=3)

    outcome = merge_live_session_into_recording(recording_id, SessionLocal)

    assert outcome == "promote"
    with SessionLocal() as db:
        segments = _transcript(db, recording_id)
        assert len(segments) == 3
        assert [segment.idx for segment in segments] == [0, 1, 2]
        assert [segment.text for segment in segments] == ["实时稿第1句。", "实时稿第2句。", "实时稿第3句。"]
        assert all(segment.start_ms < segment.end_ms for segment in segments)
        speakers = list(db.scalars(select(Speaker).where(Speaker.recording_id == recording_id)).all())
        assert len(speakers) == 1
        assert speakers[0].label == LIVE_SPEAKER_LABEL
        assert speakers[0].display_name == LIVE_SPEAKER_LABEL
        recording = db.get(Recording, recording_id)
        assert recording.asr_provider == "live:mock"
        assert recording.duration_ms >= 4500
        assert recording.error_message is None
        assert db.get(LiveSession, session_id).status == "merged"


def test_merge_auto_promotes_when_offline_transcript_missing(database):
    recording_id = _make_recording(with_offline_transcript=False)
    _make_session(recording_id, merge_mode="replace", finals=2)

    assert merge_live_session_into_recording(recording_id, SessionLocal) == "promote"
    with SessionLocal() as db:
        assert len(_transcript(db, recording_id)) == 2
        assert db.get(Recording, recording_id).asr_provider == "live:mock"


def test_merge_auto_promotes_after_offline_failure(database):
    recording_id = _make_recording(with_offline_transcript=True, status="failed")
    _make_session(recording_id, merge_mode="replace", finals=2, status="failed")

    assert merge_live_session_into_recording(recording_id, SessionLocal) == "promote"
    with SessionLocal() as db:
        assert len(_transcript(db, recording_id)) == 2
        assert db.get(Recording, recording_id).asr_provider == "live:mock"


# ------------------------------------------------------------------ idempotency
def test_merge_is_idempotent(database):
    recording_id = _make_recording(with_offline_transcript=False)
    _make_session(recording_id, merge_mode="promote", finals=2)

    assert merge_live_session_into_recording(recording_id, SessionLocal) == "promote"
    with SessionLocal() as db:
        count_after_first = len(_transcript(db, recording_id))
    assert count_after_first == 2

    assert merge_live_session_into_recording(recording_id, SessionLocal) is None
    with SessionLocal() as db:
        assert len(_transcript(db, recording_id)) == count_after_first


def test_merge_returns_none_without_session_or_segments(database):
    recording_id = _make_recording(with_offline_transcript=True)
    assert merge_live_session_into_recording(recording_id, SessionLocal) is None

    _make_session(recording_id, merge_mode="replace", finals=0)
    assert merge_live_session_into_recording(recording_id, SessionLocal) is None
    assert merge_live_session_into_recording(999_999, SessionLocal) is None


def test_merge_skips_cancelled_session(database):
    recording_id = _make_recording(with_offline_transcript=False)
    _make_session(recording_id, merge_mode="promote", status="cancelled", finals=2)
    assert merge_live_session_into_recording(recording_id, SessionLocal) is None
    with SessionLocal() as db:
        assert _transcript(db, recording_id) == []


def test_merge_handles_unbound_session(database):
    recording_id = _make_recording(with_offline_transcript=True)
    _make_session(None, merge_mode="replace")  # session not bound to this recording
    assert merge_live_session_into_recording(recording_id, SessionLocal) is None


def test_merge_custom_session_factory(database):
    recording_id = _make_recording(with_offline_transcript=False)
    _make_session(recording_id, merge_mode="promote", finals=1)
    calls: list[object] = []

    def factory():
        calls.append(object())
        return SessionLocal()

    assert merge_live_session_into_recording(recording_id, factory) == "promote"
    assert calls, "the provided session factory must be used"
