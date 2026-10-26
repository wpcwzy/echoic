"""Shared helpers for the backend API tests (owned by backend-api).

Named ``test_api_helpers.py`` so it stays inside the ``test_api_*.py`` scope; it
contains no test cases itself, only seeding/util functions imported by the other
``test_api_*`` modules.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid as uuidlib
from pathlib import Path
from typing import Any

from app.config import settings
from app.core.utils import dump_json, utcnow
from app.models import (
    Analysis,
    ChatMessage,
    Job,
    Recording,
    Speaker,
    Tag,
    TranscriptSegment,
)

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"

DEMO_SPEAKERS = [
    {"label": "说话人1", "display_name": "吴克强", "color": "#5B8FF9"},
    {"label": "说话人2", "display_name": "李梦", "color": "#8BC6A9"},
]

DEMO_SEGMENTS = [
    (0, 12_000, 0, "我们这次主要讨论 Graph500 基准测试的交付节点。"),
    (12_000, 28_000, 1, "摘要部分预计还有两周时间完成并交付。"),
    (28_000, 45_000, 0, "那我们把最终报告定在三周之后提交。"),
]

DEMO_ANALYSES: dict[str, Any] = {
    "summary": {"markdown": "## 会议概述\n本次会议讨论了交付节点。\n\n## 关键结论\n- 摘要两周内完成"},
    "chapters": [
        {
            "id": "ch1",
            "title": "交付节点讨论",
            "start_ms": 0,
            "end_ms": 45_000,
            "summary": "确认摘要与报告的时间节点",
            "bullets": ["摘要两周内完成", "报告三周后提交"],
        }
    ],
    "viewpoint_tree": {
        "root": {
            "id": "root",
            "label": "交付节点",
            "detail": "会议主题",
            "kind": "topic",
            "children": [
                {
                    "id": "n1",
                    "label": "摘要提交",
                    "detail": "预计两周",
                    "kind": "point",
                    "time_ms": 12_000,
                    "speaker": "吴克强",
                    "children": [],
                }
            ],
        }
    },
    "insights": [
        {
            "id": "i1",
            "title": "摘要两周交付",
            "content": "摘要部分预计两周内完成",
            "category": "decision",
        }
    ],
    "action_items": [
        {
            "id": "a1",
            "title": "完成摘要",
            "owner": "吴克强",
            "due": "两周内",
            "status": "open",
            "detail": "先交付摘要",
        }
    ],
    "suggested_questions": ["这次会议定了哪些交付节点？", "摘要什么时候交付？"],
    "keywords": [{"name": "Graph500", "weight": 0.9}, {"name": "交付节点", "weight": 0.7}],
}


# ------------------------------------------------------------------ throttling util
def clear_login_throttle() -> None:
    from app.core import security

    security._login_attempts.clear()  # noqa: SLF001 - test helper


# ------------------------------------------------------------------ media utils
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_wav_bytes(duration: float = 1.0, frequency: int = 440) -> bytes:
    """Render a short sine-wave wav with ffmpeg (tests skip when unavailable)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="echoic-wav-"))
    path = tmpdir / "tone.wav"
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
            f"sine=frequency={frequency}:sample_rate=16000:duration={duration:.2f}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return path.read_bytes()


def write_media(
    recording: Recording,
    *,
    original: bytes | None = None,
    playback: bytes | None = None,
    ext: str | None = None,
) -> Path:
    base = settings.media_path / recording.uuid
    base.mkdir(parents=True, exist_ok=True)
    if original is not None:
        (base / f"original{ext or recording.original_ext or '.mp3'}").write_bytes(original)
    if playback is not None:
        (base / "playback.m4a").write_bytes(playback)
    return base


# ------------------------------------------------------------------ DB seeding
def make_recording(db, **overrides) -> Recording:
    requested_uuid = overrides.pop("uuid", None) or str(uuidlib.uuid4())
    values: dict[str, Any] = {
        "uuid": requested_uuid,
        "title": "产品评审会议",
        "description": None,
        "status": "ready",
        "stage": "done",
        "progress": 100,
        "media_type": "audio",
        "mime_type": "audio/mpeg",
        "original_filename": "产品评审会议.mp3",
        "original_ext": ".mp3",
        "storage_dir": f"media/{requested_uuid}",
        "size_bytes": 2048,
        "duration_ms": 120_000,
        "recorded_at": utcnow(),
        "language": "zh",
        "source": "upload",
    }
    values.update(overrides)
    recording = Recording(**values)
    db.add(recording)
    db.commit()
    db.refresh(recording)
    return recording


def add_tag(db, name: str, color: str = "#1677ff") -> Tag:
    tag = db.query(Tag).filter(Tag.name == name).one_or_none()
    if tag is None:
        tag = Tag(name=name, color=color)
        db.add(tag)
        db.commit()
        db.refresh(tag)
    return tag


def attach_tag(db, recording: Recording, tag: Tag) -> None:
    recording.tags.append(tag)
    db.commit()


def add_speakers(db, recording: Recording, speakers=None) -> list[Speaker]:
    rows: list[Speaker] = []
    for index, data in enumerate(speakers or DEMO_SPEAKERS):
        speaker = Speaker(
            recording_id=recording.id,
            label=data.get("label", f"说话人{index + 1}"),
            display_name=data.get("display_name", ""),
            color=data.get("color", "#5B8FF9"),
            is_auto=data.get("is_auto", True),
        )
        db.add(speaker)
        rows.append(speaker)
    db.commit()
    for speaker in rows:
        db.refresh(speaker)
    return rows


def add_segments(db, recording: Recording, speakers: list[Speaker], segments=None) -> list[TranscriptSegment]:
    rows: list[TranscriptSegment] = []
    for index, (start_ms, end_ms, speaker_index, text) in enumerate(segments or DEMO_SEGMENTS):
        speaker_id = speakers[speaker_index % len(speakers)].id if speakers else None
        segment = TranscriptSegment(
            recording_id=recording.id,
            idx=index,
            speaker_id=speaker_id,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            confidence=0.95,
        )
        db.add(segment)
        rows.append(segment)
    db.commit()
    for segment in rows:
        db.refresh(segment)
    return rows


def add_analysis(db, recording: Recording, kind: str, payload: Any, *, model: str = "qwen-plus") -> Analysis:
    analysis = Analysis(
        recording_id=recording.id,
        kind=kind,
        payload_json=payload if isinstance(payload, str) else dump_json(payload),
        provider="mock",
        model=model,
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)
    return analysis


def add_all_analyses(db, recording: Recording) -> None:
    for kind, payload in DEMO_ANALYSES.items():
        add_analysis(db, recording, kind, payload)


def seed_ready_recording(db, *, with_transcript: bool = False, with_insights: bool = False, **overrides) -> Recording:
    recording = make_recording(db, status="ready", stage="done", progress=100, **overrides)
    if with_transcript:
        speakers = add_speakers(db, recording)
        add_segments(db, recording, speakers)
    if with_insights:
        add_all_analyses(db, recording)
    return recording


def make_job(db, recording: Recording, **overrides) -> Job:
    values: dict[str, Any] = {
        "recording_id": recording.id,
        "type": "process",
        "status": "queued",
        "stage": "preparing",
        "progress": 0,
        "from_stage": "all",
        "attempts": 1,
        "error": None,
    }
    values.update(overrides)
    job = Job(**values)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def make_chat_message(db, recording: Recording, role: str = "user", content: str = "你好", citations=None) -> ChatMessage:
    message = ChatMessage(
        recording_id=recording.id,
        role=role,
        content=content,
        citations_json=dump_json(citations) if citations is not None else None,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


# ------------------------------------------------------------------ response utils
def parse_sse(body: str) -> list[dict]:
    events: list[dict] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block.startswith("data:"):
            continue
        payload = block[5:].strip()
        if payload:
            events.append(json.loads(payload))
    return events
