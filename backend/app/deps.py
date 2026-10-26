from __future__ import annotations

import logging

from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .core.errors import forbidden, not_found, unauthorized
from .core.security import COOKIE_NAME, decode_access_token, hash_password
from .db import get_db
from .models import Recording, User

logger = logging.getLogger(__name__)


def ensure_admin_user(db: Session) -> User:
    """Create the bootstrap admin account when the users table is empty."""
    count = db.scalar(select(func.count()).select_from(User)) or 0
    if count:
        admin = db.scalar(select(User).where(User.username == settings.admin_username))
        return admin  # type: ignore[return-value]
    user = User(
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        display_name=settings.admin_display_name,
        is_admin=True,
        role="admin",
        disabled=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.warning(
        "created initial admin user %r with the configured password; change it in Settings after first login",
        settings.admin_username,
    )
    return user


def _read_token(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        return token
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = _read_token(request)
    user_id = decode_access_token(token) if token else None
    if not user_id:
        raise unauthorized()
    user = db.get(User, user_id)
    if user is None:
        raise unauthorized("登录状态已失效，请重新登录")
    if user.disabled:
        raise unauthorized("账号已被停用，请联系管理员")
    return user


def is_admin(user: User) -> bool:
    return bool(user.is_admin) or getattr(user, "role", "") == "admin"


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_admin(user):
        raise forbidden()
    return user


def can_access_recording(user: User, recording: Recording) -> bool:
    """Admin sees all; members only their own recordings (v0.2)."""
    if is_admin(user):
        return True
    return recording.owner_id is not None and recording.owner_id == user.id


def get_recording_for_user(
    db: Session, user: User, recording_id: int, *, include_deleted: bool = False
) -> Recording:
    recording = db.get(Recording, recording_id)
    if recording is None or not can_access_recording(user, recording):
        raise not_found("录音不存在")
    if not include_deleted and getattr(recording, "deleted_at", None) is not None:
        raise not_found("录音不存在")
    return recording


def get_recording_any(db: Session, user: User, recording_id: int) -> Recording:
    """Permission-checked lookup that also returns trashed recordings (for trash APIs)."""
    recording = db.get(Recording, recording_id)
    if recording is None or not can_access_recording(user, recording):
        raise not_found("录音不存在")
    return recording


def recording_scope(query, user: User, *, include_deleted: bool = False):
    """Apply visibility scope (and exclude trashed recordings by default)."""
    if not is_admin(user):
        query = query.where(Recording.owner_id == user.id)
    if not include_deleted:
        query = query.where(Recording.deleted_at.is_(None))
    return query
