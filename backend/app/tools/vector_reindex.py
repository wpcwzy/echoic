"""Vector index rebuild CLI (v0.3.0 R-20 Phase 1).

Usage (run from the ``backend`` directory, no server required)::

    python -m app.tools.vector_reindex --all
    python -m app.tools.vector_reindex --all --dry-run
    python -m app.tools.vector_reindex --recording-id 12 --force
    python -m app.tools.vector_reindex --all --force

It reuses :func:`app.services.vector_store.reindex_recordings` (same code path
as ``POST /api/vector/reindex``), processes recordings in batches of ≤200 so
large libraries are supported, prints a Chinese summary and exits non-zero when
the embedding provider is missing or any recording failed.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from sqlalchemy import select

from ..db import SessionLocal, init_db
from ..migrations import run_migrations
from ..models import Recording
from ..providers.registry import build_embedding_provider
from ..services.settings_store import ensure_default_settings, load_ai_config
from ..services.vector_store import (
    MAX_REINDEX_RECORDINGS,
    ReindexResult,
    get_vector_store,
    reindex_recordings,
)

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.vector_reindex",
        description="重建 Echoic 检索索引（R-20）：为缺失/过期的 chunks 生成 embedding",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="处理全部未删除录音（分批，每批 ≤200）")
    target.add_argument("--recording-id", type=int, metavar="ID", help="只处理指定录音")
    parser.add_argument("--dry-run", action="store_true", help="只统计待处理量，不调用模型、不写库")
    parser.add_argument("--force", action="store_true", help="强制重算已有向量（包括 model/dim 未变的）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


def _merge(target: ReindexResult, part: ReindexResult) -> None:
    target.processed_recordings += part.processed_recordings
    target.chunks += part.chunks
    target.skipped += part.skipped
    target.errors.extend(part.errors)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    init_db()
    run_migrations()
    with SessionLocal() as db:
        ensure_default_settings(db)
        cfg = load_ai_config(db)
        provider_name = (cfg.embeddings.provider or "none").strip()
        if provider_name in ("none", ""):
            print(
                "[vector-reindex] 未配置 Embedding 模型，无法重建索引；"
                "请先在设置 → AI 服务中启用（演示环境可选 mock）。",
                file=sys.stderr,
            )
            return 2
        try:
            provider = build_embedding_provider(cfg.embeddings)
        except Exception as exc:  # noqa: BLE001 - friendly CLI error
            message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            print(f"[vector-reindex] Embedding 配置不可用：{message}", file=sys.stderr)
            return 2
        if provider is None:
            print("[vector-reindex] 未配置 Embedding 模型，无法重建索引。", file=sys.stderr)
            return 2

        if args.recording_id is not None:
            batches = [[int(args.recording_id)]]
        else:
            all_ids = [
                int(value)
                for value in db.scalars(
                    select(Recording.id)
                    .where(Recording.deleted_at.is_(None))
                    .order_by(Recording.id.asc())
                ).all()
            ]
            batches = [
                all_ids[offset : offset + MAX_REINDEX_RECORDINGS]
                for offset in range(0, len(all_ids), MAX_REINDEX_RECORDINGS)
            ]
            if not batches:
                print("[vector-reindex] 没有可处理的录音。")
                return 0

        total = ReindexResult()
        for index, batch in enumerate(batches, start=1):
            logger.info("batch %s/%s: %s recordings", index, len(batches), len(batch))
            part = reindex_recordings(
                db,
                provider=provider,
                recording_ids=batch,
                force=bool(args.force),
                dry_run=bool(args.dry_run),
            )
            _merge(total, part)

    mode_label = "dry-run（未写入）" if args.dry_run else "完成"
    model_name = str(getattr(provider, "model", "") or cfg.embeddings.model or "")
    dim = getattr(provider, "dim", None) or cfg.embeddings.dimensions
    store = get_vector_store()
    backend = str(getattr(store, "backend", "numpy"))
    backend_note = ""
    fallback_reason = getattr(store, "fallback_reason", None)
    if fallback_reason:
        backend_note = f"（{fallback_reason}）"
    lines = [
        f"[vector-reindex] {mode_label}：向量后端 {backend}{backend_note}，"
        f"embedding 模型 {model_name or '未知'}（维度 {dim}）",
        f"[vector-reindex] 处理录音 {total.processed_recordings} 条，"
        f"{'待写入' if args.dry_run else '写入'}切片 {total.chunks} 个，跳过 {total.skipped} 个",
    ]
    if total.errors:
        lines.append(f"[vector-reindex] 错误 {len(total.errors)} 条：")
        lines.extend(f"  - {message}" for message in total.errors[:20])
        if len(total.errors) > 20:
            lines.append(f"  …… 其余 {len(total.errors) - 20} 条省略")
    print("\n".join(lines))
    return 0 if not total.errors else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
