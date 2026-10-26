"""Mock pipeline end-to-end, reprocess, cancel and JSON-robustness tests.

Network free; ffmpeg is used when available and the pipeline is also exercised
through its no-ffmpeg fallback (mock provider only).
"""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.core import media, storage
from app.core.utils import utcnow
from app.db import Base, SessionLocal, init_db
from app.models import Analysis, Chunk, Job, Recording, Speaker, TranscriptSegment
from app.services import analysis as analysis_service
from app.services.pipeline import process_recording
from app.services.queue import enqueue, request_cancel


@pytest.fixture()
def database(db):
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    yield db


def _make_recording(db, *, title: str = "测试会议", duration_s: int = 30, with_audio: bool = True) -> int:
    unique = uuid.uuid4().hex
    directory = storage.recording_dir(unique)
    recording = Recording(
        uuid=unique,
        title=title,
        status="uploaded",
        stage="preparing",
        storage_dir=f"media/{unique}",
        media_type="audio",
        language="zh",
        source="upload",
    )
    if with_audio:
        source = directory / "original.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=280:duration={duration_s}",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(source),
            ],
            check=True,
        )
        recording.original_filename = f"{title}.wav"
        recording.original_ext = ".wav"
        recording.size_bytes = source.stat().st_size
    db.add(recording)
    db.commit()
    return int(recording.id)


def _analysis_map(recording_id: int) -> dict[str, object]:
    with SessionLocal() as db:
        rows = db.scalars(select(Analysis).where(Analysis.recording_id == recording_id)).all()
        return {row.kind: json.loads(row.payload_json) for row in rows}


@pytest.mark.skipif(not media.ffmpeg_available(settings.ffmpeg), reason="ffmpeg unavailable")
async def test_mock_pipeline_end_to_end(database):
    recording_id = _make_recording(database, duration_s=45)
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")

    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        assert recording is not None
        assert recording.status == "ready"
        assert recording.stage == "done"
        assert recording.progress == 100
        assert recording.error_message is None
        assert recording.processed_at is not None
        assert recording.asr_provider == "mock"
        assert recording.llm_provider == "mock"
        assert 40_000 <= int(recording.duration_ms) <= 46_000
        speaker_count = len(db.scalars(select(Speaker).where(Speaker.recording_id == recording_id)).all())
        segments = db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.recording_id == recording_id)
            .order_by(TranscriptSegment.idx)
        ).all()
        chunks = db.scalars(select(Chunk).where(Chunk.recording_id == recording_id)).all()
        job = db.scalars(select(Job).where(Job.recording_id == recording_id).order_by(Job.id.desc())).first()

    assert 2 <= speaker_count <= 4
    assert 8 <= len(segments) <= 14
    assert [segment.idx for segment in segments] == list(range(len(segments)))
    assert all(segment.text for segment in segments)
    assert all(segment.start_ms < segment.end_ms for segment in segments)
    assert [segment.start_ms for segment in segments] == sorted(segment.start_ms for segment in segments)
    assert chunks, "chunks must be created"
    assert all(len(chunk.text) <= 1200 for chunk in chunks)
    assert all(chunk.embedding is None and chunk.dim is None for chunk in chunks)
    assert job is not None and job.status == "done" and job.progress == 100

    kinds = _analysis_map(recording_id)
    assert set(kinds) == {
        "summary",
        "chapters",
        "viewpoint_tree",
        "insights",
        "action_items",
        "keywords",
        "suggested_questions",
    }
    assert kinds["summary"]["markdown"].startswith("##")
    assert 3 <= len(kinds["chapters"]) <= 12
    assert kinds["chapters"][0]["start_ms"] == 0
    assert kinds["chapters"][0]["end_ms"] <= int(recording.duration_ms)
    assert isinstance(kinds["viewpoint_tree"]["root"]["children"], list)
    assert kinds["viewpoint_tree"]["root"]["children"]
    assert 4 <= len(kinds["suggested_questions"]) <= 8
    assert 5 <= len(kinds["keywords"]) <= 10

    # temp asr.wav cleanup + browser playback generated
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        directory = storage.resolve_storage_dir(recording.storage_dir)
        assert not (directory / "asr.wav").exists()
        assert (directory / "playback.m4a").exists()


async def test_pipeline_without_ffmpeg_mock_fallback(database, monkeypatch):
    """Without ffmpeg/ffprobe the mock pipeline still produces a transcript."""
    monkeypatch.setattr(media, "ffmpeg_available", lambda *args, **kwargs: False)
    monkeypatch.setattr(media, "ffprobe_available", lambda *args, **kwargs: False)
    recording_id = _make_recording(database, title="无 ffmpeg 录音", with_audio=False)
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        recording.duration_ms = 180_000
        db.commit()
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        assert recording.status == "ready", recording.error_message
        assert recording.duration_ms == 180_000
        assert db.scalars(select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)).all()


async def test_reprocess_insights_keeps_transcript(database):
    recording_id = _make_recording(database, duration_s=20)
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")
    with SessionLocal() as db:
        segment_count = len(
            db.scalars(select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)).all()
        )
        summary = db.scalars(
            select(Analysis).where(
                Analysis.recording_id == recording_id, Analysis.kind == "summary"
            )
        ).first()
        original = summary.payload_json
        summary.payload_json = json.dumps({"markdown": "## 旧纪要\n将被重新生成"})
        db.commit()

    enqueue(database, recording_id, "insights")
    await process_recording(recording_id, "insights")
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        assert recording.status == "ready"
        segments_after = len(
            db.scalars(select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)).all()
        )
        summary_after = db.scalars(
            select(Analysis).where(
                Analysis.recording_id == recording_id, Analysis.kind == "summary"
            )
        ).first()
    assert segments_after == segment_count
    assert "旧纪要" not in summary_after.payload_json
    assert "## 会议概述" in summary_after.payload_json


async def test_cancel_before_processing(database):
    recording_id = _make_recording(database, duration_s=10)
    enqueue(database, recording_id, "all")
    with SessionLocal() as db:
        assert request_cancel(db, recording_id) is True
    await process_recording(recording_id, "all")
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        assert recording.status == "cancelled"
        assert recording.error_message is None
        segments = db.scalars(
            select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)
        ).all()
        job = db.scalars(select(Job).where(Job.recording_id == recording_id).order_by(Job.id.desc())).first()
    assert not segments
    assert job.status == "cancelled"


def test_extract_json_robustness():
    payload = {"summary_md": "hi", "nested": {"value": [1, 2]}}
    raw = json.dumps(payload, ensure_ascii=False)
    assert analysis_service.extract_json(raw) == payload
    assert analysis_service.extract_json(f"```json\n{raw}\n```") == payload
    assert analysis_service.extract_json(f"好的，结果如下：\n{raw}\n请查收。") == payload
    assert analysis_service.extract_json('[{"a": 1}, {"a": 2}]') == [{"a": 1}, {"a": 2}]
    assert analysis_service.extract_json("not json at all") is None
    assert analysis_service.extract_json("") is None
    assert analysis_service.extract_json(None) is None


def test_chapter_and_tree_validation_clamps():
    chapters = analysis_service.sanitize_chapters(
        {
            "chapters": [
                {"title": "B", "start_ms": 90_000, "end_ms": 80_000},
                {"title": "A", "start_ms": 10_000, "end_ms": 120_000},
                {"title": "C", "start_ms": -5, "end_ms": 999_999},
            ]
        },
        duration_ms=100_000,
    )
    assert [chapter["title"] for chapter in chapters] == ["C", "A", "B"]
    assert chapters[0]["start_ms"] == 0
    assert chapters[-1]["end_ms"] == 100_000
    for index in range(1, len(chapters)):
        assert chapters[index]["start_ms"] >= chapters[index - 1]["end_ms"]

    tree = analysis_service.sanitize_viewpoint_tree(
        {
            "root": {
                "label": "主题",
                "children": [
                    {"label": "分支", "time_ms": 999_999, "children": [{"label": "观点", "kind": "unknown"}]}
                ],
            }
        },
        duration_ms=50_000,
    )
    root = tree["root"]
    assert root["id"] == "root"
    assert root["children"][0]["time_ms"] == 50_000
    assert root["children"][0]["children"][0]["kind"] == "point"


def test_chunking_hard_limits():
    from app.services.analysis import build_chunk_payloads

    long_text = "很长的发言内容" * 400
    rows = [
        {"start_ms": 0, "end_ms": 30_000, "speaker": "说话人1", "text": long_text[:400]},
        {"start_ms": 30_000, "end_ms": 60_000, "speaker": "说话人2", "text": "短发言"},
    ]
    chunks = build_chunk_payloads(rows, duration_ms=60_000)
    assert chunks
    assert all(len(chunk["text"]) <= 1200 for chunk in chunks)
    assert all(0 <= chunk["start_ms"] <= chunk["end_ms"] <= 60_000 for chunk in chunks)
