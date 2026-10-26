"""Recording library endpoints (docs/API.md §3)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..core.errors import ApiError, bad_request, conflict, not_found
from ..core.media import MediaError, guess_media_type, guess_mime, probe
from ..core.storage import (
    StorageError,
    delete_recording_dir,
    original_path,
    resolve_storage_dir,
    safe_extension,
    save_upload,
    storage_relpath,
)
from ..core.utils import parse_iso, utcnow
from ..db import get_db
from ..deps import get_current_user
from ..models import (
    ChatMessage,
    Job,
    Recording,
    RecordingStatus,
    Speaker,
    Stage,
    Tag,
    TranscriptSegment,
    User,
)
from ..schemas import (
    JobInfo,
    OkOut,
    RecordingDetail,
    RecordingListOut,
    RecordingSummary,
    RecordingUpdateIn,
    ReprocessIn,
)
from ..services.settings_store import load_ai_config
from ._common import (
    ACTIVE_STATUSES,
    cancel_job,
    clean_filename,
    content_disposition,
    enqueue_job,
    get_job_or_none,
    get_recording_or_404,
    insight_kinds_for,
    library_stats,
    like_pattern,
    normalize_datetime,
    parse_tag_ids,
    recording_to_summary,
    recordings_to_summaries,
    transcript_ids_for,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


class JobListOut(BaseModel):
    items: list[JobInfo]


# ------------------------------------------------------------------ query helpers
def _status_values(raw: str | None) -> list[str]:
    return [part.strip().lower() for part in (raw or "").split(",") if part.strip()]


def _list_query(query: str | None, status: str | None, tag_id: int | None):
    stmt = select(Recording)
    if query and query.strip():
        pattern = like_pattern(query.strip())
        segment_match = (
            select(TranscriptSegment.id)
            .where(
                TranscriptSegment.recording_id == Recording.id,
                TranscriptSegment.text.ilike(pattern, escape="\\"),
            )
            .exists()
        )
        stmt = stmt.where(
            or_(
                Recording.title.ilike(pattern, escape="\\"),
                Recording.description.ilike(pattern, escape="\\"),
                segment_match,
            )
        )
    statuses = _status_values(status)
    if statuses:
        stmt = stmt.where(Recording.status.in_(statuses))
    if tag_id is not None:
        stmt = stmt.where(Recording.tags.any(Tag.id == tag_id))
    return stmt


_ORDER_BY = {
    "recent": (Recording.recorded_at.desc(), Recording.id.desc()),
    "oldest": (Recording.recorded_at.asc(), Recording.id.asc()),
    "title": (Recording.title.asc(), Recording.id.asc()),
    "duration": (Recording.duration_ms.desc(), Recording.id.desc()),
}


# ------------------------------------------------------------------ media helpers
def _storage_path(recording: Recording) -> Path | None:
    try:
        return resolve_storage_dir(recording.storage_dir)
    except StorageError:
        return None


def _original_file(recording: Recording) -> Path | None:
    base = _storage_path(recording)
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


def _playback_file(recording: Recording) -> Path | None:
    base = _storage_path(recording)
    if base is None:
        return None
    candidate = base / "playback.m4a"
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    return None


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
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


def _range_invalid(size: int) -> ApiError:
    return ApiError(
        416,
        "请求的 Range 不合法",
        "VALIDATION_ERROR",
        headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
    )


def _serve_file(
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
        parsed = _parse_range(range_header, size)
        if parsed is None:
            raise _range_invalid(size)
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


def _pick_media(recording: Recording, *, download: bool) -> tuple[Path, str, str, bool]:
    original = _original_file(recording)
    playback = _playback_file(recording)
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


# ------------------------------------------------------------------ list / detail
@router.get("", response_model=RecordingListOut)
def list_recordings(
    query: str | None = Query(None, description="标题/描述/转录全文模糊搜索"),
    status: str | None = Query(None, description="逗号分隔的状态，如 ready,processing"),
    tag_id: int | None = Query(None),
    sort: Literal["recent", "oldest", "title", "duration"] = Query("recent"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    db: Session = Depends(get_db),
):
    base = _list_query(query, status, tag_id)
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    stmt = (
        base.options(
            selectinload(Recording.tags),
            selectinload(Recording.speakers),
            selectinload(Recording.analyses),
        )
        .order_by(*_ORDER_BY[sort])
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list(db.scalars(stmt).all())
    return RecordingListOut(
        items=recordings_to_summaries(db, rows),
        total=total,
        page=page,
        page_size=page_size,
        stats=library_stats(db),
    )


@router.post("/upload", response_model=RecordingSummary, status_code=201)
def upload_recording(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    description: str | None = Form(None),
    recorded_at: str | None = Form(None),
    language: str | None = Form(None),
    tag_ids: str | None = Form(None),
    source: str = Form("upload"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    filename = clean_filename(file.filename, fallback="recording")
    ext = safe_extension(filename)  # raises 415 for unsupported extensions
    if source not in ("upload", "browser_recording"):
        raise bad_request("source 只能是 upload 或 browser_recording")
    selected_tags = parse_tag_ids(tag_ids)

    config = load_ai_config(db)
    max_mb = int(config.system.get("max_upload_mb") or settings.max_upload_mb)
    max_bytes = max_mb * 1024 * 1024

    recorded = utcnow()
    if recorded_at and recorded_at.strip():
        parsed = parse_iso(recorded_at)
        if parsed is None:
            raise bad_request("recorded_at 不是合法的 ISO-8601 时间")
        recorded = normalize_datetime(parsed)
    lang = (language or config.system.get("default_language") or "zh").strip()[:16] or "zh"

    recording_uuid = str(uuid4())
    storage_dir = storage_relpath(recording_uuid)
    destination = original_path(storage_dir, filename, recording_uuid)
    try:
        size = save_upload(file.file, destination, max_bytes)  # raises 413 when too large
    except BaseException:
        delete_recording_dir(storage_dir)
        raise
    try:
        info = probe(destination, ffprobe_bin=settings.ffprobe)
    except MediaError as exc:
        delete_recording_dir(storage_dir)
        raise ApiError(415, f"无法解析媒体文件：{exc}", "UNSUPPORTED_MEDIA")
    if not info.has_audio and not info.has_video:
        delete_recording_dir(storage_dir)
        raise ApiError(415, "文件中没有可用的音视频流", "UNSUPPORTED_MEDIA")
    if size <= 0:
        delete_recording_dir(storage_dir)
        raise ApiError(400, "上传文件为空", "VALIDATION_ERROR")

    stem = Path(filename).stem.strip() or "未命名录音"
    requested_title = clean_filename(title, fallback="") if title else ""
    final_title = (requested_title or stem)[:255] or "未命名录音"

    tags: list[Tag] = []
    if selected_tags:
        tags = list(db.scalars(select(Tag).where(Tag.id.in_(selected_tags))).all())

    auto_process = bool(config.system.get("auto_process", True))
    recording = Recording(
        uuid=recording_uuid,
        title=final_title,
        description=(description or "").strip() or None,
        status=RecordingStatus.QUEUED if auto_process else RecordingStatus.UPLOADED,
        stage=Stage.PREPARING,
        progress=0,
        media_type=info.media_type or guess_media_type(ext),
        mime_type=info.mime_type or guess_mime(ext),
        original_filename=filename,
        original_ext=ext,
        storage_dir=storage_dir,
        size_bytes=size,
        duration_ms=int(info.duration_ms or 0),
        recorded_at=recorded,
        language=lang,
        source=source,
        owner_id=user.id,
    )
    if tags:
        recording.tags = tags
    try:
        db.add(recording)
        db.commit()
    except Exception:
        db.rollback()
        delete_recording_dir(storage_dir)
        raise

    if auto_process:
        enqueue_job(db, recording.id, "all")
    logger.info(
        "recording %s uploaded (%s, %s bytes, %s ms) by user %s",
        recording.id,
        filename,
        size,
        recording.duration_ms,
        user.username,
    )
    return recording_to_summary(recording, has_transcript=False, insight_kinds=[])


@router.get("/{recording_id}", response_model=RecordingDetail)
def get_recording(recording_id: int, db: Session = Depends(get_db)):
    recording = get_recording_or_404(db, recording_id)
    job = get_job_or_none(db, recording_id)
    chat_count = int(
        db.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.recording_id == recording_id)
        )
        or 0
    )
    transcript_ids = transcript_ids_for(db, [recording_id])
    kinds = insight_kinds_for(db, [recording_id])
    detail = recording_to_summary(
        recording,
        has_transcript=recording_id in transcript_ids,
        insight_kinds=kinds.get(recording_id, []),
        cls=RecordingDetail,
    )
    detail.job = JobInfo.model_validate(job) if job is not None else None
    detail.chat_message_count = chat_count
    return detail


@router.patch("/{recording_id}", response_model=RecordingSummary)
def update_recording(
    recording_id: int, payload: RecordingUpdateIn, db: Session = Depends(get_db)
):
    recording = get_recording_or_404(db, recording_id)
    fields = payload.model_fields_set
    if "title" in fields and payload.title is not None:
        recording.title = payload.title.strip()[:255] or recording.title
    if "description" in fields:
        recording.description = (payload.description or "").strip() or None
    if "recorded_at" in fields and payload.recorded_at is not None:
        recording.recorded_at = normalize_datetime(payload.recorded_at)
    if "language" in fields and payload.language is not None:
        recording.language = payload.language.strip()[:16] or recording.language
    if "tag_ids" in fields and payload.tag_ids is not None:
        ids = [int(tag_id) for tag_id in payload.tag_ids]
        tags = list(db.scalars(select(Tag).where(Tag.id.in_(ids))).all()) if ids else []
        recording.tags = tags
    if "speaker_names" in fields and payload.speaker_names:
        for raw_id, display_name in payload.speaker_names.items():
            try:
                speaker_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            speaker = db.get(Speaker, speaker_id)
            if speaker is None or speaker.recording_id != recording.id:
                continue
            speaker.display_name = (display_name or "").strip()[:120]
    db.commit()

    transcript_ids = transcript_ids_for(db, [recording_id])
    kinds = insight_kinds_for(db, [recording_id])
    return recording_to_summary(
        recording,
        has_transcript=recording_id in transcript_ids,
        insight_kinds=kinds.get(recording_id, []),
    )


@router.delete("/{recording_id}", response_model=OkOut)
def delete_recording(recording_id: int, db: Session = Depends(get_db)):
    recording = get_recording_or_404(db, recording_id)
    storage_dir = recording.storage_dir
    db.delete(recording)
    db.commit()
    delete_recording_dir(storage_dir)
    logger.info("recording %s deleted", recording_id)
    return OkOut()


# ------------------------------------------------------------------ process control
@router.post("/{recording_id}/reprocess")
def reprocess_recording(
    recording_id: int,
    payload: ReprocessIn | None = None,
    db: Session = Depends(get_db),
):
    recording = get_recording_or_404(db, recording_id)
    from_stage = payload.from_ if payload is not None else "all"
    stage = {
        "insights": Stage.ANALYZING,
        "embeddings": Stage.EMBEDDING,
    }.get(from_stage, Stage.PREPARING)
    recording.status = RecordingStatus.QUEUED
    recording.stage = stage
    recording.progress = 0
    recording.error_message = None
    recording.processed_at = None
    db.commit()
    enqueue_job(db, recording_id, from_stage)
    logger.info("recording %s queued for reprocess (%s)", recording_id, from_stage)
    return {"ok": True, "status": RecordingStatus.QUEUED}


@router.post("/{recording_id}/cancel", response_model=OkOut)
def cancel_recording(recording_id: int, db: Session = Depends(get_db)):
    recording = get_recording_or_404(db, recording_id)
    if recording.status == RecordingStatus.CANCELLED:
        return OkOut()
    if recording.status not in ACTIVE_STATUSES:
        raise conflict("录音当前状态无法取消", "CONFLICT")
    cancel_job(db, recording_id)
    recording = get_recording_or_404(db, recording_id)
    recording.status = RecordingStatus.CANCELLED
    recording.progress = 0
    recording.error_message = None
    db.commit()
    logger.info("recording %s cancelled", recording_id)
    return OkOut()


# ------------------------------------------------------------------ media
@router.get("/{recording_id}/media")
def get_recording_media(
    recording_id: int,
    request: Request,
    download: bool = Query(False),
    db: Session = Depends(get_db),
):
    recording = get_recording_or_404(db, recording_id)
    path, content_type, filename, attachment = _pick_media(recording, download=bool(download))
    return _serve_file(
        path,
        request,
        content_type=content_type,
        filename=filename,
        inline=not attachment,
    )


@router.get("/{recording_id}/jobs", response_model=JobListOut)
def list_recording_jobs(recording_id: int, db: Session = Depends(get_db)):
    get_recording_or_404(db, recording_id)
    jobs = list(
        db.scalars(
            select(Job).where(Job.recording_id == recording_id).order_by(Job.id.desc())
        ).all()
    )
    return JobListOut(items=[JobInfo.model_validate(job) for job in jobs])
