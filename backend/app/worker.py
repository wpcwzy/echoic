"""Background worker (stub — backend-ai implements)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_state: dict = {"running": False, "last_error": None, "processed": 0, "current": None}


def start_embedded_worker() -> None:
    logger.info("embedded worker stub started")


def stop_embedded_worker() -> None:
    logger.info("embedded worker stub stopped")


def worker_status() -> dict:
    return dict(_state)
