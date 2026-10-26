"""Shared helpers used by the HTTP routers.

Kept deliberately small: lookups, DTO conversion, stats and job-queue bridges.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Sequence, TypeVar
from urllib.parse import quote

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..core.errors import bad_request, not_found
from ..core.utils import utcnow
from ..models import (
    Analysis,
    Job,
    Recording,
    RecordingStatus,
    Tag,
    TranscriptSegment,
    recording_tags,
)
from ..schemas import LibraryStats, RecordingSummary, TagWithCountOut

logger = logging.getLogger(__name__)

#: statuses that mean "still being worked on"
ACTIVE_STATUSES = (
    RecordingStatus.UPLOADED,
    RecordingStatus.QUEUED,
    RecordingStatus.PROCESSING,
)

SummaryT = TypeVar("SummaryT", bound=RecordingSummary)


# ------------------------------------------------------------------ lookups
def get_recording_or_404(db: Session, recording_id: int) -> Recording:
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise not_found("录音不存在")
    return recording


def get_job_or_none(db: Session, recording_id: int) -> Job | None:
    return db.scalar(
        select(Job).where(Job.recording_id == recording_id).order_by(Job.id.desc()).limit(1)
    )


# ------------------------------------------------------------------ DTO
def transcript_ids_for(db: Session, recording_ids: Sequence[int]) -> set[int]:
    ids = [int(i) for i in recording_ids]
    if not ids:
        return set()
    rows = db.scalars(
        select(TranscriptSegment.recording_id)
        .where(TranscriptSegment.recording_id.in_(ids))
        .distinct()
    )
    return set(rows.all())


def insight_kinds_for(db: Session, recording_ids: Sequence[int]) -> dict[int, list[str]]:
    ids = [int(i) for i in recording_ids]
    if not ids:
        return {}
    rows = db.execute(
        select(Analysis.recording_id, Analysis.kind).where(Analysis.recording_id.in_(ids))
    ).all()
    result: dict[int, list[str]] = defaultdict(list)
    for recording_id, kind in rows:
        result[int(recording_id)].append(kind)
    return {key: sorted(value) for key, value in result.items()}


def recording_to_summary(
    recording: Recording,
    *,
    has_transcript: bool = False,
    insight_kinds: list[str] | None = None,
    cls: type[SummaryT] = RecordingSummary,
) -> SummaryT:
    summary = cls.model_validate(recording)
    summary.has_transcript = bool(has_transcript)
    summary.insight_kinds = list(insight_kinds or [])
    return summary


def recordings_to_summaries(
    db: Session, recordings: Sequence[Recording]
) -> list[RecordingSummary]:
    ids = [recording.id for recording in recordings]
    transcript_ids = transcript_ids_for(db, ids)
    kinds = insight_kinds_for(db, ids)
    return [
        recording_to_summary(
            recording,
            has_transcript=recording.id in transcript_ids,
            insight_kinds=kinds.get(recording.id, []),
        )
        for recording in recordings
    ]


# ------------------------------------------------------------------ stats
def library_stats(db: Session) -> LibraryStats:
    week_ago = utcnow() - timedelta(days=7)
    row = db.execute(
        select(
            func.count(Recording.id),
            func.coalesce(
                func.sum(case((Recording.status == RecordingStatus.READY, 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((Recording.status.in_(ACTIVE_STATUSES), 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((Recording.status == RecordingStatus.FAILED, 1), else_=0)), 0
            ),
            func.coalesce(func.sum(Recording.duration_ms), 0),
            func.coalesce(
                func.sum(case((Recording.created_at >= week_ago, 1), else_=0)), 0
            ),
        )
    ).one()
    total, ready, processing, failed, total_duration_ms, this_week = (
        int(row[0] or 0),
        int(row[1] or 0),
        int(row[2] or 0),
        int(row[3] or 0),
        int(row[4] or 0),
        int(row[5] or 0),
    )
    return LibraryStats(
        total=total,
        ready=ready,
        processing=processing,
        failed=failed,
        total_duration_ms=total_duration_ms,
        this_week=this_week,
        hours=round(total_duration_ms / 3_600_000, 1),
    )


# ------------------------------------------------------------------ text helpers
def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_pattern(value: str) -> str:
    return f"%{escape_like(value)}%"


def normalize_datetime(value: datetime) -> datetime:
    """Store naive UTC datetimes (SQLite has no timezone support)."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def clean_filename(name: str | None, fallback: str = "recording") -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name[:255] or fallback


def parse_tag_ids(raw: str | None) -> list[int]:
    ids: list[int] = []
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError:
            raise bad_request(f"tag_ids 参数不合法：{piece}")
    return ids


def content_disposition(filename: str, *, inline: bool = False) -> str:
    """RFC 5987 compliant Content-Disposition with an ASCII fallback."""
    safe_name = filename or "download"
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", safe_name).strip()
    if not re.search(r"[A-Za-z0-9]", fallback):
        extension = safe_name.rsplit(".", 1)[-1] if "." in safe_name else ""
        extension = re.sub(r"[^A-Za-z0-9]", "", extension)
        fallback = f"download.{extension}" if extension else "download"
    quoted = quote(safe_name, safe="")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


def title_for_download(title: str, ext: str) -> str:
    name = (title or "").strip() or "transcript"
    return f"{name}.{ext.lstrip('.')}"


# ------------------------------------------------------------------ tags
def tags_with_counts(db: Session, *, limit: int | None = None, order: str = "name") -> list[TagWithCountOut]:
    """All tags with their recording count (``order``: name|count)."""
    count_column = func.count(recording_tags.c.recording_id)
    stmt = (
        select(Tag, count_column)
        .outerjoin(recording_tags, recording_tags.c.tag_id == Tag.id)
        .group_by(Tag.id)
    )
    if order == "count":
        stmt = stmt.order_by(count_column.desc(), Tag.name.asc())
    else:
        stmt = stmt.order_by(Tag.name.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = db.execute(stmt).all()
    result: list[TagWithCountOut] = []
    for tag, count in rows:
        item = TagWithCountOut.model_validate(tag)
        item.recording_count = int(count or 0)
        result.append(item)
    return result


# ------------------------------------------------------------------ job queue bridge
def enqueue_job(db: Session, recording_id: int, from_: str = "all") -> None:
    """Ask the AI queue to (re)process a recording.

    ``app.services.queue`` is owned by backend-ai; while it is still a stub the
    call raises ``NotImplementedError`` which must not break the HTTP layer.
    """
    try:
        from ..services import queue as queue_service

        queue_service.enqueue(db, recording_id, from_=from_)
    except NotImplementedError:  # pragma: no cover - transitional stub
        logger.warning("queue.enqueue 尚未实现，录音 %s 保持待处理状态", recording_id)


def cancel_job(db: Session, recording_id: int) -> bool:
    try:
        from ..services import queue as queue_service

        return bool(queue_service.request_cancel(db, recording_id))
    except NotImplementedError:  # pragma: no cover - transitional stub
        return False


__all__ = [
    "ACTIVE_STATUSES",
    "cancel_job",
    "clean_filename",
    "content_disposition",
    "enqueue_job",
    "escape_like",
    "get_job_or_none",
    "get_recording_or_404",
    "insight_kinds_for",
    "library_stats",
    "like_pattern",
    "normalize_datetime",
    "parse_tag_ids",
    "recording_to_summary",
    "recordings_to_summaries",
    "tags_with_counts",
    "transcript_ids_for",
    "title_for_download",
]
