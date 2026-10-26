"""Unified keyword / semantic / hybrid retrieval (R-20 Phase 1, v0.3.0).

This module is the single entry point used by ``GET /api/vector/search`` and by
the RAG chat integration in ``services/chat.py``:

* :func:`retrieve_for_query` — global search (admin sees all, members only their
  own recordings; trashed recordings are always excluded).  ``mode`` is
  ``keyword``, ``semantic`` or ``hybrid``.  When embeddings/index are not
  available, ``semantic``/``hybrid`` degrade (semantic → hybrid → keyword) and
  ``RetrievalResult.fallback_reason`` explains why; results stay keyword based.
* :func:`retrieve_for_chat` — single-recording RAG retrieval returning chunk
  hits aligned with transcript segments/speakers.

Hybrid ranking is Reciprocal Rank Fusion (RRF, ``k=60``) over the keyword
segment list and the vector chunk list, deduped by ``segment_id`` (falling back
to chunk id) so the same transcript moment never appears twice.

Lead integration (``services/chat.py``): inside the async ``_retrieve`` helper,
replace the hand rolled score selection with::

    from .retrieval import retrieve_for_chat

    with SessionLocal() as db:
        retrieval = await retrieve_for_chat(db, recording_id, question, top_k=TOP_CHUNKS)
    chunks_by_id = {chunk.id: chunk for chunk in chunks}
    selected = [
        chunk_index[hit.chunk_id]
        for hit in retrieval.chunks
        if hit.chunk_id in chunk_index
    ]

``ChatChunkHit`` already carries ``segment_id`` / ``speaker`` / ``start_ms`` /
``end_ms``, so the existing citation builder can stay as is.  With no
embeddings configured the helper reproduces the v0.2 lexical behaviour (mode
``"lexical"``), keeping chat RAG non-regressive.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

import numpy as np
from sqlalchemy import or_, select

from ..config import settings
from ..core.errors import ProviderError
from ..deps import is_admin
from ..models import Chunk, Recording, Speaker, TranscriptSegment, User
from ..providers.registry import build_embedding_provider
from .search import escape_like, make_snippet
from .settings_store import load_ai_config
from .vector_store import (
    RRF_K,
    VectorHit,
    VectorStoreUnavailable,
    get_vector_store,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ChatChunkHit",
    "ChatRetrieval",
    "RetrievalHit",
    "RetrievalResult",
    "retrieve_for_chat",
    "retrieve_for_query",
]

CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
EN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.#_-]{1,}")

#: semantic candidates fetched before RRF / truncation
VECTOR_CANDIDATES = 40
#: snippet radius for hit texts
SNIPPET_RADIUS = 80


# ------------------------------------------------------------------ result shapes
@dataclass
class RetrievalHit:
    """One API-facing search hit (maps to ``schemas.VectorSearchHit``)."""

    recording_id: int
    recording_title: str = ""
    segment_id: int | None = None
    speaker: str = ""
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    score: float = 0.0
    chunk_id: int | None = None
    source: str = "keyword"

    def as_search_hit(self) -> dict[str, Any]:
        return {
            "recording_id": int(self.recording_id),
            "recording_title": self.recording_title or "",
            "segment_id": self.segment_id,
            "speaker": self.speaker or "",
            "start_ms": int(self.start_ms or 0),
            "end_ms": int(self.end_ms or 0),
            "text": self.text or "",
            "score": round(float(self.score or 0.0), 6),
        }


@dataclass
class RetrievalResult:
    query: str
    mode: str
    hits: list[RetrievalHit] = field(default_factory=list)
    fallback_reason: str | None = None


@dataclass
class ChatChunkHit:
    """One chat RAG chunk hit with its transcript segment metadata."""

    chunk_id: int
    recording_id: int
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    score: float = 0.0
    segment_id: int | None = None
    speaker: str = ""
    idx: int = 0


@dataclass
class ChatRetrieval:
    mode: str = "lexical"
    chunks: list[ChatChunkHit] = field(default_factory=list)
    fallback_reason: str | None = None


# ------------------------------------------------------------------ ACL helpers
def _visible_recording_ids(db, user: User | None) -> list[int] | None:
    """``None`` = admin / no filter; a list = member-visible recording ids."""
    if user is None or is_admin(user):
        return None
    return [
        int(value)
        for value in db.scalars(
            select(Recording.id).where(
                Recording.owner_id == user.id, Recording.deleted_at.is_(None)
            )
        ).all()
    ]


# ------------------------------------------------------------------ embedding setup
def _resolve_embedding_provider(cfg) -> tuple[Any | None, str | None, str | None]:
    """Return ``(provider, model, reason)``; ``provider is None`` carries a reason."""
    name = (cfg.embeddings.provider or "none").strip()
    if name in ("none", ""):
        return None, None, "未配置 Embedding 模型，语义检索不可用"
    try:
        provider = build_embedding_provider(cfg.embeddings)
    except ProviderError as exc:
        return None, None, exc.message
    except Exception as exc:  # noqa: BLE001 - defensive, config is user editable
        logger.warning("build embedding provider failed: %s", exc)
        return None, None, f"Embedding 配置不可用：{type(exc).__name__}"
    if provider is None:
        return None, None, "未配置 Embedding 模型，语义检索不可用"
    model = str(getattr(provider, "model", "") or cfg.embeddings.model or "").strip()[:120] or None
    return provider, model, None


async def _embed_query_vector(provider, query: str) -> list[float]:
    embed_query = getattr(provider, "embed_query", None)
    if callable(embed_query):
        vectors = await embed_query([query])
    else:
        vectors = await provider.embed([query])
    if not vectors:
        raise ProviderError("Embedding 服务未返回查询向量", provider="embeddings")
    return [float(value) for value in vectors[0]]


# ------------------------------------------------------------------ keyword side
def _keyword_score(text: str, needle: str) -> float:
    lowered = (text or "").lower()
    token = (needle or "").lower()
    if not lowered or not token:
        return 0.0
    count = lowered.count(token)
    if count <= 0:
        return 0.0
    density = count / max(1, len(lowered))
    return round(min(1.0, 0.55 + 0.12 * min(count, 4) + min(0.2, density * 8)), 4)


def _keyword_hits(db, user: User | None, query: str, limit: int) -> list[RetrievalHit]:
    needle = (query or "").strip()
    if not needle:
        return []
    scope_ids = _visible_recording_ids(db, user)
    if scope_ids is not None and not scope_ids:
        return []
    pattern = f"%{escape_like(needle)}%"
    stmt = (
        select(TranscriptSegment, Recording, Speaker)
        .join(Recording, Recording.id == TranscriptSegment.recording_id)
        .outerjoin(Speaker, Speaker.id == TranscriptSegment.speaker_id)
        .where(
            TranscriptSegment.text.ilike(pattern, escape="\\"),
            Recording.deleted_at.is_(None),
        )
        .order_by(TranscriptSegment.recording_id.asc(), TranscriptSegment.start_ms.asc())
        .limit(max(limit * 4, VECTOR_CANDIDATES))
    )
    if scope_ids is not None:
        stmt = stmt.where(TranscriptSegment.recording_id.in_(scope_ids))
    hits: list[RetrievalHit] = []
    for segment, recording, speaker in db.execute(stmt).all():
        speaker_name = (speaker.display_name or speaker.label) if speaker is not None else ""
        hits.append(
            RetrievalHit(
                recording_id=int(recording.id),
                recording_title=recording.title or "",
                segment_id=int(segment.id),
                speaker=speaker_name,
                start_ms=int(segment.start_ms or 0),
                end_ms=int(segment.end_ms or 0),
                text=make_snippet(segment.text or "", needle, radius=60),
                score=_keyword_score(segment.text or "", needle),
                source="keyword",
            )
        )
    if len(hits) < limit:
        existing = {hit.recording_id for hit in hits}
        for title_hit in _recording_title_hits(db, scope_ids, needle, limit - len(hits)):
            if title_hit.recording_id in existing:
                continue
            existing.add(title_hit.recording_id)
            hits.append(title_hit)
    return hits


def _recording_title_hits(
    db, scope_ids: list[int] | None, needle: str, limit: int
) -> list[RetrievalHit]:
    if limit <= 0:
        return []
    pattern = f"%{escape_like(needle)}%"
    stmt = (
        select(Recording)
        .where(
            Recording.deleted_at.is_(None),
            or_(
                Recording.title.ilike(pattern, escape="\\"),
                Recording.description.ilike(pattern, escape="\\"),
            ),
        )
        .order_by(Recording.recorded_at.desc(), Recording.id.desc())
        .limit(limit)
    )
    if scope_ids is not None:
        stmt = stmt.where(Recording.id.in_(scope_ids))
    hits: list[RetrievalHit] = []
    for recording in db.scalars(stmt).all():
        source_text = recording.description or recording.title or ""
        hits.append(
            RetrievalHit(
                recording_id=int(recording.id),
                recording_title=recording.title or "",
                segment_id=None,
                speaker="",
                start_ms=0,
                end_ms=0,
                text=make_snippet(source_text, needle, radius=60),
                score=0.5,
                source="keyword",
            )
        )
    return hits


# ------------------------------------------------------------------ vector side
def _overlapping_segment(
    segments: Sequence[TranscriptSegment], start_ms: int, end_ms: int
) -> TranscriptSegment | None:
    if not segments:
        return None
    best: TranscriptSegment | None = None
    best_overlap = 0
    end_ms = max(int(end_ms or 0), int(start_ms or 0) + 1)
    for segment in segments:
        segment_start = int(segment.start_ms or 0)
        if segment_start >= end_ms:
            break
        segment_end = int(segment.end_ms or 0)
        overlap = min(end_ms, segment_end) - max(int(start_ms or 0), segment_start)
        if overlap > best_overlap:
            best = segment
            best_overlap = overlap
    return best


def _load_segments_and_speakers(
    db, recording_ids: Sequence[int]
) -> tuple[dict[int, list[TranscriptSegment]], dict[int, Speaker]]:
    ids = sorted({int(value) for value in recording_ids})
    if not ids:
        return {}, {}
    segments = list(
        db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.recording_id.in_(ids))
            .order_by(TranscriptSegment.recording_id.asc(), TranscriptSegment.start_ms.asc())
        ).all()
    )
    speakers = {
        int(speaker.id): speaker
        for speaker in db.scalars(
            select(Speaker).where(Speaker.recording_id.in_(ids))
        ).all()
    }
    by_recording: dict[int, list[TranscriptSegment]] = {}
    for segment in segments:
        by_recording.setdefault(int(segment.recording_id), []).append(segment)
    return by_recording, speakers


def _speaker_name(speaker: Speaker | None) -> str:
    if speaker is None:
        return ""
    return speaker.display_name or speaker.label or ""


def _attach_vector_metadata(
    db, query: str, raw_hits: Sequence[VectorHit]
) -> list[RetrievalHit]:
    if not raw_hits:
        return []
    chunk_ids = [int(hit.chunk_id) for hit in raw_hits]
    recording_ids = {int(hit.recording_id) for hit in raw_hits}
    chunks = {
        int(chunk.id): chunk
        for chunk in db.scalars(select(Chunk).where(Chunk.id.in_(chunk_ids))).all()
    }
    recordings = {
        int(recording.id): recording
        for recording in db.scalars(
            select(Recording).where(
                Recording.id.in_(sorted(recording_ids)), Recording.deleted_at.is_(None)
            )
        ).all()
    }
    segments_by_recording, speakers = _load_segments_and_speakers(db, recording_ids)
    results: list[RetrievalHit] = []
    for hit in raw_hits:
        chunk = chunks.get(int(hit.chunk_id))
        recording = recordings.get(int(hit.recording_id))
        if chunk is None or recording is None:
            continue
        segment = _overlapping_segment(
            segments_by_recording.get(int(hit.recording_id), []),
            int(hit.start_ms),
            int(hit.end_ms),
        )
        segment_id = None
        start_ms = int(hit.start_ms or 0)
        end_ms = int(hit.end_ms or 0)
        speaker = ""
        if segment is not None:
            segment_id = int(segment.id)
            start_ms = int(segment.start_ms or 0)
            end_ms = int(segment.end_ms or 0)
            if segment.speaker_id:
                speaker = _speaker_name(speakers.get(int(segment.speaker_id)))
        results.append(
            RetrievalHit(
                recording_id=int(recording.id),
                recording_title=recording.title or "",
                segment_id=segment_id,
                speaker=speaker,
                start_ms=start_ms,
                end_ms=end_ms,
                text=make_snippet(chunk.text or "", query, radius=SNIPPET_RADIUS),
                score=round(float(hit.score), 6),
                chunk_id=int(chunk.id),
                source="vector",
            )
        )
    return results


async def _semantic_hits(
    db, user: User | None, query: str, *, top_k: int, min_score: float
) -> tuple[list[RetrievalHit], str | None]:
    """Return ``(hits, fallback_reason)``; ``reason`` is set when unavailable."""
    cfg = load_ai_config(db)
    provider, model, reason = _resolve_embedding_provider(cfg)
    if provider is None:
        return [], reason
    try:
        query_vector = await _embed_query_vector(provider, query)
    except ProviderError as exc:
        return [], exc.message
    except Exception as exc:  # noqa: BLE001 - network/provider failures must degrade
        logger.warning("embed query failed: %s", exc)
        return [], f"查询向量生成失败（{type(exc).__name__}），语义检索不可用"
    dim = len(query_vector)
    scope_ids = _visible_recording_ids(db, user)
    store = get_vector_store()
    try:
        raw_hits = store.search(
            query_vector,
            top_k=max(top_k, VECTOR_CANDIDATES),
            recording_ids=scope_ids,
            min_score=min_score,
            model=model,
            dim=dim,
        )
    except VectorStoreUnavailable as exc:
        return [], exc.reason
    except Exception as exc:  # noqa: BLE001
        logger.warning("vector search failed: %s", exc)
        return [], f"向量检索失败（{type(exc).__name__}），语义检索不可用"
    if not raw_hits:
        indexed = 0
        try:
            indexed = int(store.stats(model=model, dim=dim).chunks_indexed or 0)
        except Exception:  # noqa: BLE001 - stats failure is not fatal
            logger.debug("vector store stats failed", exc_info=True)
        if indexed == 0:
            return [], "向量索引为空，请先在设置 → 检索索引中重建"
        return [], None
    return _attach_vector_metadata(db, query, raw_hits), None


# ------------------------------------------------------------------ RRF
def _hit_key(hit: RetrievalHit) -> tuple:
    if hit.segment_id is not None:
        return ("segment", int(hit.segment_id))
    if hit.chunk_id is not None:
        return ("chunk", int(hit.chunk_id))
    return ("recording", int(hit.recording_id))


def _rrf_fuse(
    keyword_hits: Sequence[RetrievalHit],
    semantic_hits: Sequence[RetrievalHit],
    limit: int,
) -> list[RetrievalHit]:
    scores: dict[tuple, float] = {}
    fused: dict[tuple, RetrievalHit] = {}
    for ranked, source in ((keyword_hits, "keyword"), (semantic_hits, "vector")):
        for rank, hit in enumerate(ranked, start=1):
            key = _hit_key(hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
            existing = fused.get(key)
            if existing is None:
                fused[key] = replace(hit, source="hybrid")
                continue
            # Keep the keyword snippet (it highlights the query) and upgrade
            # metadata when only the vector hit mapped to a transcript segment.
            if existing.segment_id is None and hit.segment_id is not None:
                existing.segment_id = hit.segment_id
                existing.speaker = existing.speaker or hit.speaker
                existing.start_ms = hit.start_ms
                existing.end_ms = hit.end_ms
            if hit.chunk_id is not None and existing.chunk_id is None:
                existing.chunk_id = hit.chunk_id
    fused_hits = [
        replace(hit, score=round(scores[key], 6)) for key, hit in fused.items()
    ]
    fused_hits.sort(key=lambda hit: hit.score, reverse=True)
    return fused_hits[:limit]


# ------------------------------------------------------------------ public API
async def retrieve_for_query(
    db,
    user: User | None,
    query: str,
    mode: str = "hybrid",
    limit: int | None = None,
) -> RetrievalResult:
    """Retrieve keyword / semantic / hybrid hits for the global search API.

    ``limit`` defaults to ``ECHOIC_VECTOR_TOP_K`` and is capped at 50.  The
    returned ``mode`` is the *effective* mode after fallback; when degradation
    happened ``fallback_reason`` explains it in Chinese.
    """
    cleaned = (query or "").strip()
    top = max(1, min(int(limit or settings.vector_top_k or 8), 50))
    requested = (mode or "hybrid").strip().lower()
    if requested not in ("keyword", "hybrid", "semantic"):
        requested = "hybrid"
    if not cleaned:
        return RetrievalResult(query=cleaned, mode=requested, hits=[])

    keyword_hits = _keyword_hits(db, user, cleaned, top)
    if requested == "keyword":
        return RetrievalResult(query=cleaned, mode="keyword", hits=keyword_hits[:top])

    semantic_hits, reason = await _semantic_hits(
        db,
        user,
        cleaned,
        top_k=max(top, int(settings.vector_top_k or 8)),
        min_score=float(settings.vector_min_score or 0.0),
    )

    if requested == "semantic":
        if semantic_hits:
            return RetrievalResult(query=cleaned, mode="semantic", hits=semantic_hits[:top])
        if reason:
            return RetrievalResult(
                query=cleaned,
                mode="keyword",
                hits=keyword_hits[:top],
                fallback_reason=f"{reason}，已降级为关键词检索",
            )
        return RetrievalResult(query=cleaned, mode="semantic", hits=[])

    # hybrid
    if semantic_hits:
        return RetrievalResult(
            query=cleaned, mode="hybrid", hits=_rrf_fuse(keyword_hits, semantic_hits, top)
        )
    if reason:
        return RetrievalResult(
            query=cleaned,
            mode="keyword",
            hits=keyword_hits[:top],
            fallback_reason=f"{reason}，已降级为关键词检索",
        )
    return RetrievalResult(
        query=cleaned, mode="hybrid", hits=_rrf_fuse(keyword_hits, [], top)
    )


# ------------------------------------------------------------------ chat retrieval
def _text_tokens(text: str) -> set[str]:
    """CJK bigrams + latin tokens (same tokenizer as the v0.2 chat fallback)."""
    text = (text or "").lower()
    tokens: set[str] = set()
    for run in CJK_RUN_RE.findall(text):
        for index in range(len(run) - 1):
            tokens.add(run[index : index + 2])
    for token in EN_TOKEN_RE.findall(text):
        tokens.add(token)
    return tokens


def _lexical_scores(question: str, chunks: list[Chunk]) -> list[float]:
    query_tokens = _text_tokens(question)
    if not query_tokens or not chunks:
        return [0.0] * len(chunks)
    document_tokens = [_text_tokens(chunk.text or "") for chunk in chunks]
    doc_freq: Counter[str] = Counter()
    for tokens in document_tokens:
        for token in tokens & query_tokens:
            doc_freq[token] += 1
    total = len(chunks)
    idf = {token: math.log(1 + total / (1 + frequency)) for token, frequency in doc_freq.items()}
    normalized_question = re.sub(r"\s+", "", question or "")
    scores: list[float] = []
    for chunk, tokens in zip(chunks, document_tokens):
        shared = query_tokens & tokens
        score = sum(idf.get(token, 1.0) for token in shared) / math.sqrt(len(tokens) + 1)
        if normalized_question and normalized_question in re.sub(r"\s+", "", chunk.text or ""):
            score += 2.0
        scores.append(score)
    return scores


def _embedding_scores(question_vector: Sequence[float], chunks: list[Chunk]) -> list[float]:
    query = np.asarray(question_vector, dtype=np.float32).ravel()
    norm = float(np.linalg.norm(query))
    if norm <= 0:
        return [0.0] * len(chunks)
    query = query / norm
    scores: list[float] = []
    for chunk in chunks:
        if not chunk.embedding or not chunk.dim:
            scores.append(0.0)
            continue
        try:
            vector = np.frombuffer(chunk.embedding, dtype=np.float32)
        except (ValueError, TypeError):
            scores.append(0.0)
            continue
        if vector.size != query.size:
            scores.append(0.0)
            continue
        vector_norm = float(np.linalg.norm(vector))
        if vector_norm <= 0:
            scores.append(0.0)
            continue
        scores.append(float(np.dot(query, vector / vector_norm)))
    return scores


async def retrieve_for_chat(
    db, recording_id: int, query: str, top_k: int = 8
) -> ChatRetrieval:
    """Retrieve ranked chunks for RAG chat on one recording.

    Uses the configured embedding provider when the recording has vectors,
    otherwise falls back to the v0.2 lexical scoring (mode ``"lexical"``).
    Trashed/missing recordings return an empty result with a reason.
    """
    cleaned = (query or "").strip()
    top_k = max(1, min(int(top_k or 8), 50))
    if not cleaned:
        return ChatRetrieval(mode="empty", fallback_reason="问题不能为空")
    recording = db.get(Recording, int(recording_id))
    if recording is None or getattr(recording, "deleted_at", None) is not None:
        return ChatRetrieval(mode="empty", fallback_reason="录音不存在或已被删除")
    chunks = list(
        db.scalars(
            select(Chunk)
            .where(Chunk.recording_id == int(recording_id))
            .order_by(Chunk.idx.asc(), Chunk.id.asc())
        ).all()
    )
    if not chunks:
        return ChatRetrieval(mode="empty", fallback_reason="录音没有可检索的文本片段")

    cfg = load_ai_config(db)
    provider, _model, reason = _resolve_embedding_provider(cfg)
    scores: list[float] | None = None
    mode = "lexical"
    fallback_reason: str | None = reason
    if provider is not None:
        has_vectors = any(chunk.embedding for chunk in chunks)
        if has_vectors:
            try:
                query_vector = await _embed_query_vector(provider, cleaned)
                candidate_scores = _embedding_scores(query_vector, chunks)
                if any(score > 0 for score in candidate_scores):
                    scores = candidate_scores
                    mode = "vector"
                    fallback_reason = None
                else:
                    fallback_reason = "向量检索未命中，已降级为关键词检索"
            except ProviderError as exc:
                fallback_reason = f"{exc.message}，已降级为关键词检索"
            except Exception as exc:  # noqa: BLE001
                logger.warning("chat query embedding failed: %s", exc)
                fallback_reason = f"查询向量生成失败（{type(exc).__name__}），已降级为关键词检索"
        else:
            fallback_reason = "录音尚未生成向量索引，使用关键词检索"
    if scores is None:
        scores = _lexical_scores(cleaned, chunks)
        mode = "lexical"

    ranked = sorted(range(len(chunks)), key=lambda index: scores[index], reverse=True)
    selected = [index for index in ranked[:top_k] if scores[index] > 0]
    if not selected:
        selected = ranked[: min(3, len(ranked))]

    segments_by_recording, speakers = _load_segments_and_speakers(db, [int(recording_id)])
    segments = segments_by_recording.get(int(recording_id), [])
    hits: list[ChatChunkHit] = []
    for index in selected:
        chunk = chunks[index]
        segment = _overlapping_segment(segments, int(chunk.start_ms or 0), int(chunk.end_ms or 0))
        segment_id = int(segment.id) if segment is not None else None
        speaker = ""
        if segment is not None and segment.speaker_id:
            speaker = _speaker_name(speakers.get(int(segment.speaker_id)))
        hits.append(
            ChatChunkHit(
                chunk_id=int(chunk.id),
                recording_id=int(chunk.recording_id),
                start_ms=int(segment.start_ms if segment is not None else chunk.start_ms or 0),
                end_ms=int(segment.end_ms if segment is not None else chunk.end_ms or 0),
                text=chunk.text or "",
                score=round(float(scores[index]), 6),
                segment_id=segment_id,
                speaker=speaker,
                idx=int(chunk.idx or 0),
            )
        )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return ChatRetrieval(mode=mode, chunks=hits, fallback_reason=fallback_reason)
