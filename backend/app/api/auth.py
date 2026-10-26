"""Authentication endpoints (docs/API.md §2 + docs/08-P1 §2.3)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..core.errors import ApiError, bad_request, conflict, forbidden, unauthorized
from ..core.security import (
    COOKIE_NAME,
    check_login_rate,
    create_access_token,
    hash_password,
    register_login_attempt,
    reset_login_attempts,
    verify_password,
)
from ..core.utils import utcnow
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import LoginIn, LoginOut, OkOut, RegisterIn, UserOut
from ..services.settings_store import load_ai_config

logger = logging.getLogger(__name__)

router = APIRouter()


class MeOut(BaseModel):
    user: UserOut


class ChangePasswordIn(BaseModel):
    """Contract wants ``400 VALIDATION_ERROR`` for a too-short password, so the
    length rule lives in the handler (pydantic would answer 422 instead)."""

    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or "unknown"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.session_days * 24 * 3600,
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,
    )


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = _client_ip(request)
    if not check_login_rate(ip):
        raise ApiError(
            429,
            "登录尝试过于频繁，请稍后再试",
            "RATE_LIMITED",
            headers={"Retry-After": "60"},
        )

    user = db.scalar(select(User).where(User.username == payload.username.strip()))
    if user is None or not verify_password(payload.password, user.password_hash):
        register_login_attempt(ip)
        raise unauthorized("用户名或密码错误")
    if user.disabled:
        raise forbidden("账号已被停用，请联系管理员")

    reset_login_attempts(ip)
    user.last_login_at = utcnow()
    db.add(user)
    db.commit()
    token = create_access_token(user.id)
    _set_session_cookie(response, token)
    logger.info("user %s logged in", user.username)
    return LoginOut(user=UserOut.model_validate(user), token=token)


@router.post("/register", response_model=UserOut, status_code=201)
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    config = load_ai_config(db)
    if not bool(config.system.get("allow_registration", False)):
        raise forbidden("注册暂未开放")

    username = payload.username.strip()
    existing = db.scalar(select(User.id).where(User.username == username))
    if existing is not None:
        raise conflict(f"用户名已被占用：{username}")
    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        display_name=(payload.display_name or username).strip()[:120],
        email=(payload.email or "").strip() or None,
        role="member",
        is_admin=False,
        disabled=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("registered member %s", user.username)
    return UserOut.model_validate(user)


@router.post("/logout", response_model=OkOut)
def logout(response: Response, user: User = Depends(get_current_user)):
    response.delete_cookie(COOKIE_NAME, path="/")
    logger.info("user %s logged out", user.username)
    return OkOut()


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)):
    return MeOut(user=UserOut.model_validate(user))


@router.post("/change-password", response_model=OkOut)
def change_password(
    payload: ChangePasswordIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if len(payload.new_password) < 6:
        raise bad_request("新密码至少 6 位")
    if not verify_password(payload.old_password, user.password_hash):
        raise bad_request("旧密码不正确")
    if payload.old_password == payload.new_password:
        raise bad_request("新密码不能与旧密码相同")
    user.password_hash = hash_password(payload.new_password)
    db.add(user)
    db.commit()
    logger.info("user %s changed password", user.username)
    return OkOut()
