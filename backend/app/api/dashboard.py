"""Dashboard aggregation endpoint (docs/API.md §5, scoped by recording owner)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..deps import get_current_user, is_admin
from ..models import Recording, User
from ..schemas import DashboardOut, StorageInfo
from ._common import (
    ACTIVE_STATUSES,
    library_stats,
    recordings_to_summaries,
    tags_with_counts,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])

_LOAD_OPTIONS = (
    selectinload(Recording.tags),
    selectinload(Recording.speakers),
    selectinload(Recording.analyses),
)


def _scoped(stmt, user: User):
    stmt = stmt.where(Recording.deleted_at.is_(None))
    if is_admin(user):
        return stmt
    return stmt.where(Recording.owner_id == user.id)


@router.get("", response_model=DashboardOut)
def get_dashboard(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stats = library_stats(db, user)
    recent = list(
        db.scalars(
            _scoped(select(Recording), user)
            .options(*_LOAD_OPTIONS)
            .order_by(Recording.recorded_at.desc(), Recording.id.desc())
            .limit(6)
        ).all()
    )
    processing = list(
        db.scalars(
            _scoped(select(Recording), user)
            .options(*_LOAD_OPTIONS)
            .where(Recording.status.in_(ACTIVE_STATUSES))
            .order_by(Recording.created_at.desc(), Recording.id.desc())
            .limit(10)
        ).all()
    )
    media_bytes = int(
        db.scalar(
            _scoped(select(func.coalesce(func.sum(Recording.size_bytes), 0)), user)
        )
        or 0
    )
    recording_count = int(
        db.scalar(_scoped(select(func.count()).select_from(Recording), user)) or 0
    )
    return DashboardOut(
        stats=stats,
        recent=recordings_to_summaries(db, recent),
        processing=recordings_to_summaries(db, processing),
        top_tags=tags_with_counts(db, limit=10, order="count", user=user),
        storage=StorageInfo(media_bytes=media_bytes, recordings=recording_count),
    )
