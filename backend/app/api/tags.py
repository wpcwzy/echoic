"""Tag endpoints (docs/API.md §5).

Every logged-in user can browse the shared tag catalogue and create custom tags;
tag rename/delete remains admin-only so a member cannot alter global labels.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import bad_request, conflict, not_found
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import Tag, User
from ..schemas import OkOut, TagCreateIn, TagOut, TagUpdateIn, TagWithCountOut
from ._common import tags_with_counts

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


class TagListOut(BaseModel):
    items: list[TagWithCountOut]


def _find_by_name(db: Session, name: str, exclude_id: int | None = None) -> Tag | None:
    stmt = select(Tag).where(Tag.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Tag.id != exclude_id)
    return db.scalar(stmt)


@router.get("", response_model=TagListOut)
def list_tags(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return TagListOut(items=tags_with_counts(db, user=user))


@router.post("", response_model=TagOut, status_code=201)
def create_tag(
    payload: TagCreateIn,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    name = payload.name.strip()
    if not name:
        raise bad_request("标签名称不能为空")
    if _find_by_name(db, name) is not None:
        raise conflict(f"标签已存在：{name}")
    tag = Tag(name=name[:64], color=(payload.color or "#5B8FF9").strip()[:16] or "#5B8FF9")
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return TagOut.model_validate(tag)


@router.patch("/{tag_id}", response_model=TagOut)
def update_tag(
    tag_id: int,
    payload: TagUpdateIn,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise not_found("标签不存在")
    fields = payload.model_fields_set
    if "name" in fields and payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise bad_request("标签名称不能为空")
        duplicate = _find_by_name(db, name, exclude_id=tag.id)
        if duplicate is not None:
            raise conflict(f"标签已存在：{name}")
        tag.name = name[:64]
    if "color" in fields and payload.color is not None:
        tag.color = payload.color.strip()[:16] or tag.color
    db.commit()
    db.refresh(tag)
    return TagOut.model_validate(tag)


@router.delete("/{tag_id}", response_model=OkOut)
def delete_tag(
    tag_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise not_found("标签不存在")
    tag.recordings = []
    db.delete(tag)
    db.commit()
    logger.info("tag %s deleted", tag_id)
    return OkOut()
