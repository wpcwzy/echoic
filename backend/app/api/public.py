"""Public share endpoints — NO authentication required (docs/08-P1 §4.3).

A share link exposes a read-only view of one recording. Password-protected
shares issue a short lived, path-scoped HttpOnly cookie after a successful
``POST .../unlock``; the JWT is signed with the instance secret and bound to the
share id + token so it cannot unlock a different share.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..core.errors import ApiError, forbidden, not_found, unauthorized
from ..core.security import verify_password
from ..core.utils import utcnow
from ..db import get_db
from ..models import Recording, Share, Speaker
from ..schemas import (
    InsightsOut,
    PublicRecordingOut,
    PublicShareInfoOut,
    PublicShareOut,
    ShareUnlockIn,
    ShareUnlockOut,
    SpeakerOut,
    TagOut,
    TranscriptOut,
)
from ._common import pick_media, serve_media_file
from .insights import build_insights
from .transcript import build_transcript

logger = logging.getLogger(__name__)

router = APIRouter()

SHARE_COOKIE_NAME = "echoic_share"
SHARE_COOKIE_MAX_AGE = 7200
_UNLOCK_WINDOW_SECONDS = 60
_UNLOCK_MAX_ATTEMPTS = 5

_unlock_attempts: dict[str, list[float]] = {}
_unlock_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or "unknown"


def _throttle_key(ip: str, token: str) -> str:
    return f"{ip}:{token}"


def _unlock_allowed(key: str) -> bool:
    now = time.time()
    with _unlock_lock:
        attempts = [t for t in _unlock_attempts.get(key, []) if now - t < _UNLOCK_WINDOW_SECONDS]
        _unlock_attempts[key] = attempts
        return len(attempts) < _UNLOCK_MAX_ATTEMPTS


def _register_unlock_attempt(key: str) -> None:
    with _unlock_lock:
        _unlock_attempts.setdefault(key, []).append(time.time())


def _reset_unlock_attempts(key: str) -> None:
    with _unlock_lock:
        _unlock_attempts.pop(key, None)


def _share_or_404(db: Session, token: str) -> Share:
    share = db.scalar(select(Share).where(Share.token == token))
    if share is None:
        raise not_found("分享链接不存在或已失效")
    if share.revoked_at is not None:
        raise not_found("分享链接已被撤销")
    if share.expires_at is not None and share.expires_at <= utcnow():
        raise not_found("分享链接已过期")
    return share


def _recording_for_share(db: Session, share: Share) -> Recording:
    recording = db.get(Recording, share.recording_id)
    if recording is None:
        raise not_found("分享链接不存在或已失效")
    return recording


def _touch_view(db: Session, share: Share) -> None:
    share.view_count = int(share.view_count or 0) + 1
    share.last_viewed_at = utcnow()
    db.commit()


def _has_share_cookie(request: Request, share: Share) -> bool:
    raw = request.cookies.get(SHARE_COOKIE_NAME)
    if not raw:
        return False
    try:
        payload = jwt.decode(raw, settings.get_secret_key(), algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return False
    try:
        share_id = int(payload.get("share_id"))
    except (TypeError, ValueError):
        return False
    return payload.get("token") == share.token and share_id == int(share.id)


def _require_unlocked(request: Request, share: Share) -> None:
    if not share.password_hash:
        return
    if not _has_share_cookie(request, share):
        raise unauthorized("该分享需要密码访问")


def _public_info(share: Share) -> PublicShareInfoOut:
    return PublicShareInfoOut(
        include_transcript=bool(share.include_transcript),
        include_insights=bool(share.include_insights),
        allow_download=bool(share.allow_download),
        has_password=bool(share.password_hash),
        expires_at=share.expires_at,
        created_at=share.created_at,
        view_count=int(share.view_count or 0),
    )


def _public_recording(db: Session, recording: Recording) -> PublicRecordingOut:
    speakers = list(
        db.scalars(
            select(Speaker).where(Speaker.recording_id == recording.id).order_by(Speaker.id)
        ).all()
    )
    tags = sorted(recording.tags, key=lambda item: item.name)
    return PublicRecordingOut(
        uuid=recording.uuid,
        title=recording.title,
        description=recording.description,
        media_type=recording.media_type,
        duration_ms=recording.duration_ms,
        recorded_at=recording.recorded_at,
        language=recording.language or "zh",
        speakers=[SpeakerOut.model_validate(speaker) for speaker in speakers],
        tags=[TagOut.model_validate(tag) for tag in tags],
    )


# ------------------------------------------------------------------ endpoints
@router.get("/shares/{token}", response_model=PublicShareOut)
def get_public_share(token: str, db: Session = Depends(get_db)):
    share = _share_or_404(db, token)
    recording = _recording_for_share(db, share)
    _touch_view(db, share)
    return PublicShareOut(
        share=_public_info(share),
        recording=_public_recording(db, recording),
    )


@router.post("/shares/{token}/unlock", response_model=ShareUnlockOut)
def unlock_share(
    token: str,
    payload: ShareUnlockIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    share = _share_or_404(db, token)
    key = _throttle_key(_client_ip(request), token)
    if not _unlock_allowed(key):
        raise ApiError(
            429,
            "密码尝试过于频繁，请稍后再试",
            "RATE_LIMITED",
            headers={"Retry-After": "60"},
        )
    if not share.password_hash:
        # No password configured: nothing to unlock.
        return ShareUnlockOut(ok=True, expires_in=SHARE_COOKIE_MAX_AGE)
    if not verify_password(payload.password, share.password_hash):
        _register_unlock_attempt(key)
        raise unauthorized("分享密码错误")

    _reset_unlock_attempts(key)
    expire = datetime.now(timezone.utc) + timedelta(seconds=SHARE_COOKIE_MAX_AGE)
    cookie_token = jwt.encode(
        {"share_id": int(share.id), "token": share.token, "exp": expire},
        settings.get_secret_key(),
        algorithm="HS256",
    )
    response.set_cookie(
        key=SHARE_COOKIE_NAME,
        value=cookie_token,
        max_age=SHARE_COOKIE_MAX_AGE,
        path=f"/api/public/shares/{share.token}",
        httponly=True,
        samesite="lax",
        secure=False,
    )
    logger.info("share %s unlocked", share.id)
    return ShareUnlockOut(ok=True, expires_in=SHARE_COOKIE_MAX_AGE)


@router.get("/shares/{token}/media")
def get_public_media(
    token: str,
    request: Request,
    download: bool = Query(False),
    db: Session = Depends(get_db),
):
    share = _share_or_404(db, token)
    _require_unlocked(request, share)
    recording = _recording_for_share(db, share)
    if download and not share.allow_download:
        raise forbidden("该分享不允许下载原文件")
    _touch_view(db, share)
    path, content_type, filename, attachment = pick_media(recording, download=bool(download))
    return serve_media_file(
        path,
        request,
        content_type=content_type,
        filename=filename,
        inline=not attachment,
    )


@router.get("/shares/{token}/download")
def download_public_media(token: str, request: Request, db: Session = Depends(get_db)):
    share = _share_or_404(db, token)
    _require_unlocked(request, share)
    if not share.allow_download:
        raise forbidden("该分享不允许下载原文件")
    _touch_view(db, share)
    recording = _recording_for_share(db, share)
    path, content_type, filename, _attachment = pick_media(recording, download=True)
    return serve_media_file(
        path,
        request,
        content_type=content_type,
        filename=filename,
        inline=False,
    )


@router.get("/shares/{token}/transcript", response_model=TranscriptOut)
def get_public_transcript(token: str, request: Request, db: Session = Depends(get_db)):
    share = _share_or_404(db, token)
    if not share.include_transcript:
        raise forbidden("该分享未开放转录稿")
    _require_unlocked(request, share)
    recording = _recording_for_share(db, share)
    return build_transcript(db, recording)


@router.get("/shares/{token}/insights", response_model=InsightsOut)
def get_public_insights(token: str, request: Request, db: Session = Depends(get_db)):
    share = _share_or_404(db, token)
    if not share.include_insights:
        raise forbidden("该分享未开放 AI 分析结果")
    _require_unlocked(request, share)
    recording = _recording_for_share(db, share)
    return build_insights(db, recording)
