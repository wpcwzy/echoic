"""Database backed job queue (stub — backend-ai implements).

Contract (keep signatures stable):
    enqueue(db, recording_id, from_="all") -> None
    request_cancel(db, recording_id) -> bool
    queue_depth(db) -> int
    reset_stale_tasks() -> None
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def enqueue(db: Session, recording_id: int, from_: str = "all") -> None:
    raise NotImplementedError("queue.enqueue 由 backend-ai 实现")


def request_cancel(db: Session, recording_id: int) -> bool:
    return False


def queue_depth(db: Session) -> int:
    return 0


def reset_stale_tasks() -> None:
    logger.debug("queue.reset_stale_tasks stub")
