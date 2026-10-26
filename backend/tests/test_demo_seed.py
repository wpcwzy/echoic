"""Demo seeding tests: idempotency, content completeness, synthesized audio."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.core import media, storage
from app.db import Base, SessionLocal, init_db
from app.models import Analysis, Chunk, Recording, Speaker, Tag, TranscriptSegment
from app.providers.demo_fixtures import demo_recordings, load_demo_data
from app.services.demo import ensure_mock_demo, seed_demo

EXPECTED_KINDS = {
    "summary",
    "chapters",
    "viewpoint_tree",
    "insights",
    "action_items",
    "suggested_questions",
    "keywords",
}


@pytest.fixture()
def database(db):
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    yield db


def _fixtures() -> list[dict]:
    data = load_demo_data()
    if data is None:
        pytest.skip("app/demo_data.json 不可用")
    return demo_recordings()


@pytest.mark.skipif(not media.ffmpeg_available(settings.ffmpeg), reason="ffmpeg unavailable")
def test_seed_demo_is_idempotent_and_creates_recordings(database, monkeypatch):
    fixtures = _fixtures()
    created = seed_demo(database)
    assert created == len(fixtures)
    assert seed_demo(database) == 0
    with SessionLocal() as db:
        recordings = db.scalars(select(Recording).order_by(Recording.id)).all()
        assert len(recordings) == len(fixtures)
        assert len({recording.title for recording in recordings}) == len(recordings)
        assert db.scalars(select(Tag)).all()
        for recording in recordings:
            assert recording.status == "ready"
            assert recording.stage == "done"
            assert recording.progress == 100
            assert recording.processed_at is not None
            assert recording.asr_provider == "mock"
            assert recording.llm_provider == "mock"
            assert recording.duration_ms > 0
            directory = storage.resolve_storage_dir(recording.storage_dir)
            assert (directory / "playback.m4a").exists()
            assert (directory / "original.mp3").exists()


def test_seed_demo_content_completeness(database, monkeypatch):
    # Audio synthesis is validated separately; skip it here to keep the suite fast.
    monkeypatch.setattr("app.services.demo._generate_demo_audio", lambda recording, generate: None)
    fixtures = _fixtures()
    assert seed_demo(database) == len(fixtures)
    with SessionLocal() as db:
        traceopt = db.scalars(
            select(Recording).where(Recording.title.like("TraceOpt%"))
        ).first()
        assert traceopt is not None
        assert traceopt.duration_ms > 15 * 60 * 1000
        speakers = db.scalars(
            select(Speaker).where(Speaker.recording_id == traceopt.id).order_by(Speaker.id)
        ).all()
        assert len(speakers) >= 2
        assert any(speaker.display_name == "吴克强" for speaker in speakers)
        segments = db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.recording_id == traceopt.id)
            .order_by(TranscriptSegment.idx)
        ).all()
        assert len(segments) == 73
        assert [segment.idx for segment in segments] == list(range(len(segments)))
        assert all(segment.start_ms < segment.end_ms for segment in segments)

        analyses = {
            row.kind: row
            for row in db.scalars(select(Analysis).where(Analysis.recording_id == traceopt.id)).all()
        }
        assert set(analyses) == EXPECTED_KINDS

        chunks = db.scalars(
            select(Chunk).where(Chunk.recording_id == traceopt.id).order_by(Chunk.idx)
        ).all()
        assert len(chunks) > 10
        assert all(len(chunk.text) <= 1200 for chunk in chunks)
        assert all(chunk.embedding is None for chunk in chunks)

        import json

        summary = json.loads(analyses["summary"].payload_json)
        assert summary["markdown"].startswith("##")
        chapters = json.loads(analyses["chapters"].payload_json)
        assert 3 <= len(chapters) <= 12
        assert chapters[0]["start_ms"] == 0
        assert chapters[-1]["end_ms"] == traceopt.duration_ms
        tree = json.loads(analyses["viewpoint_tree"].payload_json)
        assert tree["root"]["label"]
        assert tree["root"]["children"]
        insights = json.loads(analyses["insights"].payload_json)
        assert insights and all(item["category"] in {"decision", "risk", "insight", "opportunity"} for item in insights)
        actions = json.loads(analyses["action_items"].payload_json)
        assert actions and all(item["status"] in {"open", "done"} for item in actions)
        questions = json.loads(analyses["suggested_questions"].payload_json)
        assert 4 <= len(questions) <= 8
        keywords = json.loads(analyses["keywords"].payload_json)
        assert 5 <= len(keywords) <= 10
        assert all(0 <= item["weight"] <= 1 for item in keywords)


def test_ensure_mock_demo_only_seeds_empty_library(database, monkeypatch):
    fixtures = _fixtures()
    monkeypatch.setattr(settings, "demo_seed", True)
    monkeypatch.setattr("app.services.demo._generate_demo_audio", lambda recording, generate: None)
    with SessionLocal() as db:
        assert db.scalars(select(Recording)).all() == []
        ensure_mock_demo(db)
        first_count = len(db.scalars(select(Recording)).all())
        assert first_count == len(fixtures)
        # Second call must be a no-op (library not empty).
        ensure_mock_demo(db)
        assert len(db.scalars(select(Recording)).all()) == first_count


def test_ensure_mock_demo_respects_seed_switch(database, monkeypatch):
    _fixtures()
    monkeypatch.setattr(settings, "demo_seed", False)
    with SessionLocal() as db:
        ensure_mock_demo(db)
        assert db.scalars(select(Recording)).all() == []
