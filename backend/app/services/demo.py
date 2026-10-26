"""Demo data seeding (stub — backend-ai implements; Lead provides demo_data.json)."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def seed_demo(db: Session) -> int:
    """Idempotent: create demo recordings, tags, speakers, segments, analyses, chunks."""
    return 0


def ensure_mock_demo(db: Session) -> None:
    """Auto seed when demo mode is active and the library is empty."""
    return None
