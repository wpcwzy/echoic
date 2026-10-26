"""Demo data seeding from ``app/demo_data.json`` (docs/PIPELINE.md §7).

``seed_demo`` creates the bundled high-quality demo library (recordings, tags,
speakers, transcript segments, analyses, chunks, synthesized tone audio) and is
idempotent by recording title.  ``ensure_mock_demo`` is the boot-time hook that
only seeds when demo mode is active and the library is empty.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid as uuid_lib
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..core import media, storage
from ..core.utils import utcnow
from ..models import (
    Analysis,
    Chunk,
    Recording,
    RecordingStatus,
    Speaker,
    Stage,
    Tag,
    TranscriptSegment,
    User,
)
from ..providers.demo_fixtures import demo_recordings, fixture_segments, fixture_speaker_labels, load_demo_data
from .analysis import (
    build_chunk_payloads,
    sanitize_action_items,
    sanitize_chapters,
    sanitize_insights,
    sanitize_keywords,
    sanitize_questions,
    sanitize_summary,
    sanitize_viewpoint_tree,
)
from .settings_store import load_ai_config

logger = logging.getLogger(__name__)

DEFAULT_TAG_COLOR = "#5B8FF9"


def _ensure_tag(db: Session, name: str, color: str = "") -> Tag | None:
    name = (name or "").strip()
    if not name:
        return None
    tag = db.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        tag = Tag(name=name, color=(color or DEFAULT_TAG_COLOR)[:16])
        db.add(tag)
        db.flush()
    elif color:
        tag.color = color[:16]
    return tag


def _normalize_analyses(
    insights: dict[str, Any], duration_ms: int, speaker_names: list[str]
) -> dict[str, Any]:
    """Convert fixture insights into canonical analysis payloads (skip empty kinds)."""
    payloads: dict[str, Any] = {}
    raw_summary = insights.get("summary")
    summary_md = ""
    if isinstance(raw_summary, dict):
        summary_md = str(raw_summary.get("markdown") or "")
    summary_md = summary_md or str(insights.get("summary_md") or "")
    if summary_md.strip():
        payloads["summary"] = {"markdown": summary_md.strip()}

    raw_chapters = insights.get("chapters")
    if isinstance(raw_chapters, dict):
        raw_chapters = raw_chapters.get("chapters")
    try:
        chapters = sanitize_chapters(raw_chapters or [], duration_ms)
    except Exception:  # noqa: BLE001 - demo data should never block boot
        chapters = []
    if chapters:
        payloads["chapters"] = chapters

    raw_tree = insights.get("viewpoint_tree")
    if isinstance(raw_tree, dict) and "root" in raw_tree:
        tree_payload: Any = raw_tree
    elif isinstance(raw_tree, dict):
        tree_payload = {"root": raw_tree}
    else:
        tree_payload = {"root": {}}
    try:
        tree = sanitize_viewpoint_tree(tree_payload, duration_ms, speaker_names)
    except Exception:  # noqa: BLE001
        tree = None
    if tree:
        payloads["viewpoint_tree"] = tree

    for kind, sanitizer in (
        ("insights", sanitize_insights),
        ("action_items", sanitize_action_items),
        ("keywords", sanitize_keywords),
    ):
        try:
            value = sanitizer(insights.get(kind) or [])
        except Exception:  # noqa: BLE001
            value = []
        if value:
            payloads[kind] = value

    try:
        questions = sanitize_questions(insights.get("suggested_questions") or [])
    except Exception:  # noqa: BLE001
        questions = []
    if questions:
        payloads["suggested_questions"] = questions
    return payloads


def _generate_demo_audio(recording: Recording, *, generate: bool) -> None:
    if not generate:
        return
    try:
        directory = storage.recording_dir(recording.uuid)
    except Exception:  # pragma: no cover - defensive
        logger.warning("cannot create media dir for demo recording %r", recording.title)
        return
    playback = directory / "playback.m4a"
    original = directory / "original.mp3"
    duration_ms = int(recording.duration_ms or 0)
    if duration_ms <= 0:
        return
    if not playback.exists():
        ok = media.generate_tone_audio(duration_ms, playback, settings.ffmpeg)
        if not ok:
            logger.warning("demo audio synthesis skipped (ffmpeg unavailable) for %r", recording.title)
            return
    try:
        if not original.exists() and playback.exists():
            shutil.copyfile(playback, original)
        recording.original_filename = f"{recording.title}.mp3"[:255]
        recording.original_ext = ".mp3"
        recording.mime_type = "audio/mpeg"
        recording.size_bytes = sum(
            path.stat().st_size for path in (playback, original) if path.exists()
        )
    except OSError as exc:  # pragma: no cover - disk issues should not break seeding
        logger.warning("demo audio bookkeeping failed for %r: %s", recording.title, exc)
        return
    try:
        from ..core.storage_backend import get_storage, original_key, playback_key

        backend = get_storage()
        if getattr(backend, "name", "local") != "local":
            storage_dir = recording.storage_dir
            backend.save_file(playback, playback_key(storage_dir), content_type="audio/mp4")
            backend.save_file(
                original,
                original_key(storage_dir, original_filename="original.mp3") ,
                content_type="audio/mpeg",
            )
            recording.size_bytes = int(backend.size(playback_key(storage_dir))) + int(
                backend.size(original_key(storage_dir, original_filename="original.mp3"))
            )
    except Exception:  # noqa: BLE001 - demo seeding must never fail because of storage
        logger.warning("demo audio upload failed for %r", recording.title, exc_info=True)


def _demo_owner_id(db: Session) -> int | None:
    owner = db.scalar(select(User).where(User.role == "admin").order_by(User.id).limit(1))
    if owner is None:
        owner = db.scalar(select(User).order_by(User.id).limit(1))
    return owner.id if owner is not None else None


def _create_demo_recording(db: Session, item: dict[str, Any]) -> Recording:
    duration_ms = max(0, int(item.get("duration_ms") or 0))
    recorded_days_ago = item.get("recorded_at_days_ago")
    try:
        recorded_days_ago = float(recorded_days_ago) if recorded_days_ago is not None else 2.0
    except (TypeError, ValueError):
        recorded_days_ago = 2.0
    now = utcnow()
    recording_uuid = str(uuid_lib.uuid4())
    insight_speakers = fixture_speaker_labels(item)
    recording = Recording(
        uuid=recording_uuid,
        title=str(item.get("title") or "演示录音")[:255],
        description=(str(item.get("description") or "") or None),
        status=RecordingStatus.READY,
        stage=Stage.DONE,
        progress=100,
        error_message=None,
        media_type=str(item.get("media_type") or "audio"),
        mime_type="audio/mpeg" if str(item.get("media_type") or "audio") == "audio" else "video/mp4",
        original_filename="",
        original_ext="",
        storage_dir=storage.storage_relpath(recording_uuid),
        size_bytes=0,
        duration_ms=duration_ms,
        recorded_at=now - timedelta(days=recorded_days_ago),
        language=str(item.get("language") or "zh"),
        source="upload",
        owner_id=_demo_owner_id(db),
        asr_provider="mock",
        llm_provider="mock",
        processed_at=now,
    )
    db.add(recording)
    db.flush()

    tag_rows: list[Tag] = []
    for raw_tag in item.get("tags") or []:
        if isinstance(raw_tag, dict):
            tag = _ensure_tag(db, str(raw_tag.get("name") or ""), str(raw_tag.get("color") or ""))
            if tag is not None and tag not in tag_rows:
                tag_rows.append(tag)
        elif isinstance(raw_tag, str):
            tag = _ensure_tag(db, raw_tag)
            if tag is not None and tag not in tag_rows:
                tag_rows.append(tag)
    recording.tags = tag_rows

    # Speakers: use the fixture list (label + display_name + color), then any
    # segment-only speaker indices get generated rows as a safety net.
    speaker_rows: dict[int, Speaker] = {}
    raw_speakers = item.get("speakers") or []
    for index, raw_speaker in enumerate(raw_speakers):
        if not isinstance(raw_speaker, dict):
            speaker_rows[index] = Speaker(
                recording_id=recording.id,
                label=f"说话人{index + 1}",
                display_name=f"说话人{index + 1}",
                color=["#5B8FF9", "#61DDAA", "#65789B", "#F6BD16"][index % 4],
                is_auto=False,
            )
        else:
            label = str(raw_speaker.get("label") or f"说话人{index + 1}")
            speaker_rows[index] = Speaker(
                recording_id=recording.id,
                label=label[:64],
                display_name=str(raw_speaker.get("display_name") or label)[:120],
                color=str(raw_speaker.get("color") or ["#5B8FF9", "#61DDAA", "#65789B", "#F6BD16"][index % 4])[:16],
                is_auto=False,
            )
        db.add(speaker_rows[index])
    db.flush()
    speakers = [row.display_name or row.label for _, row in sorted(speaker_rows.items())]
    if not speakers:
        speakers = insight_speakers

    segments = fixture_segments(item)
    for index, segment in enumerate(segments):
        speaker_index = int(segment.get("speaker_index") or 0)
        speaker = speaker_rows.get(speaker_index)
        if speaker is None:
            label = segment.get("speaker_label") or f"说话人{speaker_index + 1}"
            speaker = Speaker(
                recording_id=recording.id,
                label=label[:64],
                display_name=label[:120],
                color=["#5B8FF9", "#61DDAA", "#65789B", "#F6BD16"][speaker_index % 4],
                is_auto=False,
            )
            db.add(speaker)
            db.flush()
            speaker_rows[speaker_index] = speaker
        start = max(0, int(segment["start_ms"]))
        end = max(start + 100, int(segment["end_ms"]))
        if duration_ms:
            end = min(end, duration_ms)
        db.add(
            TranscriptSegment(
                recording_id=recording.id,
                idx=index,
                speaker_id=speaker.id,
                start_ms=start,
                end_ms=end,
                text=segment["text"],
                confidence=None,
            )
        )

    payloads = _normalize_analyses(item.get("insights") or {}, duration_ms, speakers)
    for kind, payload in payloads.items():
        db.add(
            Analysis(
                recording_id=recording.id,
                kind=kind,
                payload_json=json.dumps(payload, ensure_ascii=False),
                provider="mock",
                model="mock-llm",
            )
        )

    speaker_map = {index: row.display_name or row.label for index, row in speaker_rows.items()}
    segment_rows = [
        {
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
            "speaker": speaker_map.get(int(segment.get("speaker_index") or 0), segment.get("speaker_label") or "说话人"),
            "text": segment["text"],
        }
        for segment in segments
        if (segment.get("text") or "").strip()
    ]
    for index, payload in enumerate(build_chunk_payloads(segment_rows, duration_ms)):
        db.add(
            Chunk(
                recording_id=recording.id,
                idx=index,
                start_ms=int(payload["start_ms"]),
                end_ms=int(payload["end_ms"]),
                text=payload["text"],
                embedding=None,
                dim=None,
            )
        )

    _generate_demo_audio(recording, generate=bool(item.get("generate_audio")))
    db.commit()
    logger.info(
        "seeded demo recording %r (%s segments, %s analyses, %s chunks)",
        recording.title,
        len(segments),
        len(payloads),
        len(build_chunk_payloads(segment_rows, duration_ms)),
    )
    return recording


def seed_demo(db: Session) -> int:
    """Idempotent seed of the bundled demo library. Returns the number created."""
    data = load_demo_data()
    if not data:
        logger.warning("demo_data.json 不存在或无法解析，跳过演示数据 seed")
        return 0
    created = 0
    for item in demo_recordings():
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        exists = db.scalar(select(func.count()).select_from(Recording).where(Recording.title == title))
        if exists:
            continue
        try:
            _create_demo_recording(db, item)
            created += 1
        except Exception:  # noqa: BLE001 - one broken fixture must not block the rest
            db.rollback()
            logger.exception("failed to seed demo recording %r", title)
    return created


def ensure_mock_demo(db: Session) -> None:
    """Auto seed when ``ECHOIC_DEMO_SEED`` is on, demo mode is active and the library is empty."""
    if not settings.demo_seed:
        return
    try:
        existing = db.scalar(select(func.count()).select_from(Recording)) or 0
    except Exception:  # pragma: no cover - table might not exist yet
        logger.exception("demo seeding skipped: could not count recordings")
        return
    if existing:
        return
    try:
        cfg = load_ai_config(db)
    except Exception:  # pragma: no cover - defensive
        logger.exception("demo seeding skipped: failed to load AI config")
        return
    if not (cfg.asr.provider == "mock" or cfg.llm.provider == "mock"):
        return
    try:
        created = seed_demo(db)
    except Exception:  # pragma: no cover - never block boot
        logger.exception("demo seeding failed")
        return
    if created:
        logger.info("auto-seeded %s demo recording(s)", created)


__all__ = ["ensure_mock_demo", "seed_demo"]
