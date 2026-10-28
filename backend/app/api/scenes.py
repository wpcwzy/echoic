"""Video slide/scene endpoints (v1.0) — mounted at ``/api/recordings``.

The pipeline extracts slide/keyframe JPEGs for video recordings (scene-cut
detection) and stores them under the recording's media prefix (local ``scenes/``
directory or the S3 ``scenes/`` prefix). This router exposes them to the detail
page timeline:

* ``GET /api/recordings/{id}/scenes`` — ordered scene list
* ``GET /api/recordings/{id}/scenes/{scene_id}/image`` — JPEG bytes

Both endpoints require a logged-in owner (or admin), reuse the standard
recording visibility check (foreign/trashed recordings are reported as 404) and
work for both local and S3/MinIO storage backends.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import not_found
from ..core.storage_backend import StorageError, get_storage, scene_key, serve_key
from ..db import get_db
from ..deps import get_current_user, get_recording_for_user
from ..models import User, VideoScene
from ..schemas import VideoSceneListOut, VideoSceneOut

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])

SCENE_IMAGE_MEDIA_TYPE = "image/jpeg"
SCENE_IMAGE_CACHE_CONTROL = "private, max-age=3600"


@router.get("/{recording_id}/scenes", response_model=VideoSceneListOut)
def list_recording_scenes(
    recording_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> VideoSceneListOut:
    """Return the slide/scene timeline of a video recording (sorted by position)."""
    get_recording_for_user(db, user, recording_id)
    rows = db.scalars(
        select(VideoScene)
        .where(VideoScene.recording_id == recording_id)
        .order_by(VideoScene.idx, VideoScene.time_ms)
    ).all()
    return VideoSceneListOut(items=[VideoSceneOut.model_validate(row) for row in rows])


@router.get("/{recording_id}/scenes/{scene_id}/image")
def get_scene_image(
    recording_id: int,
    scene_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Serve one scene JPEG (served through the storage backend proxy for local + S3)."""
    recording = get_recording_for_user(db, user, recording_id)
    scene = db.get(VideoScene, scene_id)
    if scene is None or int(scene.recording_id) != int(recording.id):
        raise not_found("场景图片不存在")
    filename = (scene.image_filename or "").strip()
    if not filename:
        raise not_found("场景图片不存在")
    backend = get_storage()
    try:
        key = scene_key(recording.storage_dir, filename)
    except StorageError:
        raise not_found("场景图片不存在") from None
    try:
        response = serve_key(
            backend,
            key,
            filename=filename,
            request_headers=request.headers,
            media_type=SCENE_IMAGE_MEDIA_TYPE,
            download=False,
        )
    except StorageError:
        raise not_found("场景图片不存在") from None
    # Image URLs are stable for a processed recording: allow short private caching.
    if getattr(response, "status_code", 200) in (200, 206):
        # Scene images are immutable for a processed recording; allow short
        # private caching instead of the media endpoint's no-cache default.
        response.headers["Cache-Control"] = SCENE_IMAGE_CACHE_CONTROL
    return response


__all__ = ["router"]
