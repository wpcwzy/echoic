from __future__ import annotations

import logging

from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .core.errors import forbidden, unauthorized
from .core.security import COOKIE_NAME, decode_access_token, hash_password
from .db import get_db
from .models import User

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
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise forbidden()
    return user
