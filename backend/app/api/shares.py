"""Share link management endpoints (docs/08-P1 §4.2).

Recording-scoped create/list live in ``app/api/recordings.py`` (they share the
``/api/recordings`` prefix); this router owns the cross-recording list and the
revoke endpoint under ``/api/shares``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import not_found
from ..core.utils import utcnow
from ..db import get_db
from ..deps import can_access_recording, get_current_user, is_admin
from ..models import Recording, Share, User
from ..schemas import OkOut, ShareListOut, ShareOut
from ._common import share_to_out

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


def _shares_to_out(db: Session, shares) -> list[ShareOut]:
    creator_ids = {share.created_by for share in shares if share.created_by}
    creators = (
        {user.id: user for user in db.scalars(select(User).where(User.id.in_(creator_ids))).all()}
        if creator_ids
        else {}
    )
    return [share_to_out(share, creators.get(share.created_by)) for share in shares]


@router.get("", response_model=ShareListOut)
def list_shares(
    recording_id: int | None = Query(None),
    include_revoked: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(Share).join(Recording, Recording.id == Share.recording_id)
    if not is_admin(user):
        stmt = stmt.where(Recording.owner_id == user.id)
    if recording_id is not None:
        stmt = stmt.where(Share.recording_id == recording_id)
    if not include_revoked:
        stmt = stmt.where(Share.revoked_at.is_(None))
    shares = list(db.scalars(stmt.order_by(Share.id.desc())).all())
    items = _shares_to_out(db, shares)
    return ShareListOut(items=items, total=len(items))


@router.delete("/{share_id}", response_model=OkOut)
def revoke_share(
    share_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    share = db.get(Share, share_id)
    recording = db.get(Recording, share.recording_id) if share is not None else None
    if share is None or recording is None or not can_access_recording(user, recording):
        raise not_found("分享不存在")
    if share.revoked_at is None:
        share.revoked_at = utcnow()
        db.commit()
        logger.info("share %s revoked by %s", share.id, user.username)
    return OkOut()
