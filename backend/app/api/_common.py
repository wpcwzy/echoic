"""Shared helpers used by the HTTP routers (v0.2: roles, shares, scoped stats)."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence, TypeVar
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..core.errors import ApiError, bad_request, not_found
from ..core.media import guess_mime
from ..core.storage import StorageError, resolve_storage_dir
from ..core.storage_backend import (
    get_storage as get_media_storage,
    recording_original_key,
    recording_playback_key,
    serve_key as serve_storage_key,
)
from ..core.utils import utcnow
from ..deps import is_admin
from ..models import (
    Analysis,
    Job,
    Recording,
    RecordingStatus,
    Share,
    Tag,
    TranscriptSegment,
    User,
    recording_tags,
)
from ..schemas import (
    LibraryStats,
    RecordingSummary,
    ShareOut,
    TagWithCountOut,
    UserBriefOut,
)

logger = logging.getLogger(__name__)

#: statuses that mean "still being worked on"
ACTIVE_STATUSES = (
    RecordingStatus.UPLOADED,
    RecordingStatus.QUEUED,
    RecordingStatus.PROCESSING,
)

SummaryT = TypeVar("SummaryT", bound=RecordingSummary)


# ------------------------------------------------------------------ lookups
def get_job_or_none(db: Session, recording_id: int) -> Job | None:
    return db.scalar(
        select(Job).where(Job.recording_id == recording_id).order_by(Job.id.desc()).limit(1)
    )


def visible_owner_id(user: User | None) -> int | None:
    """``None`` means "no ownership filter" (anonymous or admin)."""
    if user is None or is_admin(user):
        return None
    return user.id


# ------------------------------------------------------------------ DTO conversion
def user_brief(user: User | None) -> UserBriefOut | None:
    if user is None:
        return None
    return UserBriefOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name or user.username,
    )


def load_owners(db: Session, recordings: Sequence[Recording]) -> dict[int, User]:
    owner_ids = {
        recording.owner_id for recording in recordings if getattr(recording, "owner_id", None)
    }
    if not owner_ids:
        return {}
    users = db.scalars(select(User).where(User.id.in_(owner_ids))).all()
    return {user.id: user for user in users}


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
    owner: User | None = None,
    cls: type[SummaryT] = RecordingSummary,
) -> SummaryT:
    summary = cls.model_validate(recording)
    summary.has_transcript = bool(has_transcript)
    summary.insight_kinds = list(insight_kinds or [])
    summary.owner_id = recording.owner_id
    summary.owner = user_brief(owner)
    return summary


def recordings_to_summaries(
    db: Session, recordings: Sequence[Recording]
) -> list[RecordingSummary]:
    ids = [recording.id for recording in recordings]
    transcript_ids = transcript_ids_for(db, ids)
    kinds = insight_kinds_for(db, ids)
    owners = load_owners(db, recordings)
    return [
        recording_to_summary(
            recording,
            has_transcript=recording.id in transcript_ids,
            insight_kinds=kinds.get(recording.id, []),
            owner=owners.get(recording.owner_id) if recording.owner_id else None,
        )
        for recording in recordings
    ]


# ------------------------------------------------------------------ scoped stats
def library_stats(db: Session, user: User | None = None, *, trashed: bool = False) -> LibraryStats:
    """Library counters. ``trashed=True`` reports the trash view instead."""
    week_ago = utcnow() - timedelta(days=7)
    stmt = select(
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
        func.coalesce(func.sum(case((Recording.created_at >= week_ago, 1), else_=0)), 0),
    )
    if trashed:
        stmt = stmt.where(Recording.deleted_at.is_not(None))
    else:
        stmt = stmt.where(Recording.deleted_at.is_(None))
    owner_id = visible_owner_id(user)
    if owner_id is not None:
        stmt = stmt.where(Recording.owner_id == owner_id)
    row = db.execute(stmt).one()
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
def tags_with_counts(
    db: Session,
    *,
    limit: int | None = None,
    order: str = "name",
    user: User | None = None,
) -> list[TagWithCountOut]:
    """Tag catalogue with recording counts scoped to the caller's visibility."""
    owner_id = visible_owner_id(user)
    count_query = (
        select(func.count(recording_tags.c.recording_id))
        .join(Recording, Recording.id == recording_tags.c.recording_id)
        .where(
            recording_tags.c.tag_id == Tag.id,
            Recording.deleted_at.is_(None),
        )
    )
    if owner_id is not None:
        count_query = count_query.where(Recording.owner_id == owner_id)
    count_column = count_query.scalar_subquery()
    stmt = select(Tag, count_column.label("recording_count"))
    if order == "count":
        stmt = stmt.order_by(count_column.desc(), Tag.name.asc())
    else:
        stmt = stmt.order_by(Tag.name.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    result: list[TagWithCountOut] = []
    for tag, count in db.execute(stmt).all():
        item = TagWithCountOut.model_validate(tag)
        item.recording_count = int(count or 0)
        result.append(item)
    return result


# ------------------------------------------------------------------ shares
def share_url_path(token: str) -> str:
    return f"/s/{token}"


def share_expired(share: Share) -> bool:
    return share.expires_at is not None and share.expires_at <= utcnow()


def share_to_out(share: Share, creator: User | None = None) -> ShareOut:
    return ShareOut(
        id=share.id,
        recording_id=share.recording_id,
        token=share.token,
        url_path=share_url_path(share.token),
        include_transcript=bool(share.include_transcript),
        include_insights=bool(share.include_insights),
        allow_download=bool(share.allow_download),
        has_password=bool(share.password_hash),
        expires_at=share.expires_at,
        expired=share_expired(share),
        revoked_at=share.revoked_at,
        view_count=int(share.view_count or 0),
        last_viewed_at=share.last_viewed_at,
        created_at=share.created_at,
        created_by=user_brief(creator),
    )


# ------------------------------------------------------------------ media streaming
def recording_storage_path(recording: Recording) -> Path | None:
    try:
        return resolve_storage_dir(recording.storage_dir)
    except StorageError:
        return None


def recording_original_file(recording: Recording) -> Path | None:
    base = recording_storage_path(recording)
    if base is None or not base.is_dir():
        return None
    if recording.original_ext:
        exact = base / f"original{recording.original_ext}"
        if exact.is_file() and exact.stat().st_size > 0:
            return exact
    for candidate in sorted(base.glob("original.*")):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def recording_playback_file(recording: Recording) -> Path | None:
    base = recording_storage_path(recording)
    if base is None:
        return None
    candidate = base / "playback.m4a"
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    return None


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single-range ``bytes=`` header; ``None`` means invalid (HTTP 416)."""
    if not header:
        return None
    value = header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[6:].strip()
    if not spec or "," in spec or "-" not in spec:
        return None
    first, last = (part.strip() for part in spec.split("-", 1))
    try:
        if first == "":
            if last == "":
                return None
            suffix = int(last)
            if suffix <= 0:
                return None
            suffix = min(suffix, size)
            start, end = size - suffix, size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        return None
    if size <= 0 or start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def range_invalid_error(size: int) -> ApiError:
    return ApiError(
        416,
        "请求的 Range 不合法",
        "VALIDATION_ERROR",
        headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
    )


def serve_media_file(
    path: Path,
    request: Request,
    *,
    content_type: str,
    filename: str,
    inline: bool,
) -> StreamingResponse:
    size = path.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition(filename, inline=inline),
        "Cache-Control": "private, max-age=0, must-revalidate",
    }
    range_header = request.headers.get("range")
    if range_header:
        parsed = parse_range(range_header, size)
        if parsed is None:
            raise range_invalid_error(size)
        start, end = parsed
        length = end - start + 1

        def iter_range():
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers.update(
            {
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
            }
        )
        return StreamingResponse(
            iter_range(), status_code=206, media_type=content_type, headers=headers
        )

    def iter_full():
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                yield chunk

    headers["Content-Length"] = str(size)
    return StreamingResponse(iter_full(), status_code=200, media_type=content_type, headers=headers)


def pick_media(recording: Recording, *, download: bool) -> tuple[Path, str, str, bool]:
    """Choose the file to stream: playback for audio, original for video/download.

    Returns ``(path, content_type, filename, attachment)``.
    """
    original = recording_original_file(recording)
    playback = recording_playback_file(recording)
    if download:
        path = original or playback
    elif recording.media_type == "video":
        path = original or playback
    else:
        path = playback or original
    if path is None:
        raise not_found("媒体文件不存在")
    is_playback = path.name.startswith("playback")
    if is_playback:
        content_type = "audio/mp4"
        filename = f"{recording.title or 'recording'}.m4a"
    else:
        content_type = recording.mime_type or guess_mime(path.suffix)
        filename = recording.original_filename or f"{recording.title or 'recording'}{path.suffix}"
    return path, content_type, filename, download


def serve_recording_media(recording: Recording, request: Request, *, download: bool):
    """Stream a recording's media through the configured storage backend.

    Local deployments keep the v0.2 file-range path; S3/MinIO deployments proxy
    Range requests (or optionally 307 to a presigned URL when enabled).
    """
    backend = get_media_storage()
    if getattr(backend, "name", "local") == "local":
        path, media_type, filename, attachment = pick_media(recording, download=download)
        return serve_media_file(
            path,
            request,
            content_type=media_type,
            filename=filename,
            inline=not attachment,
        )

    original = recording_original_key(backend, recording.storage_dir, recording.original_ext)
    playback = recording_playback_key(backend, recording.storage_dir)
    if download:
        key = original or playback
    elif recording.media_type == "video":
        key = original or playback
    else:
        key = playback or original
    if not key:
        raise not_found("媒体文件不存在")
    if key.endswith("/playback.m4a"):
        media_type = "audio/mp4"
        filename = f"{recording.title or 'recording'}.m4a"
    else:
        suffix = Path(key).suffix
        media_type = recording.mime_type or guess_mime(suffix)
        filename = recording.original_filename or f"{recording.title or 'recording'}{suffix}"
    try:
        return serve_storage_key(
            backend,
            key,
            filename=filename,
            request_headers=request.headers,
            media_type=media_type,
            download=bool(download),
        )
    except StorageError as exc:
        raise ApiError(503, f"媒体存储暂不可用：{exc}", "STORAGE_UNAVAILABLE") from exc


# ------------------------------------------------------------------ job queue bridge
def enqueue_job(db: Session, recording_id: int, from_: str = "all") -> None:
    """Ask the AI queue to (re)process a recording."""
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
    "insight_kinds_for",
    "library_stats",
    "like_pattern",
    "load_owners",
    "normalize_datetime",
    "parse_range",
    "parse_tag_ids",
    "pick_media",
    "range_invalid_error",
    "recording_original_file",
    "recording_playback_file",
    "recording_storage_path",
    "recording_to_summary",
    "recordings_to_summaries",
    "serve_media_file",
    "share_expired",
    "share_to_out",
    "share_url_path",
    "tags_with_counts",
    "title_for_download",
    "transcript_ids_for",
    "user_brief",
    "visible_owner_id",
]
