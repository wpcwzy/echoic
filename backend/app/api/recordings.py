"""Recording library endpoints (docs/08-P1 §4.2/§4.5, docs/API.md §3).

v0.2: every recording-scoped endpoint is permission checked through
``app.deps.get_recording_for_user`` (admin sees all, members only their own;
invisible resources answer 404 to avoid leaking existence).
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..core.errors import ApiError, bad_request, conflict, forbidden
from ..core.media import MediaError, guess_media_type, guess_mime, probe
from ..core.security import hash_password
from ..core.storage import (
    delete_recording_dir,
    original_path,
    safe_extension,
    save_upload,
    storage_relpath,
)
from ..core.utils import parse_iso, utcnow
from ..db import get_db
from ..deps import get_current_user, get_recording_for_user, is_admin, recording_scope
from ..models import (
    ChatMessage,
    Job,
    Recording,
    RecordingStatus,
    Share,
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
    ShareCreateIn,
    ShareListOut,
    ShareOut,
)
from ..services.settings_store import load_ai_config
from ._common import (
    ACTIVE_STATUSES,
    cancel_job,
    clean_filename,
    enqueue_job,
    get_job_or_none,
    insight_kinds_for,
    library_stats,
    like_pattern,
    normalize_datetime,
    parse_tag_ids,
    pick_media,
    recording_to_summary,
    recordings_to_summaries,
    serve_media_file,
    share_to_out,
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


def _single_summary(db: Session, recording: Recording) -> RecordingSummary:
    owner = db.get(User, recording.owner_id) if recording.owner_id else None
    transcript_ids = transcript_ids_for(db, [recording.id])
    kinds = insight_kinds_for(db, [recording.id])
    return recording_to_summary(
        recording,
        has_transcript=recording.id in transcript_ids,
        insight_kinds=kinds.get(recording.id, []),
        owner=owner,
    )


def _new_share_token(db: Session) -> str:
    for _ in range(6):
        token = secrets.token_urlsafe(24)
        if db.scalar(select(Share.id).where(Share.token == token)) is None:
            return token
    raise ApiError(500, "分享链接生成失败，请重试", "ERROR")


def _shares_out(db: Session, shares) -> list[ShareOut]:
    creator_ids = {share.created_by for share in shares if share.created_by}
    creators = (
        {user.id: user for user in db.scalars(select(User).where(User.id.in_(creator_ids))).all()}
        if creator_ids
        else {}
    )
    return [share_to_out(share, creators.get(share.created_by)) for share in shares]


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
    user: User = Depends(get_current_user),
):
    base = recording_scope(_list_query(query, status, tag_id), user)
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
        stats=library_stats(db, user),
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
    return recording_to_summary(
        recording, has_transcript=False, insight_kinds=[], owner=user
    )


@router.get("/{recording_id}", response_model=RecordingDetail)
def get_recording(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
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
    owner = db.get(User, recording.owner_id) if recording.owner_id else None
    detail = recording_to_summary(
        recording,
        has_transcript=recording_id in transcript_ids,
        insight_kinds=kinds.get(recording_id, []),
        owner=owner,
        cls=RecordingDetail,
    )
    detail.job = JobInfo.model_validate(job) if job is not None else None
    detail.chat_message_count = chat_count
    return detail


@router.patch("/{recording_id}", response_model=RecordingSummary)
def update_recording(
    recording_id: int,
    payload: RecordingUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
    fields = payload.model_fields_set
    if "title" in fields and payload.title is not None:
        recording.title = payload.title.strip()[:255] or recording.title
    if "description" in fields:
        recording.description = (payload.description or "").strip() or None
    if "recorded_at" in fields and payload.recorded_at is not None:
        recording.recorded_at = normalize_datetime(payload.recorded_at)
    if "language" in fields and payload.language is not None:
        recording.language = payload.language.strip()[:16] or recording.language
    if "owner_id" in fields:
        if not is_admin(user):
            raise forbidden("仅管理员可以转移录音归属")
        if payload.owner_id is not None:
            target = db.get(User, payload.owner_id)
            if target is None:
                raise bad_request("目标用户不存在")
            recording.owner_id = target.id
        else:
            recording.owner_id = None
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
    return _single_summary(db, recording)


@router.delete("/{recording_id}", response_model=OkOut)
def delete_recording(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
    storage_dir = recording.storage_dir
    db.delete(recording)
    db.commit()
    delete_recording_dir(storage_dir)
    logger.info("recording %s deleted by %s", recording_id, user.username)
    return OkOut()


# ------------------------------------------------------------------ process control
@router.post("/{recording_id}/reprocess")
def reprocess_recording(
    recording_id: int,
    payload: ReprocessIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
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
def cancel_recording(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
    if recording.status == RecordingStatus.CANCELLED:
        return OkOut()
    if recording.status not in ACTIVE_STATUSES:
        raise conflict("录音当前状态无法取消", "CONFLICT")
    cancel_job(db, recording_id)
    recording = get_recording_for_user(db, user, recording_id)
    recording.status = RecordingStatus.CANCELLED
    recording.progress = 0
    recording.error_message = None
    db.commit()
    logger.info("recording %s cancelled by %s", recording_id, user.username)
    return OkOut()


# ------------------------------------------------------------------ media
@router.get("/{recording_id}/media")
def get_recording_media(
    recording_id: int,
    request: Request,
    download: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
    path, content_type, filename, attachment = pick_media(recording, download=bool(download))
    return serve_media_file(
        path,
        request,
        content_type=content_type,
        filename=filename,
        inline=not attachment,
    )


@router.get("/{recording_id}/jobs", response_model=JobListOut)
def list_recording_jobs(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    get_recording_for_user(db, user, recording_id)
    jobs = list(
        db.scalars(
            select(Job).where(Job.recording_id == recording_id).order_by(Job.id.desc())
        ).all()
    )
    return JobListOut(items=[JobInfo.model_validate(job) for job in jobs])


# ------------------------------------------------------------------ shares (R-17)
@router.get("/{recording_id}/shares", response_model=ShareListOut)
def list_recording_shares(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
    shares = list(
        db.scalars(
            select(Share).where(Share.recording_id == recording.id).order_by(Share.id.desc())
        ).all()
    )
    items = _shares_out(db, shares)
    return ShareListOut(items=items, total=len(items))


@router.post("/{recording_id}/shares", response_model=ShareOut, status_code=201)
def create_recording_share(
    recording_id: int,
    payload: ShareCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
    expires_at = None
    if payload.expires_at is not None:
        expires_at = normalize_datetime(payload.expires_at)
        if expires_at <= utcnow():
            raise bad_request("expires_at 必须在未来")
    share = Share(
        recording_id=recording.id,
        token=_new_share_token(db),
        password_hash=hash_password(payload.password) if payload.password else None,
        expires_at=expires_at,
        include_transcript=bool(payload.include_transcript),
        include_insights=bool(payload.include_insights),
        allow_download=bool(payload.allow_download),
        created_by=user.id,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    logger.info("share %s created for recording %s by %s", share.id, recording.id, user.username)
    return share_to_out(share, user)
