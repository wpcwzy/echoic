"""Recording description auto-fill tests (v0.3.10 upload "暂无描述" fix).

Covers the derivation helpers, the pipeline integration for new uploads and
reprocessing (summary path + transcript fallback + never overwrite user text),
and the optional ``python -m app.tools.backfill_descriptions`` CLI.
"""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest
from sqlalchemy import select

from app.core import media
from app.db import Base, SessionLocal, init_db
from app.models import Analysis, Recording, TranscriptSegment
from app.services import analysis as analysis_service
from app.services.analysis import (
    DESCRIPTION_MAX_CHARS,
    apply_auto_description,
    derive_auto_description,
    derive_description_from_segments,
    derive_description_from_summary,
    shorten_description,
)
from app.services.pipeline import process_recording
from app.services.queue import enqueue
from app.tools import backfill_descriptions as backfill_tool


@pytest.fixture()
def database(db):
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    yield db


def _make_recording(
    db,
    *,
    title: str = "描述测试会议",
    description: str | None = None,
    with_audio: bool = False,
    duration_s: int = 20,
) -> int:
    unique = uuid.uuid4().hex
    from app.core import storage

    directory = storage.recording_dir(unique)
    recording = Recording(
        uuid=unique,
        title=title,
        description=description,
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
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"sine=frequency=280:duration={duration_s}",
                "-ar", "16000", "-ac", "1", str(source),
            ],
            check=True,
        )
        recording.original_filename = f"{title}.wav"
        recording.original_ext = ".wav"
        recording.size_bytes = source.stat().st_size
    db.add(recording)
    db.commit()
    return int(recording.id)


def _description(recording_id: int) -> str | None:
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        return recording.description if recording is not None else None


def _summary_markdown(recording_id: int) -> str | None:
    with SessionLocal() as db:
        row = db.scalars(
            select(Analysis).where(
                Analysis.recording_id == recording_id, Analysis.kind == "summary"
            )
        ).first()
        if row is None:
            return None
        return json.loads(row.payload_json).get("markdown")


requires_ffmpeg = pytest.mark.skipif(
    not media.ffmpeg_available("ffmpeg"), reason="ffmpeg unavailable"
)


# ------------------------------------------------------------------ unit tests
def test_derive_description_from_summary_overview():
    markdown = (
        "## 会议概述\n本次组会围绕 ICPE 投稿展开，先倒排摘要与正文的交付节点。\n"
        "随后复盘了实验数据。\n\n## 关键结论\n- Graph500 提升约 10%\n"
    )
    description = derive_description_from_summary(markdown)
    assert description is not None
    assert description.startswith("本次组会围绕")
    assert "##" not in description and "- " not in description
    assert len(description) <= DESCRIPTION_MAX_CHARS


def test_derive_description_from_summary_fallbacks_and_limits():
    assert derive_description_from_summary(None) is None
    assert derive_description_from_summary("") is None
    # No 会议概述 heading -> first meaningful non-heading line.
    description = derive_description_from_summary("# 标题\n\n这是一段没有章节的纪要内容。")
    assert description == "这是一段没有章节的纪要内容。"
    long_markdown = "## 会议概述\n" + "这是一句很长的会议描述，" * 40
    limited = derive_description_from_summary(long_markdown)
    assert limited is not None and len(limited) <= DESCRIPTION_MAX_CHARS


def test_derive_description_from_segments_joins_and_limits():
    description = derive_description_from_segments(
        ["第一句介绍了会议背景。", "第二句补充了讨论范围。", "第三句是更多细节。", "第四句不该出现。"]
    )
    assert description is not None
    assert description.startswith("第一句介绍了会议背景。")
    assert "第四句不该出现" not in description
    assert len(description) <= DESCRIPTION_MAX_CHARS
    assert derive_description_from_segments([]) is None
    assert derive_description_from_segments(["", "  "]) is None


def test_shorten_description_sentence_aware():
    assert shorten_description("短描述") == "短描述"
    text = "甲" * 200
    shortened = shorten_description(text)
    assert shortened is not None and len(shortened) <= DESCRIPTION_MAX_CHARS


def test_auto_description_never_overwrites_user_text(database):
    recording_id = _make_recording(database, description="用户自己写的描述")
    with SessionLocal() as db:
        db.add(
            Analysis(
                recording_id=recording_id,
                kind="summary",
                payload_json=json.dumps({"markdown": "## 会议概述\nAI 生成的概述。"}),
            )
        )
        db.commit()
        assert derive_auto_description(recording_id, db) is None
        assert apply_auto_description(recording_id, db) is None
    assert _description(recording_id) == "用户自己写的描述"


def test_derive_prefers_summary_then_transcript(database):
    summary_id = _make_recording(database, title="有纪要的录音")
    transcript_id = _make_recording(database, title="只有转录的录音")
    with SessionLocal() as db:
        db.add(
            Analysis(
                recording_id=summary_id,
                kind="summary",
                payload_json=json.dumps({"markdown": "## 会议概述\n来自 AI 纪要的一句话概述。"}),
            )
        )
        for idx, text in enumerate(["转录第一句。", "转录第二句。"]):
            db.add(
                TranscriptSegment(
                    recording_id=transcript_id, idx=idx, start_ms=idx * 1000, end_ms=(idx + 1) * 1000, text=text
                )
            )
        db.add(
            TranscriptSegment(
                recording_id=summary_id, idx=0, start_ms=0, end_ms=1000, text="不应被使用的转录。"
            )
        )
        db.commit()

    assert derive_auto_description(summary_id) == "来自 AI 纪要的一句话概述。"
    assert derive_auto_description(transcript_id, None) == "转录第一句。转录第二句。"


# --------------------------------------------------------------- pipeline e2e
@requires_ffmpeg
async def test_pipeline_auto_fills_description_from_summary(database):
    recording_id = _make_recording(database, with_audio=True)
    assert _description(recording_id) is None
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")

    description = _description(recording_id)
    markdown = _summary_markdown(recording_id)
    assert markdown, "mock pipeline must generate a summary"
    assert description, "description should be auto-filled after analysis"
    assert len(description) <= DESCRIPTION_MAX_CHARS
    assert "##" not in description and "**" not in description
    assert description == derive_description_from_summary(markdown)
    with SessionLocal() as db:
        assert db.get(Recording, recording_id).status == "ready"


@requires_ffmpeg
async def test_pipeline_description_falls_back_to_transcript(database, monkeypatch):
    recording_id = _make_recording(database, with_audio=True)

    async def noop_generate_analyses(db, recording, cfg, *, progress=None, check_cancel=None):
        return {"written": [], "failed": []}

    monkeypatch.setattr("app.services.pipeline.generate_analyses", noop_generate_analyses)
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")

    with SessionLocal() as db:
        assert db.get(Recording, recording_id).status == "ready"
        assert db.scalars(
            select(Analysis).where(Analysis.recording_id == recording_id)
        ).all() == []
        transcript = "".join(
            segment.text
            for segment in db.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.recording_id == recording_id)
                .order_by(TranscriptSegment.idx)
            ).all()
        )
    description = _description(recording_id)
    assert description, "transcript fallback must produce a description"
    assert len(description) <= DESCRIPTION_MAX_CHARS
    assert description.split("；")[0][:8] in transcript


@requires_ffmpeg
async def test_pipeline_keeps_user_description(database):
    recording_id = _make_recording(database, with_audio=True, description="用户写的会议描述")
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")
    assert _description(recording_id) == "用户写的会议描述"


@requires_ffmpeg
async def test_reprocess_refills_missing_description_and_preserves_custom(database):
    recording_id = _make_recording(database, with_audio=True)
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")
    first_description = _description(recording_id)
    assert first_description

    # Simulate an old recording whose description was cleared -> reprocess fills it.
    with SessionLocal() as db:
        db.get(Recording, recording_id).description = None
        db.commit()
    enqueue(database, recording_id, "insights")
    await process_recording(recording_id, "insights")
    refilled = _description(recording_id)
    assert refilled and len(refilled) <= DESCRIPTION_MAX_CHARS

    # A user-edited description survives reprocessing.
    with SessionLocal() as db:
        db.get(Recording, recording_id).description = "用户手动修改后的描述"
        db.commit()
    enqueue(database, recording_id, "insights")
    await process_recording(recording_id, "insights")
    assert _description(recording_id) == "用户手动修改后的描述"


# -------------------------------------------------------------------- CLI tool
def test_backfill_cli_dry_run_and_apply(database, capsys):
    summary_id = _make_recording(database, title="待回填-有纪要")
    transcript_id = _make_recording(database, title="待回填-有转录")
    custom_id = _make_recording(database, title="已有描述", description="保持不变")
    with SessionLocal() as db:
        db.add(
            Analysis(
                recording_id=summary_id,
                kind="summary",
                payload_json=json.dumps({"markdown": "## 会议概述\n回填用的一句话概述。"}),
            )
        )
        db.add(
            TranscriptSegment(
                recording_id=transcript_id, idx=0, start_ms=0, end_ms=1000, text="转录回填用的一句话。"
            )
        )
        db.commit()

    dry_run = backfill_tool.backfill(apply=False)
    assert dry_run["dry_run"] is True
    assert dry_run["scanned"] == 2
    assert dry_run["derivable"] == 2
    assert dry_run["filled"] == 0
    assert _description(summary_id) is None

    applied = backfill_tool.backfill(apply=True)
    assert applied["filled"] == 2
    assert _description(summary_id) == "回填用的一句话概述。"
    assert _description(transcript_id) == "转录回填用的一句话。"
    assert _description(custom_id) == "保持不变"

    # Idempotent: nothing left to fill.
    assert backfill_tool.backfill(apply=True)["scanned"] == 0

    # --recording-id filter + CLI JSON output.
    assert backfill_tool.main(["--apply", "--limit", "5"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["apply"] is True and output["scanned"] == 0
