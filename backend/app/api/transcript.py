"""Transcript endpoints (docs/API.md §3 + docs/08-P1 permission model)."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core.errors import bad_request, not_found
from ..db import get_db
from ..deps import get_current_user, get_recording_for_user
from ..models import Recording, Speaker, TranscriptSegment, User
from ..schemas import (
    SegmentOut,
    SegmentUpdateIn,
    SpeakerMergeIn,
    SpeakerMergeOut,
    SpeakerOut,
    SpeakerUpdateIn,
    TranscriptOut,
)
from ._common import content_disposition, title_for_download

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


# ------------------------------------------------------------------ helpers
def _speakers_for(db: Session, recording_id: int) -> list[Speaker]:
    return list(
        db.scalars(
            select(Speaker).where(Speaker.recording_id == recording_id).order_by(Speaker.id)
        ).all()
    )


def _segments_for(db: Session, recording_id: int) -> list[TranscriptSegment]:
    return list(
        db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.recording_id == recording_id)
            .order_by(TranscriptSegment.idx, TranscriptSegment.id)
        ).all()
    )


def _speaker_name(segment: TranscriptSegment, speakers: dict[int, Speaker]) -> str:
    speaker = speakers.get(segment.speaker_id) if segment.speaker_id else None
    if speaker is None:
        return ""
    return (speaker.display_name or speaker.label or "").strip()


def _hhmmss(ms: int) -> str:
    total = max(0, int(ms // 1000))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _srt_timestamp(ms: int) -> str:
    total = max(0, int(ms))
    millis = total % 1000
    total //= 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def build_transcript(db: Session, recording: Recording) -> TranscriptOut:
    """Shared transcript DTO builder (also used by the public share endpoints)."""
    speakers = _speakers_for(db, recording.id)
    segments = _segments_for(db, recording.id)
    return TranscriptOut(
        recording_id=recording.id,
        language=recording.language or "zh",
        duration_ms=recording.duration_ms or 0,
        speakers=[SpeakerOut.model_validate(speaker) for speaker in speakers],
        segments=[SegmentOut.model_validate(segment) for segment in segments],
    )


# ------------------------------------------------------------------ read / edit
@router.get("/{recording_id}/transcript", response_model=TranscriptOut)
def get_transcript(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
    return build_transcript(db, recording)


@router.patch("/{recording_id}/speakers/{speaker_id}", response_model=SpeakerOut)
def update_speaker(
    recording_id: int,
    speaker_id: int,
    payload: SpeakerUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    get_recording_for_user(db, user, recording_id)
    speaker = db.get(Speaker, speaker_id)
    if speaker is None or speaker.recording_id != recording_id:
        raise not_found("说话人不存在")
    fields = payload.model_fields_set
    if "display_name" in fields and payload.display_name is not None:
        speaker.display_name = payload.display_name.strip()[:120]
    if "color" in fields and payload.color is not None:
        speaker.color = payload.color.strip()[:16] or speaker.color
    db.commit()
    return SpeakerOut.model_validate(speaker)


@router.post("/{recording_id}/speakers/merge", response_model=SpeakerMergeOut)
def merge_speakers(
    recording_id: int,
    payload: SpeakerMergeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Merge diarization speakers that were split by mistake.

    All segments of ``source_ids`` are reassigned to ``target_id`` and the source
    speaker rows are deleted. User-edited text is untouched. AI analyses built
    from the previous speaker labels can be regenerated from the UI.
    """
    get_recording_for_user(db, user, recording_id)
    target = db.get(Speaker, payload.target_id)
    if target is None or target.recording_id != recording_id:
        raise not_found("目标说话人不存在")

    source_ids: list[int] = []
    for source_id in payload.source_ids:
        if source_id == payload.target_id or source_id in source_ids:
            continue
        source = db.get(Speaker, source_id)
        if source is None or source.recording_id != recording_id:
            raise bad_request(f"说话人 {source_id} 不属于该录音")
        source_ids.append(source_id)
    if not source_ids:
        raise bad_request("请选择至少一个要合并的说话人")

    sources = [db.get(Speaker, source_id) for source_id in source_ids]
    merged_segments = int(
        db.execute(
            update(TranscriptSegment)
            .where(
                TranscriptSegment.recording_id == recording_id,
                TranscriptSegment.speaker_id.in_(source_ids),
            )
            .values(speaker_id=target.id)
        ).rowcount
        or 0
    )

    target_name = (payload.target_name or "").strip()[:120]
    if target_name:
        target.display_name = target_name
    elif not (target.display_name or "").strip():
        # Keep a meaningful name when the target was an auto "说话人N".
        for source in sources:
            if source is not None and (source.display_name or "").strip():
                target.display_name = source.display_name.strip()[:120]
                break
    target.is_auto = False
    db.add(target)
    for source in sources:
        if source is not None:
            db.delete(source)
    db.commit()
    db.refresh(target)
    logger.info(
        "merged speakers %s into %s for recording %s (%s segments)",
        source_ids,
        target.id,
        recording_id,
        merged_segments,
    )
    return SpeakerMergeOut(
        speakers=[SpeakerOut.model_validate(item) for item in _speakers_for(db, recording_id)],
        merged_segments=merged_segments,
        deleted_speaker_ids=source_ids,
    )


@router.patch("/{recording_id}/segments/{segment_id}", response_model=SegmentOut)
def update_segment(
    recording_id: int,
    segment_id: int,
    payload: SegmentUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    get_recording_for_user(db, user, recording_id)
    segment = db.get(TranscriptSegment, segment_id)
    if segment is None or segment.recording_id != recording_id:
        raise not_found("转录分段不存在")
    fields = payload.model_fields_set
    if "text" in fields and payload.text is not None:
        segment.text = payload.text.strip()
    if "speaker_id" in fields:
        speaker_id = payload.speaker_id
        if speaker_id is not None:
            speaker = db.get(Speaker, speaker_id)
            if speaker is None or speaker.recording_id != recording_id:
                raise bad_request("speaker_id 不属于该录音")
        segment.speaker_id = speaker_id
    db.commit()
    return SegmentOut.model_validate(segment)


# ------------------------------------------------------------------ export
_EXPORT_META = {
    "txt": ("txt", "text/plain; charset=utf-8"),
    "srt": ("srt", "application/x-subrip; charset=utf-8"),
    "md": ("md", "text/markdown; charset=utf-8"),
    "json": ("json", "application/json; charset=utf-8"),
}


@router.get("/{recording_id}/transcript/export")
def export_transcript(
    recording_id: int,
    format: Literal["txt", "srt", "md", "json"] = Query("txt"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
    transcript = build_transcript(db, recording)
    speakers = {speaker.id: speaker for speaker in _speakers_for(db, recording_id)}

    if format == "json":
        body = transcript.model_dump_json()
    elif format == "srt":
        blocks: list[str] = []
        for index, segment in enumerate(transcript.segments, start=1):
            name = _speaker_name(segment, speakers)
            text = f"{name}：{segment.text}" if name else segment.text
            blocks.append(
                f"{index}\n{_srt_timestamp(segment.start_ms)} --> "
                f"{_srt_timestamp(segment.end_ms)}\n{text}"
            )
        body = "\n\n".join(blocks) + ("\n" if blocks else "")
    elif format == "md":
        lines = [f"# {recording.title}", ""]
        for segment in transcript.segments:
            name = _speaker_name(segment, speakers)
            prefix = (
                f"**[{_hhmmss(segment.start_ms)}] {name}**："
                if name
                else f"**[{_hhmmss(segment.start_ms)}]** "
            )
            lines.append(f"- {prefix}{segment.text}")
        body = "\n".join(lines) + "\n"
    else:
        lines = []
        for segment in transcript.segments:
            name = _speaker_name(segment, speakers)
            prefix = (
                f"[{_hhmmss(segment.start_ms)}] {name}："
                if name
                else f"[{_hhmmss(segment.start_ms)}] "
            )
            lines.append(f"{prefix}{segment.text}")
        body = "\n".join(lines) + ("\n" if lines else "")

    ext, media_type = _EXPORT_META[format]
    filename = title_for_download(recording.title or "transcript", ext)
    encoded = body.encode("utf-8")
    return Response(
        content=encoded,
        media_type=media_type,
        headers={
            "Content-Disposition": content_disposition(filename),
            "Content-Length": str(len(encoded)),
        },
    )
