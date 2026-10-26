"""Recording library endpoints (docs/08-P1, docs/10 §1, docs/API.md §3).

v0.2: every recording-scoped endpoint is permission checked through
``app.deps.get_recording_for_user`` (admin sees all, members only their own;
invisible resources answer 404 to avoid leaking existence).

v0.3: recordings are soft-deleted into the trash, can be restored or purged,
support batch operations, bind optional live-transcription sessions and expose
the live transcript.
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
from ..core.errors import ApiError, bad_request, conflict, forbidden, not_found
from ..core.media import MediaError, guess_media_type, guess_mime, probe
from ..core.security import hash_password
from ..core.storage import original_path, safe_extension, save_upload, storage_relpath
from ..core.utils import parse_iso, to_iso, utcnow
from ..db import get_db
from ..deps import (
    get_current_user,
    get_recording_any,
    get_recording_for_user,
    is_admin,
    recording_scope,
)
from ..models import (
    ChatMessage,
    Job,
    LiveSegment,
    LiveSession,
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
    BatchActionIn,
    BatchItemResult,
    BatchOut,
    JobInfo,
    LiveSegmentOut,
    LiveSessionOut,
    OkOut,
    RecordingDetail,
    RecordingListOut,
    RecordingLiveOut,
    RecordingSummary,
    RecordingUpdateIn,
    ReprocessIn,
    ShareCreateIn,
    ShareListOut,
    ShareOut,
)
from ..services import trash as trash_service
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
    recording_to_summary,
    recordings_to_summaries,
    serve_recording_media,
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

_TRASH_ORDER_BY = (Recording.deleted_at.desc(), Recording.id.desc())


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


def _queue_reprocess(db: Session, recording: Recording, from_stage: str) -> None:
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
    enqueue_job(db, recording.id, from_stage)


# ------------------------------------------------------------------ live helpers
def _live_session_out(session: LiveSession) -> LiveSessionOut:
    return LiveSessionOut(
        id=session.id,
        uuid=session.uuid,
        status=session.status,
        provider=session.provider or "",
        model=session.model or "",
        language=session.language or "zh",
        merge_mode=session.merge_mode if session.merge_mode in ("replace", "promote") else "replace",
        recording_id=session.recording_id,
        started_at=session.started_at,
        ended_at=session.ended_at,
        total_audio_ms=int(session.total_audio_ms or 0),
        error=session.error,
        ws_url=None,
    )


def _live_segment_out(segment: LiveSegment) -> LiveSegmentOut:
    return LiveSegmentOut(
        id=segment.id,
        idx=segment.idx,
        start_ms=int(segment.start_ms or 0),
        end_ms=int(segment.end_ms or 0),
        text=segment.text or "",
        is_final=bool(segment.is_final),
        speaker_label=segment.speaker_label,
    )


def _live_segments_for(db: Session, session_id: int) -> list[LiveSegmentOut]:
    rows = db.scalars(
        select(LiveSegment)
        .where(LiveSegment.session_id == session_id)
        .order_by(LiveSegment.idx, LiveSegment.id)
    ).all()
    return [_live_segment_out(row) for row in rows]


def _load_live_session_for_upload(
    db: Session, user: User, live_session_id: int
) -> LiveSession:
    try:
        from ..services import live as live_service

        session = live_service.get_session_for_user(db, user, live_session_id)
    except ImportError:  # pragma: no cover - services/live lands with backend-ai
        session = db.get(LiveSession, live_session_id)
        if session is None or (not is_admin(user) and session.user_id != user.id):
            raise not_found("实时会话不存在")
    if session.recording_id is not None:
        raise conflict("实时会话已绑定录音", "CONFLICT")
    if session.status not in ("active", "ended"):
        raise bad_request("实时会话状态不可用")
    return session


# ------------------------------------------------------------------ list / detail
@router.get("", response_model=RecordingListOut)
def list_recordings(
    query: str | None = Query(None, description="标题/描述/转录全文模糊搜索"),
    status: str | None = Query(None, description="逗号分隔的状态，如 ready,processing"),
    tag_id: int | None = Query(None),
    sort: Literal["recent", "oldest", "title", "duration"] = Query("recent"),
    trashed: bool = Query(False, description="回收站视图（仅已软删除的录音）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    base = recording_scope(_list_query(query, status, tag_id), user, include_deleted=trashed)
    if trashed:
        base = base.where(Recording.deleted_at.is_not(None))
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    order_by = _TRASH_ORDER_BY if (trashed and sort == "recent") else _ORDER_BY[sort]
    stmt = (
        base.options(
            selectinload(Recording.tags),
            selectinload(Recording.speakers),
            selectinload(Recording.analyses),
        )
        .order_by(*order_by)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list(db.scalars(stmt).all())
    return RecordingListOut(
        items=recordings_to_summaries(db, rows),
        total=total,
        page=page,
        page_size=page_size,
        stats=library_stats(db, user, trashed=trashed),
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
    live_session_id: int | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    filename = clean_filename(file.filename, fallback="recording")
    ext = safe_extension(filename)  # raises 415 for unsupported extensions
    if source not in ("upload", "browser_recording"):
        raise bad_request("source 只能是 upload 或 browser_recording")
    selected_tags = parse_tag_ids(tag_ids)
    live_session = (
        _load_live_session_for_upload(db, user, int(live_session_id))
        if live_session_id is not None
        else None
    )

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
    from ..core.storage_backend import get_storage
    from ..core.storage_backend import original_key as storage_original_key

    media_storage = get_storage()
    remote_storage = getattr(media_storage, "name", "local") != "local"
    if remote_storage:
        temp_dir = settings.data_path / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        destination = temp_dir / f"upload-{recording_uuid}{ext}"
        try:
            size = save_upload(file.file, destination, max_bytes)
            try:
                info = probe(destination, ffprobe_bin=settings.ffprobe)
            except MediaError as exc:
                raise ApiError(415, f"无法解析媒体文件：{exc}", "UNSUPPORTED_MEDIA") from exc
            if not info.has_audio and not info.has_video:
                raise ApiError(415, "文件中没有可用的音视频流", "UNSUPPORTED_MEDIA")
            if size <= 0:
                raise ApiError(400, "上传文件为空", "VALIDATION_ERROR")
            try:
                stored_size = media_storage.save_file(
                    destination,
                    storage_original_key(storage_dir, filename, ext),
                    content_type=info.mime_type or guess_mime(ext),
                )
                if stored_size:
                    size = int(stored_size)
            except Exception as exc:  # noqa: BLE001 - storage errors become friendly 503s
                raise ApiError(503, f"无法保存到对象存储：{exc}", "STORAGE_UNAVAILABLE") from exc
        except BaseException:
            delete_media_quietly(storage_dir)
            raise
        finally:
            destination.unlink(missing_ok=True)
    else:
        destination = original_path(storage_dir, filename, recording_uuid)
        try:
            size = save_upload(file.file, destination, max_bytes)  # raises 413 when too large
        except BaseException:
            delete_media_quietly(storage_dir)
            raise
        try:
            info = probe(destination, ffprobe_bin=settings.ffprobe)
        except MediaError as exc:
            delete_media_quietly(storage_dir)
            raise ApiError(415, f"无法解析媒体文件：{exc}", "UNSUPPORTED_MEDIA")
        if not info.has_audio and not info.has_video:
            delete_media_quietly(storage_dir)
            raise ApiError(415, "文件中没有可用的音视频流", "UNSUPPORTED_MEDIA")
        if size <= 0:
            delete_media_quietly(storage_dir)
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
        delete_media_quietly(storage_dir)
        raise

    if live_session is not None:
        live_session.recording_id = recording.id
        if live_session.status == "active":
            live_session.status = "ended"
            live_session.ended_at = live_session.ended_at or utcnow()
        db.add(live_session)
        db.commit()
        logger.info(
            "live session %s bound to recording %s", live_session.id, recording.id
        )

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


def delete_media_quietly(storage_dir: str) -> None:
    try:
        from ..core.storage_backend import get_storage

        backend = get_storage()
        if getattr(backend, "name", "local") != "local":
            backend.delete_prefix(storage_dir)
            return
    except Exception:  # noqa: BLE001 - fall back to the local directory removal
        logger.warning("object storage cleanup failed for %s", storage_dir, exc_info=True)
    from ..core.storage import delete_recording_dir

    delete_recording_dir(storage_dir)


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


# ------------------------------------------------------------------ trash (R-23)
@router.delete("/{recording_id}")
def trash_recording(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
    deleted = trash_service.soft_delete(db, recording, user_id=user.id)
    before = trash_service.restore_before(deleted)
    logger.info("recording %s trashed by %s", recording_id, user.username)
    return {
        "ok": True,
        "id": recording_id,
        "deleted_at": to_iso(deleted.deleted_at),
        "restore_before": to_iso(before),
    }


@router.post("/{recording_id}/restore", response_model=RecordingSummary)
def restore_recording(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_any(db, user, recording_id)
    if recording.deleted_at is None:
        raise not_found("录音不在回收站")
    trash_service.restore(db, recording)
    return _single_summary(db, recording)


@router.delete("/{recording_id}/purge")
def purge_recording(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_any(db, user, recording_id)
    if recording.deleted_at is None:
        raise not_found("录音不在回收站")
    trash_service.purge_recording(db, recording)
    logger.info("recording %s purged by %s", recording_id, user.username)
    return {"ok": True, "purged": True}


# ------------------------------------------------------------------ batch (R-23)
def _apply_batch_item(
    db: Session,
    user: User,
    action: str,
    recording_id: int,
    payload: BatchActionIn,
    tags: list[Tag],
) -> None:
    if action == "trash":
        recording = get_recording_for_user(db, user, recording_id)
        trash_service.soft_delete(db, recording, user_id=user.id)
        return
    if action == "restore":
        recording = get_recording_any(db, user, recording_id)
        if recording.deleted_at is None:
            raise not_found("录音不在回收站")
        trash_service.restore(db, recording)
        return
    if action == "purge":
        recording = get_recording_any(db, user, recording_id)
        if recording.deleted_at is None:
            raise not_found("录音不在回收站")
        trash_service.purge_recording(db, recording)
        return
    if action == "reprocess":
        recording = get_recording_for_user(db, user, recording_id)
        _queue_reprocess(db, recording, payload.from_)
        return
    if action in ("tag_add", "tag_remove"):
        recording = get_recording_for_user(db, user, recording_id)
        if action == "tag_add":
            existing = {tag.id for tag in recording.tags}
            for tag in tags:
                if tag.id not in existing:
                    recording.tags.append(tag)
        else:
            remove_ids = {tag.id for tag in tags}
            recording.tags = [tag for tag in recording.tags if tag.id not in remove_ids]
        db.commit()
        return
    raise bad_request(f"不支持的批量操作：{action}")


@router.post("/batch", response_model=BatchOut)
def batch_recordings(
    payload: BatchActionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    action = payload.action
    limit = max(1, int(settings.batch_max_items or 100))
    recording_ids: list[int] = []
    for raw_id in payload.recording_ids:
        value = int(raw_id)
        if value not in recording_ids:
            recording_ids.append(value)
    if len(recording_ids) > limit:
        raise bad_request(f"批量操作最多支持 {limit} 条录音")

    tags: list[Tag] = []
    if action in ("tag_add", "tag_remove"):
        if not is_admin(user):
            raise forbidden("仅管理员可以批量修改标签")
        if not payload.tag_ids:
            raise bad_request("tag_ids 不能为空")
        tags = list(db.scalars(select(Tag).where(Tag.id.in_(payload.tag_ids))).all())
        if not tags:
            raise bad_request("标签不存在")

    results: list[BatchItemResult] = []
    succeeded = 0
    failed = 0
    for recording_id in recording_ids:
        try:
            _apply_batch_item(db, user, action, recording_id, payload, tags)
        except ApiError as exc:
            db.rollback()
            failed += 1
            results.append(BatchItemResult(id=recording_id, ok=False, error=exc.message))
        except Exception:  # noqa: BLE001 - one item must not abort the batch
            db.rollback()
            failed += 1
            logger.exception("batch %s failed for recording %s", action, recording_id)
            results.append(BatchItemResult(id=recording_id, ok=False, error="操作失败"))
        else:
            succeeded += 1
            results.append(BatchItemResult(id=recording_id, ok=True))
    return BatchOut(
        action=action,
        succeeded=succeeded,
        failed=failed,
        results=results,
    )


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
    _queue_reprocess(db, recording, from_stage)
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
    return serve_recording_media(recording, request, download=bool(download))


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


# ------------------------------------------------------------------ live transcript (R-19)
@router.get("/{recording_id}/live", response_model=RecordingLiveOut)
def get_recording_live(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = get_recording_for_user(db, user, recording_id)
    session = db.scalar(
        select(LiveSession)
        .where(LiveSession.recording_id == recording.id)
        .order_by(LiveSession.id.desc())
        .limit(1)
    )
    if session is None:
        raise not_found("该录音没有实时会话记录")
    try:
        from ..services import live as live_service

        return RecordingLiveOut(
            session=LiveSessionOut.model_validate(live_service.session_out_payload(session)),
            segments=[
                LiveSegmentOut.model_validate(live_service.segment_out_payload(segment))
                for segment in live_service.list_live_segments(db, session.id)
            ],
        )
    except ImportError:  # pragma: no cover - services/live lands with backend-ai
        return RecordingLiveOut(
            session=_live_session_out(session),
            segments=_live_segments_for(db, session.id),
        )


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
