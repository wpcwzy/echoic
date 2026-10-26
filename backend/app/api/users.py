"""User management endpoints (admin only, docs/08-P1 §4.1)."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..core.errors import bad_request, conflict, not_found
from ..core.security import hash_password
from ..db import get_db
from ..deps import require_admin
from ..models import Recording, Share, User
from ..schemas import (
    OkOut,
    ResetPasswordIn,
    UserAdminOut,
    UserCreateIn,
    UserListOut,
    UserUpdateIn,
)
from ._common import like_pattern

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])


def _counts(db: Session, user_ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
    if not user_ids:
        return {}, {}
    recording_counts = dict(
        db.execute(
            select(Recording.owner_id, func.count(Recording.id))
            .where(Recording.owner_id.in_(user_ids))
            .group_by(Recording.owner_id)
        ).all()
    )
    share_counts = dict(
        db.execute(
            select(Share.created_by, func.count(Share.id))
            .where(Share.created_by.in_(user_ids))
            .group_by(Share.created_by)
        ).all()
    )
    return (
        {int(key): int(value) for key, value in recording_counts.items() if key is not None},
        {int(key): int(value) for key, value in share_counts.items() if key is not None},
    )


def _to_admin_out(
    user: User,
    recording_counts: dict[int, int] | None = None,
    share_counts: dict[int, int] | None = None,
) -> UserAdminOut:
    data = UserAdminOut.model_validate(user)
    data.recording_count = int((recording_counts or {}).get(user.id, 0))
    data.share_count = int((share_counts or {}).get(user.id, 0))
    return data


def _active_admin_count(db: Session, exclude_id: int | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.disabled.is_(False))
    )
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return int(db.scalar(stmt) or 0)


@router.get("", response_model=UserListOut)
def list_users(
    query: str | None = Query(None, description="用户名/昵称/邮箱模糊搜索"),
    role: Literal["admin", "member"] | None = Query(None),
    include_disabled: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(User)
    if query and query.strip():
        pattern = like_pattern(query.strip())
        stmt = stmt.where(
            or_(
                User.username.ilike(pattern, escape="\\"),
                User.display_name.ilike(pattern, escape="\\"),
                User.email.ilike(pattern, escape="\\"),
            )
        )
    if role is not None:
        stmt = stmt.where(User.role == role)
    if not include_disabled:
        stmt = stmt.where(User.disabled.is_(False))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    users = list(
        db.scalars(
            stmt.order_by(User.id.asc()).offset((page - 1) * page_size).limit(page_size)
        ).all()
    )
    recording_counts, share_counts = _counts(db, [user.id for user in users])
    return UserListOut(
        items=[_to_admin_out(user, recording_counts, share_counts) for user in users],
        total=total,
    )


@router.post("", response_model=UserAdminOut, status_code=201)
def create_user(payload: UserCreateIn, db: Session = Depends(get_db)):
    username = payload.username.strip()
    if db.scalar(select(User.id).where(User.username == username)) is not None:
        raise conflict(f"用户名已存在：{username}")
    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        display_name=(payload.display_name or username).strip()[:120],
        email=(payload.email or "").strip() or None,
        role=payload.role,
        is_admin=payload.role == "admin",
        disabled=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("user %s created (role=%s)", user.username, user.role)
    return _to_admin_out(user)


@router.patch("/{user_id}", response_model=UserAdminOut)
def update_user(
    user_id: int,
    payload: UserUpdateIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        raise not_found("用户不存在")
    fields = payload.model_fields_set

    new_role = payload.role if "role" in fields and payload.role is not None else user.role
    new_disabled = (
        payload.disabled
        if "disabled" in fields and payload.disabled is not None
        else user.disabled
    )
    if user.role == "admin" and (new_role != "admin" or new_disabled):
        if _active_admin_count(db, exclude_id=user.id) == 0:
            raise bad_request("不能撤销或停用最后一个管理员")
    if user.id == admin.id and new_disabled:
        raise bad_request("不能停用自己的账号")

    if "display_name" in fields and payload.display_name is not None:
        user.display_name = payload.display_name.strip()[:120]
    if "email" in fields and payload.email is not None:
        user.email = payload.email.strip() or None
    if "role" in fields and payload.role is not None:
        user.role = payload.role
        user.is_admin = payload.role == "admin"
    if "disabled" in fields and payload.disabled is not None:
        user.disabled = payload.disabled
    db.commit()
    db.refresh(user)
    recording_counts, share_counts = _counts(db, [user.id])
    logger.info("user %s updated (role=%s disabled=%s)", user.username, user.role, user.disabled)
    return _to_admin_out(user, recording_counts, share_counts)


@router.post("/{user_id}/reset-password", response_model=OkOut)
def reset_password(
    user_id: int,
    payload: ResetPasswordIn,
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise not_found("用户不存在")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    logger.info("password reset for user %s", user.username)
    return OkOut()


@router.delete("/{user_id}", response_model=OkOut)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        raise not_found("用户不存在")
    if user.id == admin.id:
        raise bad_request("不能删除当前登录的账号")
    if user.role == "admin" and _active_admin_count(db, exclude_id=user.id) == 0:
        raise bad_request("系统中至少需要保留一个可用的管理员")
    owned = int(
        db.scalar(
            select(func.count()).select_from(Recording).where(Recording.owner_id == user.id)
        )
        or 0
    )
    if owned:
        raise conflict("请先转移或删除该用户的录音")
    # Shares created by this user stay valid but lose their creator reference.
    db.execute(update(Share).where(Share.created_by == user.id).values(created_by=None))
    username = user.username
    db.delete(user)
    db.commit()
    logger.info("user %s deleted by %s", username, admin.username)
    return OkOut()
