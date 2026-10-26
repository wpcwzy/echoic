"""Trash / soft-delete service (R-23, v0.3.0).

Recordings are never deleted immediately: ``DELETE /api/recordings/{id}`` moves
them to the trash (``deleted_at``/``deleted_by``), revokes their public share
links and hides them from every normal query (see ``app.deps.recording_scope``).
A recording can be restored until the retention window expires; afterwards the
worker purges it permanently (DB rows + media files).

This module is owned by backend-api (docs/10 §0.3). The storage layer may later
back media deletion with an object store; ``delete_recording_media`` prefers
``app.core.storage_backend`` when it is available and falls back to the local
filesystem helper.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..core.storage import delete_recording_dir
from ..core.utils import to_iso, utcnow
from ..db import SessionLocal
from ..models import ImportedFile, Recording, Share

logger = logging.getLogger(__name__)

#: minimum interval between automatic retention purges (worker hook)
TRASH_PURGE_INTERVAL_SECONDS = 3600

_last_purge_at: datetime | None = None
_last_purge_result: dict | None = None


# ------------------------------------------------------------------ config helpers
def retention_days() -> int:
    try:
        value = int(settings.trash_retention_days)
    except (TypeError, ValueError):
        value = 30
    return max(1, value)


def restore_before(recording: Recording) -> datetime | None:
    """The moment after which a trashed recording may be purged automatically."""
    if recording.deleted_at is None:
        return None
    return recording.deleted_at + timedelta(days=retention_days())


# ------------------------------------------------------------------ media deletion
def delete_recording_media(recording: Recording) -> None:
    """Delete all stored files for a recording (local now, object store when ready)."""
    storage_dir = recording.storage_dir or f"media/{recording.uuid}"
    try:  # concurrent backend-storage module; optional integration
        from ..core.storage_backend import get_storage  # type: ignore[attr-defined]

        backend = get_storage()
        if getattr(backend, "name", "local") != "local":
            backend.delete_prefix(storage_dir)
            logger.info("storage backend purged prefix %s for recording %s", storage_dir, recording.id)
            return
    except Exception:  # noqa: BLE001 - storage backend is optional / may be mid-development
        logger.debug("storage backend unavailable, deleting local media for %s", recording.id, exc_info=True)
    delete_recording_dir(storage_dir)


# ------------------------------------------------------------------ state changes
def revoke_shares(db: Session, recording_id: int, *, when: datetime | None = None) -> int:
    """Soft-revoke every active share of a recording. Returns number of shares revoked."""
    when = when or utcnow()
    result = db.execute(
        update(Share)
        .where(Share.recording_id == recording_id, Share.revoked_at.is_(None))
        .values(revoked_at=when)
    )
    return int(result.rowcount or 0)


def soft_delete(
    db: Session,
    recording: Recording,
    *,
    user_id: int | None = None,
    when: datetime | None = None,
) -> Recording:
    when = when or utcnow()
    recording.deleted_at = when
    recording.deleted_by = user_id
    revoked = revoke_shares(db, recording.id, when=when)
    # Trashed recordings must no longer count as consume-import duplicates
    # (docs/10 §1.2): drop the sha256 bookkeeping row so the same source file can
    # be imported again while this recording rests in the trash.
    db.execute(delete(ImportedFile).where(ImportedFile.recording_id == recording.id))
    db.commit()
    db.refresh(recording)
    logger.info(
        "recording %s moved to trash by user %s (revoked shares: %s)",
        recording.id,
        user_id,
        revoked,
    )
    return recording


def restore(db: Session, recording: Recording) -> Recording:
    recording.deleted_at = None
    recording.deleted_by = None
    db.commit()
    db.refresh(recording)
    logger.info("recording %s restored from trash", recording.id)
    return recording


def purge_recording(db: Session, recording: Recording) -> None:
    """Permanently delete a trashed recording (media, bookkeeping, DB rows)."""
    recording_id = int(recording.id)
    delete_recording_media(recording)
    # Allow re-importing the same source file through the consume directory again.
    db.execute(delete(ImportedFile).where(ImportedFile.recording_id == recording_id))
    db.delete(recording)
    db.commit()
    logger.info("recording %s permanently purged", recording_id)


# ------------------------------------------------------------------ retention cleanup
def purge_expired(
    *,
    now: datetime | None = None,
    retention: int | None = None,
    db: Session | None = None,
) -> dict:
    """Purge recordings whose trash retention window has expired.

    Never raises for a single bad recording; returns a summary dict.
    """
    reference = now or utcnow()
    days = retention if retention is not None else retention_days()
    cutoff = reference - timedelta(days=days)
    own_session = db is None
    session = db if db is not None else SessionLocal()
    purged_ids: list[int] = []
    errors = 0
    try:
        rows = list(
            session.scalars(
                select(Recording).where(
                    Recording.deleted_at.is_not(None), Recording.deleted_at <= cutoff
                )
            ).all()
        )
        for recording in rows:
            recording_id = int(recording.id)
            try:
                purge_recording(session, recording)
                purged_ids.append(recording_id)
            except Exception:  # noqa: BLE001 - one broken recording must not stop the purge
                errors += 1
                logger.exception("failed to purge trashed recording %s", recording_id)
                try:
                    session.rollback()
                except Exception:  # pragma: no cover
                    pass
    finally:
        if own_session:
            session.close()
    result = {
        "purged": len(purged_ids),
        "ids": purged_ids,
        "errors": errors,
        "retention_days": days,
        "at": to_iso(reference),
    }
    if purged_ids or errors:
        logger.info("trash retention purge: %s", result)
    return result


def maybe_purge_expired(*, force: bool = False) -> dict | None:
    """Worker hook: run the retention purge at most once per hour."""
    global _last_purge_at, _last_purge_result
    now = utcnow()
    if (
        not force
        and _last_purge_at is not None
        and (now - _last_purge_at).total_seconds() < TRASH_PURGE_INTERVAL_SECONDS
    ):
        return None
    try:
        result = purge_expired(now=now)
    except Exception:  # noqa: BLE001 - worker must keep running
        logger.exception("trash retention purge failed")
        _last_purge_at = now
        _last_purge_result = {
            "purged": 0,
            "errors": 1,
            "retention_days": retention_days(),
            "at": to_iso(now),
        }
        return dict(_last_purge_result)
    _last_purge_at = now
    _last_purge_result = result
    return result


def last_purge_info() -> dict | None:
    return dict(_last_purge_result) if isinstance(_last_purge_result, dict) else None


def reset_schedule_state() -> None:
    """Test helper: forget the last automatic purge time."""
    global _last_purge_at, _last_purge_result
    _last_purge_at = None
    _last_purge_result = None


__all__ = [
    "TRASH_PURGE_INTERVAL_SECONDS",
    "delete_recording_media",
    "last_purge_info",
    "maybe_purge_expired",
    "purge_expired",
    "purge_recording",
    "reset_schedule_state",
    "restore",
    "restore_before",
    "retention_days",
    "revoke_shares",
    "soft_delete",
]
