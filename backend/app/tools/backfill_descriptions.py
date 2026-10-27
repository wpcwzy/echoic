"""Backfill one-line descriptions for recordings that have none (v0.3.10).

The upload/processing pipeline auto-fills ``recordings.description`` from the AI
summary (or first transcript segments) after analysis. This CLI applies the same
derivation to existing recordings that were processed before that feature.

Usage::

    # dry run (default): show what would be filled
    python -m app.tools.backfill_descriptions --limit 20

    # actually write the derived descriptions
    python -m app.tools.backfill_descriptions --apply

    # a single recording
    python -m app.tools.backfill_descriptions --recording-id 42 --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from sqlalchemy import func, or_, select

from ..db import SessionLocal, init_db
from ..models import Recording
from ..services.analysis import apply_auto_description, derive_auto_description

logger = logging.getLogger(__name__)

MAX_PREVIEWS = 10


def _missing_description_clause():
    return or_(
        Recording.description.is_(None),
        func.trim(Recording.description) == "",
    )


def backfill(
    *,
    apply: bool = False,
    limit: int | None = None,
    recording_id: int | None = None,
) -> dict:
    """Derive/fill descriptions. Returns a JSON-serializable summary dict."""
    init_db()
    with SessionLocal() as db:
        query = select(Recording.id).where(_missing_description_clause()).order_by(Recording.id)
        if recording_id is not None:
            query = query.where(Recording.id == int(recording_id))
        if limit is not None and limit > 0:
            query = query.limit(limit)
        recording_ids = [int(value) for value in db.scalars(query).all()]

    derivable: list[dict] = []
    filled = 0
    failed = 0
    for current_id in recording_ids:
        with SessionLocal() as db:
            description = derive_auto_description(current_id, db)
        if not description:
            continue
        derivable.append({"recording_id": current_id, "description": description})
        if not apply:
            continue
        with SessionLocal() as db:
            if apply_auto_description(current_id, db):
                filled += 1
            else:  # pragma: no cover - race/empty text
                failed += 1
    return {
        "apply": bool(apply),
        "dry_run": not apply,
        "scanned": len(recording_ids),
        "derivable": len(derivable),
        "filled": filled,
        "failed": failed,
        "previews": derivable[:MAX_PREVIEWS],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="为缺少描述的录音回填一句话描述（来自 AI 纪要，回退到首段转录）",
    )
    parser.add_argument("--apply", action="store_true", help="真正写入数据库（默认仅预演）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少条录音")
    parser.add_argument("--recording-id", type=int, default=None, help="只处理指定录音 ID")
    parser.add_argument("--log-level", default="INFO", help="日志级别（默认 INFO）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    result = backfill(apply=args.apply, limit=args.limit, recording_id=args.recording_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
