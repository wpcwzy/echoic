"""Vector / hybrid search API (v0.3.0 R-20 Phase 1).

Endpoints (mounted at ``/api/vector`` in ``app/main.py``):

* ``GET  /status``  — admin: backend health + index counters + stale detection.
* ``POST /reindex`` — admin: synchronous embedding backfill for ≤200 recordings
  (``dry_run`` supported; the CLI loops batches for large libraries).
* ``GET  /search``  — any logged-in user: keyword / semantic / hybrid search with
  ACL (admin: all; member: own recordings, trashed excluded) and graceful
  semantic → hybrid → keyword degradation.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..core.errors import ProviderError, bad_request
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import User
from ..providers.registry import build_embedding_provider
from ..schemas import (
    VectorReindexIn,
    VectorReindexOut,
    VectorSearchHit,
    VectorSearchOut,
    VectorStatusOut,
)
from ..services.retrieval import retrieve_for_query
from ..services.settings_store import load_ai_config
from ..services.vector_store import (
    MAX_REINDEX_RECORDINGS,
    VectorStoreUnavailable,
    chunk_index_counts,
    get_vector_store,
    reindex_recordings,
)

logger = logging.getLogger(__name__)

router = APIRouter()

__all__ = ["router"]


def _provider_summary(cfg) -> tuple[bool, object | None, str | None, int | None, str | None]:
    """Return ``(configured, provider, model, dim, error)`` for the current config."""
    name = (cfg.embeddings.provider or "none").strip()
    configured = name not in ("none", "")
    provider = None
    error: str | None = None
    if configured:
        try:
            provider = build_embedding_provider(cfg.embeddings)
        except ProviderError as exc:
            error = exc.message
        except Exception as exc:  # noqa: BLE001 - defensive, user editable config
            logger.warning("build embedding provider failed: %s", exc)
            error = f"Embedding 配置不可用：{type(exc).__name__}"
    model = None
    dim = None
    if provider is not None:
        model = str(getattr(provider, "model", "") or cfg.embeddings.model or "").strip()[:120] or None
        dim = getattr(provider, "dim", None)
    else:
        model = str(cfg.embeddings.model or "").strip()[:120] or None
    if dim is None and configured:
        try:
            dim = int(cfg.embeddings.dimensions or 0) or None
        except (TypeError, ValueError):
            dim = None
    return configured, provider, model, dim, error


@router.get("/status", response_model=VectorStatusOut)
def vector_status(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> VectorStatusOut:
    """Admin view of the retrieval index (docs/10 §5.2)."""
    cfg = load_ai_config(db)
    configured, provider, model, dim, provider_error = _provider_summary(cfg)
    store = get_vector_store()
    healthy, health_reason = store.health()
    counts = chunk_index_counts(db, model=model, dim=dim)
    fallback_reasons: list[str] = []
    fallback = getattr(store, "fallback_reason", None)
    if fallback:
        fallback_reasons.append(str(fallback))
    if not healthy and health_reason:
        fallback_reasons.append(health_reason)
    if provider_error:
        fallback_reasons.append(f"Embedding 配置不可用：{provider_error}")
    elif not configured:
        fallback_reasons.append("未配置 Embedding 模型，语义检索不可用")
    available = bool(healthy and provider is not None and configured)
    return VectorStatusOut(
        backend=getattr(store, "backend", "numpy"),
        available=available,
        embeddings_configured=bool(configured),
        embedding_model=model,
        dim=dim,
        chunks_total=counts["total"],
        chunks_indexed=counts["indexed"],
        stale_chunks=counts["stale"],
        fallback_reason="；".join(fallback_reasons) if fallback_reasons else None,
    )


@router.post("/reindex", response_model=VectorReindexOut)
def vector_reindex(
    payload: VectorReindexIn,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> VectorReindexOut:
    """Synchronously (re)build chunk embeddings for the target recordings."""
    cfg = load_ai_config(db)
    provider_name = (cfg.embeddings.provider or "none").strip()
    if provider_name in ("none", ""):
        raise bad_request(
            "未配置 Embedding 模型，无法重建向量索引（请先在设置 → AI 服务中启用）",
            "EMBEDDINGS_NOT_CONFIGURED",
        )
    try:
        provider = build_embedding_provider(cfg.embeddings)
    except ProviderError as exc:
        raise bad_request(exc.message, "EMBEDDINGS_NOT_CONFIGURED") from exc
    if provider is None:
        raise bad_request(
            "未配置 Embedding 模型，无法重建向量索引（请先在设置 → AI 服务中启用）",
            "EMBEDDINGS_NOT_CONFIGURED",
        )
    recording_ids = payload.recording_ids
    if recording_ids is not None:
        if len(recording_ids) > MAX_REINDEX_RECORDINGS:
            raise bad_request(f"单次最多处理 {MAX_REINDEX_RECORDINGS} 条录音，请拆分后重试")
        if any(int(value) <= 0 for value in recording_ids):
            raise bad_request("recording_ids 必须是正整数")
    try:
        result = reindex_recordings(
            db,
            provider=provider,
            recording_ids=recording_ids,
            force=bool(payload.force),
            dry_run=bool(payload.dry_run),
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    except VectorStoreUnavailable as exc:
        raise bad_request(f"向量后端不可用：{exc.reason}", "VECTOR_BACKEND_UNAVAILABLE") from exc
    logger.info(
        "vector reindex (dry_run=%s, force=%s, ids=%s): recordings=%s chunks=%s skipped=%s errors=%s",
        payload.dry_run,
        payload.force,
        recording_ids,
        result.processed_recordings,
        result.chunks,
        result.skipped,
        len(result.errors),
    )
    return VectorReindexOut(
        ok=not result.errors,
        processed_recordings=result.processed_recordings,
        chunks=result.chunks,
        skipped=result.skipped,
        errors=result.errors[:100],
    )


@router.get("/search", response_model=VectorSearchOut)
async def vector_search(
    q: str = Query(..., min_length=1, max_length=200, description="搜索关键词/自然语言"),
    mode: Literal["keyword", "hybrid", "semantic"] = Query("hybrid"),
    limit: int = Query(default=0, ge=0, le=50, description="返回条数，0 表示默认 ECHOIC_VECTOR_TOP_K"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> VectorSearchOut:
    """Keyword / semantic / hybrid search over transcript segments (ACL scoped)."""
    effective_limit = int(limit or settings.vector_top_k or 8)
    result = await retrieve_for_query(db, user, q, mode=mode, limit=effective_limit)
    return VectorSearchOut(
        query=result.query,
        mode=result.mode,
        results=[VectorSearchHit(**hit.as_search_hit()) for hit in result.hits],
        fallback_reason=result.fallback_reason,
    )
