"""Vector index backends for R-20 Phase 1 (v0.3.0).

Two implementations share a small protocol so callers (retrieval service / API /
reindex CLI) do not care which backend is active:

* :class:`NumpyVectorStore` — the default and ``auto`` fallback.  It loads the
  embedding BLOBs of the *visible* (non-trashed) recordings into an
  L2-normalized float32 matrix, caches the snapshot per
  ``(recording scope, model, dim)`` and answers cosine top-k with one matrix
  multiplication.  A cheap DB signature (row count / max id) plus a
  process-wide generation counter decide when the snapshot must be reloaded
  after writes; the pipeline calls :func:`index_recording_vectors` so fresh
  embeddings invalidate it explicitly.
* :class:`PgVectorStore` — experimental PostgreSQL + pgvector backend.  It only
  activates for a real PostgreSQL database with the ``vector`` extension
  installed; every failure raises :class:`VectorStoreUnavailable` with a
  friendly Chinese reason so :func:`get_vector_store` can degrade to numpy.

``chunks.embedding`` (float32 BLOB) + ``chunks.dim`` + ``chunks.embedding_model``
remain the portable source of truth; the pgvector table is a mirrored ANN index.
The module also owns the reindex job shared by ``POST /api/vector/reindex`` and
``python -m app.tools.vector_reindex``.

No network calls are made here; embedding API calls happen through the provider
object passed to :func:`reindex_recordings`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

import numpy as np
from sqlalchemy import case, func, or_, select, text

from ..config import settings
from ..models import Chunk, Recording

logger = logging.getLogger(__name__)

__all__ = [
    "EMBED_BATCH_SIZE",
    "MAX_REINDEX_RECORDINGS",
    "NumpyVectorStore",
    "PgVectorStore",
    "SqliteVecVectorStore",
    "ReindexResult",
    "VectorHit",
    "VectorRecord",
    "VectorStore",
    "VectorStoreStats",
    "VectorStoreUnavailable",
    "chunk_index_counts",
    "embed_query_sync",
    "get_vector_store",
    "index_recording_vectors",
    "vector_index_chunks",
    "invalidate_vector_cache",
    "reindex_recordings",
    "reset_vector_store_cache",
    "run_coroutine_sync",
    "sqlite_vec_available",
]

#: reindex embedding request batch size (docs/10 §5.1: batches ≤ 10)
EMBED_BATCH_SIZE = 10
#: hard cap for one synchronous reindex request / CLI batch
MAX_REINDEX_RECORDINGS = 200
#: number of cached numpy snapshots (admin scope + a handful of member scopes)
MAX_CACHE_SCOPES = 8
#: RRF smoothing constant (Cormack et al.; same default as the literature)
RRF_K = 60

#: bumped by every explicit invalidation; part of the numpy cache signature so
#: out-of-band writes (pipeline embeds, reindex, tests resetting the DB) reload.
_GENERATION = 0
_GENERATION_LOCK = threading.Lock()


class VectorStoreUnavailable(RuntimeError):
    """Raised when a vector backend cannot be used; ``reason`` is user friendly."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ------------------------------------------------------------------ data shapes
@dataclass(frozen=True)
class VectorRecord:
    """Portable vector row (pgvector mirror / test fixtures)."""

    chunk_id: int
    recording_id: int
    idx: int
    start_ms: int
    end_ms: int
    vector: Sequence[float]
    model: str = ""
    dim: int = 0


@dataclass(frozen=True)
class VectorHit:
    """One cosine hit, sorted descending by ``score``."""

    chunk_id: int
    recording_id: int
    score: float
    idx: int = 0
    start_ms: int = 0
    end_ms: int = 0


@dataclass
class VectorStoreStats:
    backend: str = "numpy"
    available: bool = True
    chunks_total: int = 0
    chunks_indexed: int = 0
    stale_chunks: int = 0
    dim: int | None = None
    model: str | None = None
    fallback_reason: str | None = None


@dataclass
class ReindexResult:
    processed_recordings: int = 0
    chunks: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@runtime_checkable
class VectorStore(Protocol):
    backend: str

    def search(
        self,
        query: Sequence[float],
        *,
        top_k: int = 40,
        recording_ids: Sequence[int] | None = None,
        min_score: float = 0.0,
        model: str | None = None,
        dim: int | None = None,
    ) -> list[VectorHit]: ...

    def stats(self, *, model: str | None = None, dim: int | None = None) -> VectorStoreStats: ...

    def health(self) -> tuple[bool, str]: ...

    def invalidate(self) -> None: ...


# ------------------------------------------------------------------ small utils
def invalidate_vector_cache() -> None:
    """Invalidate every numpy snapshot in this process (called after writes)."""
    global _GENERATION
    with _GENERATION_LOCK:
        _GENERATION += 1


def _normalize(vector: Sequence[float] | np.ndarray) -> np.ndarray | None:
    array = np.asarray(vector, dtype=np.float32).ravel()
    if array.size == 0:
        return None
    norm = float(np.linalg.norm(array))
    if not norm or not np.isfinite(norm):
        return None
    return array / norm


def _vector_literal(vector: Sequence[float]) -> str:
    values = ", ".join(f"{float(value):.8g}" for value in vector)
    return f"[{values}]"


def _is_stale(chunk: Chunk, *, model: str | None, dim: int | None) -> bool:
    """True when a chunk must be (re)embedded for the current model/dim."""
    if not chunk.embedding or not chunk.dim:
        return True
    if dim and int(chunk.dim) != int(dim):
        return True
    stored_model = (chunk.embedding_model or "").strip()
    if model and stored_model and stored_model != model:
        return True
    return False


def _embedding_model_name(provider: Any, model: str | None = None) -> str:
    value = model if model is not None else getattr(provider, "model", "") or ""
    return str(value).strip()[:120]


# ------------------------------------------------------------------ DB counts
def _chunk_scope_conditions(recording_ids: Sequence[int] | None) -> list:
    conditions = [Recording.deleted_at.is_(None)]
    if recording_ids is not None:
        conditions.append(Chunk.recording_id.in_([int(i) for i in recording_ids]))
    return conditions


def chunk_index_counts(
    db, *, model: str | None = None, dim: int | None = None, recording_ids: Sequence[int] | None = None
) -> dict[str, int]:
    """Return ``{total, indexed, stale}`` chunk counters for the visible scope.

    ``stale`` counts chunks whose embedding does not match the requested
    ``model``/``dim`` (unlabeled legacy vectors — ``embedding_model IS NULL`` —
    are treated as current, so the pipeline keeps working before the Lead wires
    the ``embedding_model`` write).
    """
    conditions = _chunk_scope_conditions(recording_ids)
    total = int(
        db.scalar(
            select(func.count(Chunk.id))
            .join(Recording, Recording.id == Chunk.recording_id)
            .where(*conditions)
        )
        or 0
    )
    indexed_conditions = [*conditions, Chunk.embedding.is_not(None)]
    indexed = int(
        db.scalar(
            select(func.count(Chunk.id))
            .join(Recording, Recording.id == Chunk.recording_id)
            .where(*indexed_conditions)
        )
        or 0
    )
    stale_conditions = [*indexed_conditions]
    mismatch: list = []
    if dim:
        mismatch.append(Chunk.dim.is_(None))
        mismatch.append(Chunk.dim != int(dim))
    if model:
        mismatch.append(
            (Chunk.embedding_model.is_not(None))
            & (Chunk.embedding_model != "")
            & (Chunk.embedding_model != model)
        )
    if mismatch:
        stale_conditions.append(or_(*mismatch))
        stale = int(
            db.scalar(
                select(func.count(Chunk.id))
                .join(Recording, Recording.id == Chunk.recording_id)
                .where(*stale_conditions)
            )
            or 0
        )
    else:
        stale = 0
    return {"total": total, "indexed": indexed, "stale": stale}


# ------------------------------------------------------------------ coroutine bridge
def run_coroutine_sync(coroutine):
    """Run an async embedding call from synchronous code (API threadpool / CLI).

    FastAPI sync endpoints and the CLI have no running loop, so ``asyncio.run``
    is safe; if a loop is already running (e.g. a service call inside an async
    test) the coroutine runs in a dedicated worker thread instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = asyncio.run(coroutine)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            box["error"] = exc

    thread = threading.Thread(target=_runner, name="echoic-embed-sync", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def embed_query_sync(provider, text: str) -> list[float]:
    """Synchronously embed one query using the provider's preferred method."""
    embed_query = getattr(provider, "embed_query", None)
    if callable(embed_query):
        vectors = run_coroutine_sync(embed_query([text]))
    else:
        vectors = run_coroutine_sync(provider.embed([text]))
    if not vectors:
        raise VectorStoreUnavailable("Embedding 服务未返回查询向量")
    return [float(value) for value in vectors[0]]


# ------------------------------------------------------------------ numpy store
@dataclass
class _NumpySnapshot:
    signature: tuple
    matrix: np.ndarray | None
    chunk_ids: np.ndarray
    recording_ids: np.ndarray
    idxs: np.ndarray
    starts: np.ndarray
    ends: np.ndarray


class NumpyVectorStore:
    """In-process cosine index over ``chunks.embedding`` (default backend)."""

    backend = "numpy"

    def __init__(self, db_factory=None) -> None:
        if db_factory is None:
            from ..db import SessionLocal

            db_factory = SessionLocal
        self._db_factory = db_factory
        self._cache: OrderedDict[tuple, _NumpySnapshot] = OrderedDict()
        self._cache_lock = threading.Lock()
        self.fallback_reason: str | None = None

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _scope_key(recording_ids: Sequence[int] | None):
        if recording_ids is None:
            return None
        return tuple(sorted({int(value) for value in recording_ids}))

    def _signature(self, recording_ids: Sequence[int] | None) -> tuple:
        conditions = _chunk_scope_conditions(recording_ids)
        with self._db_factory() as db:
            row = db.execute(
                select(
                    func.count(Chunk.id),
                    func.coalesce(func.max(Chunk.id), 0),
                    func.coalesce(
                        func.sum(case((Chunk.embedding.is_not(None), 1), else_=0)), 0
                    ),
                )
                .join(Recording, Recording.id == Chunk.recording_id)
                .where(*conditions)
            ).one()
        with _GENERATION_LOCK:
            generation = _GENERATION
        return (generation, int(row[0] or 0), int(row[1] or 0))

    def _load_snapshot(
        self, recording_ids: Sequence[int] | None, model: str | None, dim: int | None
    ) -> _NumpySnapshot:
        conditions = [
            *_chunk_scope_conditions(recording_ids),
            Chunk.embedding.is_not(None),
        ]
        if model:
            conditions.append(
                or_(
                    Chunk.embedding_model == model,
                    Chunk.embedding_model.is_(None),
                    Chunk.embedding_model == "",
                )
            )
        if dim:
            conditions.append(Chunk.dim == int(dim))
        with self._db_factory() as db:
            rows = db.execute(
                select(
                    Chunk.id,
                    Chunk.recording_id,
                    Chunk.idx,
                    Chunk.start_ms,
                    Chunk.end_ms,
                    Chunk.embedding,
                    Chunk.dim,
                )
                .join(Recording, Recording.id == Chunk.recording_id)
                .where(*conditions)
                .order_by(Chunk.recording_id.asc(), Chunk.id.asc())
            ).all()
        vectors: list[np.ndarray] = []
        chunk_ids: list[int] = []
        recording_ids_out: list[int] = []
        idxs: list[int] = []
        starts: list[int] = []
        ends: list[int] = []
        for row in rows:
            try:
                array = np.frombuffer(row.embedding, dtype=np.float32)
            except (TypeError, ValueError):
                continue
            if dim and array.size != int(dim):
                continue
            normalized = _normalize(array)
            if normalized is None:
                continue
            vectors.append(normalized)
            chunk_ids.append(int(row.id))
            recording_ids_out.append(int(row.recording_id))
            idxs.append(int(row.idx or 0))
            starts.append(int(row.start_ms or 0))
            ends.append(int(row.end_ms or 0))
        if not vectors:
            return _NumpySnapshot(
                signature=(0, 0),
                matrix=None,
                chunk_ids=np.empty(0, dtype=np.int64),
                recording_ids=np.empty(0, dtype=np.int64),
                idxs=np.empty(0, dtype=np.int64),
                starts=np.empty(0, dtype=np.int64),
                ends=np.empty(0, dtype=np.int64),
            )
        return _NumpySnapshot(
            signature=(0, 0),
            matrix=np.vstack(vectors).astype(np.float32, copy=False),
            chunk_ids=np.asarray(chunk_ids, dtype=np.int64),
            recording_ids=np.asarray(recording_ids_out, dtype=np.int64),
            idxs=np.asarray(idxs, dtype=np.int64),
            starts=np.asarray(starts, dtype=np.int64),
            ends=np.asarray(ends, dtype=np.int64),
        )

    # ---------------------------------------------------------------- protocol
    def health(self) -> tuple[bool, str]:
        return True, "numpy 内存向量索引可用（单进程）"

    def invalidate(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def search(
        self,
        query: Sequence[float],
        *,
        top_k: int = 40,
        recording_ids: Sequence[int] | None = None,
        min_score: float = 0.0,
        model: str | None = None,
        dim: int | None = None,
    ) -> list[VectorHit]:
        top_k = max(0, int(top_k))
        if top_k == 0:
            return []
        query_array = _normalize(query)
        if query_array is None:
            return []
        if dim is None:
            dim = int(query_array.size)
        elif int(dim) != int(query_array.size):
            return []
        if recording_ids is not None and len(recording_ids) == 0:
            return []
        scope_key = self._scope_key(recording_ids)
        cache_key = (scope_key, model, int(dim))
        signature = self._signature(recording_ids)
        with self._cache_lock:
            snapshot = self._cache.get(cache_key)
            if snapshot is None or snapshot.signature != signature:
                loaded = self._load_snapshot(recording_ids, model, dim)
                loaded.signature = signature
                self._cache[cache_key] = loaded
                self._cache.move_to_end(cache_key)
                while len(self._cache) > MAX_CACHE_SCOPES:
                    self._cache.popitem(last=False)
                snapshot = loaded
            else:
                self._cache.move_to_end(cache_key)
        if snapshot.matrix is None or len(snapshot.chunk_ids) == 0:
            return []
        scores = snapshot.matrix @ query_array
        count = len(scores)
        take = min(top_k, count)
        if take < count:
            candidate_indexes = np.argpartition(-scores, take - 1)[:take]
        else:
            candidate_indexes = np.arange(count)
        ordered = candidate_indexes[np.argsort(-scores[candidate_indexes], kind="stable")]
        hits: list[VectorHit] = []
        for index in ordered:
            score = float(scores[index])
            if score < min_score:
                break
            hits.append(
                VectorHit(
                    chunk_id=int(snapshot.chunk_ids[index]),
                    recording_id=int(snapshot.recording_ids[index]),
                    score=score,
                    idx=int(snapshot.idxs[index]),
                    start_ms=int(snapshot.starts[index]),
                    end_ms=int(snapshot.ends[index]),
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    def stats(self, *, model: str | None = None, dim: int | None = None) -> VectorStoreStats:
        with self._db_factory() as db:
            counts = chunk_index_counts(db, model=model, dim=dim)
        return VectorStoreStats(
            backend=self.backend,
            available=True,
            chunks_total=counts["total"],
            chunks_indexed=counts["indexed"],
            stale_chunks=counts["stale"],
            dim=dim,
            model=model,
            fallback_reason=self.fallback_reason,
        )

    # ---------------------------------------------------------------- maintenance
    @staticmethod
    def _write_rows(connection, rows: Sequence[VectorRecord], dim: int) -> None:
        if not rows:
            return
        sql = text(
            "INSERT INTO chunk_vectors "
            "(chunk_id, recording_id, idx, start_ms, end_ms, embedding, model, dim) "
            "VALUES (:chunk_id, :recording_id, :idx, :start_ms, :end_ms, "
            "CAST(:embedding AS vector), :model, :dim) "
            "ON CONFLICT (chunk_id) DO UPDATE SET "
            "recording_id = EXCLUDED.recording_id, idx = EXCLUDED.idx, "
            "start_ms = EXCLUDED.start_ms, end_ms = EXCLUDED.end_ms, "
            "embedding = EXCLUDED.embedding, model = EXCLUDED.model, dim = EXCLUDED.dim"
        )
        for offset in range(0, len(rows), 100):
            batch = rows[offset : offset + 100]
            connection.execute(
                sql,
                [
                    {
                        "chunk_id": int(row.chunk_id),
                        "recording_id": int(row.recording_id),
                        "idx": int(row.idx),
                        "start_ms": int(row.start_ms),
                        "end_ms": int(row.end_ms),
                        "embedding": _vector_literal(row.vector),
                        "model": (row.model or "")[:120],
                        "dim": int(row.dim or dim),
                    }
                    for row in batch
                ],
            )

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        rows = list(records)
        if not rows:
            return
        dims = {int(row.dim or len(row.vector)) for row in rows}
        if len(dims) != 1:
            raise VectorStoreUnavailable("同一批写入的向量维度不一致，无法写入 pgvector")
        dim = dims.pop()
        if dim <= 0:
            raise VectorStoreUnavailable("向量维度非法，无法写入 pgvector")
        try:
            self._ensure_extension()
            with self.engine.begin() as connection:
                self._ensure_table(connection, dim)
                self._write_rows(connection, rows, dim)
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(
                f"pgvector 写入失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    def sync_recording(self, db, recording_id: int) -> None:
        """Atomically mirror one recording's chunk embeddings into ``chunk_vectors``.

        The recording's existing mirror rows are replaced as a whole so chunks
        deleted by a reprocess never leave orphan vectors behind.
        """
        rows = db.execute(
            select(
                Chunk.id,
                Chunk.recording_id,
                Chunk.idx,
                Chunk.start_ms,
                Chunk.end_ms,
                Chunk.embedding,
                Chunk.dim,
                Chunk.embedding_model,
            ).where(Chunk.recording_id == int(recording_id), Chunk.embedding.is_not(None))
        ).all()
        records: list[VectorRecord] = []
        for row in rows:
            try:
                array = np.frombuffer(row.embedding, dtype=np.float32)
            except (TypeError, ValueError):
                continue
            if array.size == 0:
                continue
            records.append(
                VectorRecord(
                    chunk_id=int(row.id),
                    recording_id=int(row.recording_id),
                    idx=int(row.idx or 0),
                    start_ms=int(row.start_ms or 0),
                    end_ms=int(row.end_ms or 0),
                    vector=array.tolist(),
                    model=(row.embedding_model or "")[:120],
                    dim=int(row.dim or array.size),
                )
            )
        if not records:
            # Nothing to mirror (e.g. embeddings not generated yet): clear stale rows.
            self.delete_recording(recording_id)
            return
        dims = {int(record.dim or len(record.vector)) for record in records}
        if len(dims) != 1:
            raise VectorStoreUnavailable("该录音的向量维度不一致，无法同步到 pgvector（请先重建索引）")
        dim = dims.pop()
        if dim <= 0:
            raise VectorStoreUnavailable("向量维度非法，无法同步到 pgvector")
        try:
            self._ensure_extension()
            with self.engine.begin() as connection:
                self._ensure_table(connection, dim)
                connection.execute(
                    text("DELETE FROM chunk_vectors WHERE recording_id = :recording_id"),
                    {"recording_id": int(recording_id)},
                )
                self._write_rows(connection, records, dim)
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(
                f"pgvector 同步失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    def delete_recording(self, recording_id: int) -> int:
        self.invalidate()
        return 0


# ------------------------------------------------------------------ pgvector store
_PG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id BIGINT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    recording_id BIGINT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    idx INT NOT NULL DEFAULT 0,
    start_ms BIGINT NOT NULL DEFAULT 0,
    end_ms BIGINT NOT NULL DEFAULT 0,
    embedding vector({dim}) NOT NULL,
    model VARCHAR(120),
    dim INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class PgVectorStore:
    """Experimental PostgreSQL + pgvector backend (degrades to numpy when absent)."""

    backend = "pgvector"
    experimental = True

    def __init__(self, engine=None) -> None:
        if engine is None:
            from ..db import engine as default_engine

            engine = default_engine
        self.engine = engine
        self._extension_error: str | None = None
        self._table_dims: set[int] = set()

    # ---------------------------------------------------------------- setup
    def _ensure_extension(self) -> None:
        if self.engine.dialect.name != "postgresql":
            raise VectorStoreUnavailable("当前数据库不是 PostgreSQL，pgvector 后端不可用")
        if self._extension_error:
            raise VectorStoreUnavailable(self._extension_error)
        try:
            with self.engine.begin() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:  # noqa: BLE001 - permission/extension missing
            reason = f"pgvector 扩展不可用：{type(exc).__name__}: {str(exc)[:180]}"
            self._extension_error = reason
            raise VectorStoreUnavailable(reason) from exc

    def _ensure_table(self, connection, dim: int) -> None:
        if dim in self._table_dims:
            return
        try:
            connection.execute(text(_PG_TABLE_DDL.format(dim=int(dim))))
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_chunk_vectors_recording "
                    "ON chunk_vectors (recording_id)"
                )
            )
            # HNSW is an optimization; older pgvector / restricted roles may not
            # allow it, so fail inside a savepoint (never abort the outer txn).
            if int(dim) <= 2000:
                try:
                    with connection.begin_nested():
                        connection.execute(
                            text(
                                "CREATE INDEX IF NOT EXISTS ix_chunk_vectors_hnsw "
                                "ON chunk_vectors USING hnsw (embedding vector_cosine_ops) "
                                "WITH (m=16, ef_construction=64)"
                            )
                        )
                except Exception:  # noqa: BLE001 - ANN index is optional
                    logger.warning("could not create pgvector HNSW index (continuing)", exc_info=True)
            row = connection.execute(
                text(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = 'chunk_vectors'::regclass AND attname = 'embedding'"
                )
            ).first()
            if row and row[0] and f"({int(dim)})" not in row[0] and "(" in str(row[0]):
                raise VectorStoreUnavailable(
                    f"已有 chunk_vectors 维度 {row[0]} 与当前维度 {int(dim)} 不一致，请先删除该表后重建索引"
                )
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(
                f"创建 pgvector 索引表失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc
        self._table_dims.add(int(dim))

    # ---------------------------------------------------------------- protocol
    def health(self) -> tuple[bool, str]:
        try:
            self._ensure_extension()
        except VectorStoreUnavailable as exc:
            return False, exc.reason
        return True, "pgvector 后端可用"

    def invalidate(self) -> None:
        # ANN mirror is updated through upsert/delete_recording; nothing cached.
        return None

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        rows = list(records)
        if not rows:
            return
        dims = {int(row.dim or len(row.vector)) for row in rows}
        if len(dims) != 1:
            raise VectorStoreUnavailable("同一批写入的向量维度不一致，无法写入 pgvector")
        dim = dims.pop()
        if dim <= 0:
            raise VectorStoreUnavailable("向量维度非法，无法写入 pgvector")
        try:
            self._ensure_extension()
            with self.engine.begin() as connection:
                self._ensure_table(connection, dim)
                sql = text(
                    "INSERT INTO chunk_vectors "
                    "(chunk_id, recording_id, idx, start_ms, end_ms, embedding, model, dim) "
                    "VALUES (:chunk_id, :recording_id, :idx, :start_ms, :end_ms, "
                    "CAST(:embedding AS vector), :model, :dim) "
                    "ON CONFLICT (chunk_id) DO UPDATE SET "
                    "recording_id = EXCLUDED.recording_id, idx = EXCLUDED.idx, "
                    "start_ms = EXCLUDED.start_ms, end_ms = EXCLUDED.end_ms, "
                    "embedding = EXCLUDED.embedding, model = EXCLUDED.model, dim = EXCLUDED.dim"
                )
                for offset in range(0, len(rows), 100):
                    batch = rows[offset : offset + 100]
                    connection.execute(
                        sql,
                        [
                            {
                                "chunk_id": int(row.chunk_id),
                                "recording_id": int(row.recording_id),
                                "idx": int(row.idx),
                                "start_ms": int(row.start_ms),
                                "end_ms": int(row.end_ms),
                                "embedding": _vector_literal(row.vector),
                                "model": (row.model or "")[:120],
                                "dim": int(row.dim or dim),
                            }
                            for row in batch
                        ],
                    )
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(
                f"pgvector 写入失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    def sync_recording(self, db, recording_id: int) -> None:
        """Mirror one recording's chunk embeddings into ``chunk_vectors``."""
        rows = db.execute(
            select(
                Chunk.id,
                Chunk.recording_id,
                Chunk.idx,
                Chunk.start_ms,
                Chunk.end_ms,
                Chunk.embedding,
                Chunk.dim,
                Chunk.embedding_model,
            ).where(Chunk.recording_id == int(recording_id), Chunk.embedding.is_not(None))
        ).all()
        records: list[VectorRecord] = []
        for row in rows:
            try:
                array = np.frombuffer(row.embedding, dtype=np.float32)
            except (TypeError, ValueError):
                continue
            if array.size == 0:
                continue
            records.append(
                VectorRecord(
                    chunk_id=int(row.id),
                    recording_id=int(row.recording_id),
                    idx=int(row.idx or 0),
                    start_ms=int(row.start_ms or 0),
                    end_ms=int(row.end_ms or 0),
                    vector=array.tolist(),
                    model=(row.embedding_model or "")[:120],
                    dim=int(row.dim or array.size),
                )
            )
        self.upsert(records)

    def delete_recording(self, recording_id: int) -> int:
        try:
            self._ensure_extension()
            with self.engine.begin() as connection:
                result = connection.execute(
                    text("DELETE FROM chunk_vectors WHERE recording_id = :recording_id"),
                    {"recording_id": int(recording_id)},
                )
                return int(result.rowcount or 0)
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(
                f"pgvector 删除失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    def search(
        self,
        query: Sequence[float],
        *,
        top_k: int = 40,
        recording_ids: Sequence[int] | None = None,
        min_score: float = 0.0,
        model: str | None = None,
        dim: int | None = None,
    ) -> list[VectorHit]:
        top_k = max(0, int(top_k))
        query_array = _normalize(query)
        if query_array is None or top_k == 0:
            return []
        if dim is None:
            dim = int(query_array.size)
        elif int(dim) != int(query_array.size):
            return []
        if recording_ids is not None and len(recording_ids) == 0:
            return []
        # ACL filtering happens after the ANN query, so oversample 4× for members.
        limit = min(200, top_k * 4) if recording_ids is not None else top_k
        query_literal = _vector_literal(query_array)
        conditions = ["dim = :dim"]
        params: dict[str, Any] = {"q": query_literal, "dim": int(dim), "limit": int(limit)}
        if model:
            conditions.append("model = :model")
            params["model"] = model
        sql = (
            "SELECT chunk_id, recording_id, idx, start_ms, end_ms, "
            "1 - (embedding <=> CAST(:q AS vector)) AS score "
            "FROM chunk_vectors WHERE "
            + " AND ".join(conditions)
            + " ORDER BY embedding <=> CAST(:q AS vector) LIMIT :limit"
        )
        try:
            self._ensure_extension()
            with self.engine.begin() as connection:
                self._ensure_table(connection, int(dim))
                try:
                    connection.execute(text("SET LOCAL hnsw.ef_search = 100"))
                except Exception:  # noqa: BLE001 - older pgvector / no index yet
                    logger.debug("SET LOCAL hnsw.ef_search failed", exc_info=True)
                rows = connection.execute(text(sql), params).all()
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(
                f"pgvector 检索失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc
        allowed = None
        if recording_ids is not None:
            allowed = {int(value) for value in recording_ids}
        hits: list[VectorHit] = []
        for row in rows:
            recording_id = int(row.recording_id)
            if allowed is not None and recording_id not in allowed:
                continue
            score = float(row.score or 0.0)
            if score < min_score:
                continue
            hits.append(
                VectorHit(
                    chunk_id=int(row.chunk_id),
                    recording_id=recording_id,
                    score=score,
                    idx=int(row.idx or 0),
                    start_ms=int(row.start_ms or 0),
                    end_ms=int(row.end_ms or 0),
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    def stats(self, *, model: str | None = None, dim: int | None = None) -> VectorStoreStats:
        try:
            self._ensure_extension()
            with self.engine.begin() as connection:
                where = []
                params: dict[str, Any] = {}
                if model:
                    where.append("model = :model")
                    params["model"] = model
                if dim:
                    where.append("dim = :dim")
                    params["dim"] = int(dim)
                clause = f" WHERE {' AND '.join(where)}" if where else ""
                row = connection.execute(
                    text(f"SELECT count(*), count(embedding) FROM chunk_vectors{clause}"),
                    params,
                ).one()
            indexed = int(row[0] or 0)
            return VectorStoreStats(
                backend=self.backend,
                available=True,
                chunks_total=indexed,
                chunks_indexed=indexed,
                stale_chunks=0,
                dim=dim,
                model=model,
            )
        except VectorStoreUnavailable as exc:
            return VectorStoreStats(backend=self.backend, available=False, fallback_reason=exc.reason)
        except Exception as exc:  # noqa: BLE001
            return VectorStoreStats(
                backend=self.backend,
                available=False,
                fallback_reason=f"pgvector 统计失败：{type(exc).__name__}: {str(exc)[:160]}",
            )


# ------------------------------------------------------------------ sqlite-vec store
#: virtual table name prefix; one vec0 table per embedding dimension
SQLITE_VEC_TABLE_PREFIX = "vec_chunks_"
#: oversampling factor when ACL/model filtering has to happen in Python
SQLITE_VEC_OVERSAMPLE = 8
#: member ACL ids are pushed into SQL when the IN-list stays this small
SQLITE_VEC_MAX_SQL_IDS = 500
#: sqlite-vec KNN ``k`` upper bound (keeps oversampling bounded)
SQLITE_VEC_MAX_K = 1000


def _import_sqlite_vec():
    """Import the optional ``sqlite-vec`` Python wheel or raise a friendly error."""
    try:
        import sqlite_vec
    except Exception as exc:  # noqa: BLE001 - optional dependency
        raise VectorStoreUnavailable(
            "未安装 sqlite-vec Python 包（pip install \"sqlite-vec\"），无法使用该向量后端"
        ) from exc
    return sqlite_vec


def _sqlite_url_path(url: str) -> str:
    """Extract the file path from a SQLAlchemy SQLite URL."""
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(url)
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreUnavailable(f"无法解析 SQLite 数据库地址：{exc}") from exc
    if parsed.get_backend_name() != "sqlite":
        raise VectorStoreUnavailable("当前数据库不是 SQLite，sqlite-vec 后端不可用")
    database = parsed.database or ""
    if not database or database == ":memory:":
        raise VectorStoreUnavailable("sqlite-vec 需要基于文件的 SQLite 数据库（不支持 :memory:）")
    return database


class SqliteVecVectorStore:
    """sqlite-vec ANN backend for the single-container SQLite deployment (R-20 Phase 2).

    The ``vec0`` virtual tables live in the same SQLite file as the application
    data and are accessed through a dedicated ``sqlite3`` connection (loading the
    bundled extension with ``sqlite_vec.load()``).  One table is kept per
    embedding dimension (``vec_chunks_<dim>``) so a model/dimension change never
    mixes incompatible vectors: reindexing a recording rewrites its rows into the
    new table and drops the old ones.

    Every failure raises :class:`VectorStoreUnavailable` with a friendly Chinese
    reason; :func:`get_vector_store` then degrades to
    :class:`NumpyVectorStore`.  ``chunks.embedding`` BLOBs stay the portable
    source of truth and can always rebuild this mirror (no re-embedding calls).
    """

    backend = "sqlite_vec"

    def __init__(self, db_url: str | None = None) -> None:
        self._db_url = db_url or settings.resolved_database_url
        self._lock = threading.RLock()
        self._connection: Any = None
        self._vec_module: Any = None
        self._version: str | None = None
        self._known_dims: set[int] = set()
        self._load_error: str | None = None
        self.fallback_reason: str | None = None

    # ---------------------------------------------------------------- connection
    @property
    def db_path(self) -> str:
        return _sqlite_url_path(self._db_url)

    def _connect(self):
        """Open (once, thread-safe) the dedicated extension-enabled connection."""
        with self._lock:
            if self._connection is not None:
                return self._connection
            if self._load_error:
                raise VectorStoreUnavailable(self._load_error)
            module = _import_sqlite_vec()
            path = self.db_path
            connection = None
            try:
                import sqlite3

                if not hasattr(sqlite3.Connection, "enable_load_extension"):
                    raise VectorStoreUnavailable(
                        "当前 Python 的 sqlite3 未启用扩展加载（enable_load_extension 不可用），"
                        "sqlite-vec 后端不可用"
                    )
                connection = sqlite3.connect(
                    path, timeout=30.0, check_same_thread=False, isolation_level=None
                )
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.enable_load_extension(True)
                module.load(connection)
                connection.enable_load_extension(False)
                row = connection.execute("SELECT vec_version()").fetchone()
                self._version = str(row[0]) if row else "unknown"
            except VectorStoreUnavailable:
                if connection is not None:
                    connection.close()
                raise
            except Exception as exc:  # noqa: BLE001 - missing wheel / extension build
                if connection is not None:
                    connection.close()
                reason = f"加载 sqlite-vec 扩展失败：{type(exc).__name__}: {str(exc)[:180]}"
                self._load_error = reason
                raise VectorStoreUnavailable(reason) from exc
            self._connection = connection
            self._vec_module = module
            return connection

    def _blob(self, vector: Sequence[float]) -> bytes:
        module = self._vec_module
        if module is None:
            module = _import_sqlite_vec()
            self._vec_module = module
        return module.serialize_float32([float(value) for value in vector])

    # ---------------------------------------------------------------- schema
    def _fetch_known_dims(self, connection) -> set[int]:
        dims: set[int] = set()
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?",
                (f"{SQLITE_VEC_TABLE_PREFIX}%",),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001 - sqlite3.Error subclasses
            raise VectorStoreUnavailable(f"读取 sqlite-vec 索引表失败：{exc}") from exc
        for (name,) in rows:
            suffix = str(name)[len(SQLITE_VEC_TABLE_PREFIX) :]
            try:
                dims.add(int(suffix))
            except ValueError:
                continue
        return dims

    @staticmethod
    def _table_name(dim: int) -> str:
        return f"{SQLITE_VEC_TABLE_PREFIX}{int(dim)}"

    @staticmethod
    def _table_exists(connection, name: str) -> bool:
        try:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
        except Exception:  # noqa: BLE001
            return False
        return row is not None

    def _ensure_table(self, connection, dim: int) -> None:
        if int(dim) in self._known_dims:
            return
        if int(dim) <= 0:
            raise VectorStoreUnavailable("向量维度非法，无法写入 sqlite-vec")
        name = self._table_name(dim)
        ddl = (
            f'CREATE VIRTUAL TABLE IF NOT EXISTS "{name}" USING vec0('
            f"chunk_id INTEGER PRIMARY KEY, "
            f"embedding float[{int(dim)}] distance_metric=cosine, "
            f"recording_id INTEGER, model TEXT, "
            f"+idx INTEGER, +start_ms INTEGER, +end_ms INTEGER)"
        )
        try:
            connection.execute(ddl)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(
                f"创建 sqlite-vec 索引表失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc
        self._known_dims.add(int(dim))

    # ---------------------------------------------------------------- protocol
    def health(self) -> tuple[bool, str]:
        try:
            with self._lock:
                connection = self._connect()
                self._known_dims = self._fetch_known_dims(connection)
        except VectorStoreUnavailable as exc:
            self._load_error = self._load_error or exc.reason
            return False, exc.reason
        return True, f"sqlite-vec {self._version} 可用（SQLite 单进程）"

    def invalidate(self) -> None:
        # The mirror is updated synchronously through sync_recording/upsert.
        return None

    def _replace_recording(self, recording_id: int, records: Sequence[VectorRecord]) -> int:
        """Atomically replace one recording's rows across every dimension table."""
        connection = self._connect()
        by_dim: dict[int, list[VectorRecord]] = {}
        for record in records:
            dim = int(record.dim or len(record.vector))
            if dim <= 0:
                continue
            by_dim.setdefault(dim, []).append(record)
        with self._lock:
            self._known_dims = self._fetch_known_dims(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreUnavailable(f"sqlite-vec 写入事务失败：{exc}") from exc
            try:
                for dim in by_dim:
                    self._ensure_table(connection, dim)
                for dim in sorted(self._known_dims):
                    table = self._table_name(dim)
                    connection.execute(
                        f'DELETE FROM "{table}" WHERE recording_id = ?', (int(recording_id),)
                    )
                written = 0
                for dim, batch in by_dim.items():
                    table = self._table_name(dim)
                    connection.executemany(
                        f'INSERT INTO "{table}" (chunk_id, embedding, recording_id, model, '
                        f"idx, start_ms, end_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                int(row.chunk_id),
                                self._blob(row.vector),
                                int(row.recording_id),
                                (row.model or "")[:120],
                                int(row.idx),
                                int(row.start_ms),
                                int(row.end_ms),
                            )
                            for row in batch
                        ],
                    )
                    written += len(batch)
                connection.execute("COMMIT")
                return written
            except Exception as exc:  # noqa: BLE001
                try:
                    connection.execute("ROLLBACK")
                except Exception:  # noqa: BLE001 - best effort
                    pass
                if isinstance(exc, VectorStoreUnavailable):
                    raise
                raise VectorStoreUnavailable(
                    f"写入 sqlite-vec 索引失败：{type(exc).__name__}: {str(exc)[:180]}"
                ) from exc

    def sync_recording(self, db, recording_id: int) -> None:
        """Mirror one recording's chunk embeddings into the sqlite-vec tables."""
        rows = db.execute(
            select(
                Chunk.id,
                Chunk.recording_id,
                Chunk.idx,
                Chunk.start_ms,
                Chunk.end_ms,
                Chunk.embedding,
                Chunk.dim,
                Chunk.embedding_model,
            ).where(Chunk.recording_id == int(recording_id), Chunk.embedding.is_not(None))
        ).all()
        records: list[VectorRecord] = []
        for row in rows:
            try:
                array = np.frombuffer(row.embedding, dtype=np.float32)
            except (TypeError, ValueError):
                continue
            if array.size == 0:
                continue
            records.append(
                VectorRecord(
                    chunk_id=int(row.id),
                    recording_id=int(row.recording_id),
                    idx=int(row.idx or 0),
                    start_ms=int(row.start_ms or 0),
                    end_ms=int(row.end_ms or 0),
                    vector=array.tolist(),
                    model=(row.embedding_model or "")[:120],
                    dim=int(row.dim or array.size),
                )
            )
        self._replace_recording(int(recording_id), records)

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        rows = list(records)
        if not rows:
            return
        connection = self._connect()
        by_dim: dict[int, list[VectorRecord]] = {}
        for record in rows:
            dim = int(record.dim or len(record.vector))
            if dim <= 0:
                continue
            by_dim.setdefault(dim, []).append(record)
        with self._lock:
            self._known_dims = self._fetch_known_dims(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreUnavailable(f"sqlite-vec 写入事务失败：{exc}") from exc
            try:
                for dim in by_dim:
                    self._ensure_table(connection, dim)
                for dim, batch in by_dim.items():
                    table = self._table_name(dim)
                    for row in batch:
                        connection.execute(
                            f'DELETE FROM "{table}" WHERE chunk_id = ?', (int(row.chunk_id),)
                        )
                    connection.executemany(
                        f'INSERT INTO "{table}" (chunk_id, embedding, recording_id, model, '
                        f"idx, start_ms, end_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                int(row.chunk_id),
                                self._blob(row.vector),
                                int(row.recording_id),
                                (row.model or "")[:120],
                                int(row.idx),
                                int(row.start_ms),
                                int(row.end_ms),
                            )
                            for row in batch
                        ],
                    )
                connection.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001
                try:
                    connection.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                if isinstance(exc, VectorStoreUnavailable):
                    raise
                raise VectorStoreUnavailable(
                    f"写入 sqlite-vec 索引失败：{type(exc).__name__}: {str(exc)[:180]}"
                ) from exc

    def delete_recording(self, recording_id: int) -> int:
        connection = self._connect()
        with self._lock:
            self._known_dims = self._fetch_known_dims(connection)
            deleted = 0
            try:
                connection.execute("BEGIN IMMEDIATE")
                for dim in sorted(self._known_dims):
                    table = self._table_name(dim)
                    cursor = connection.execute(
                        f'DELETE FROM "{table}" WHERE recording_id = ?', (int(recording_id),)
                    )
                    if cursor.rowcount and cursor.rowcount > 0:
                        deleted += int(cursor.rowcount)
                connection.execute("COMMIT")
                return deleted
            except Exception as exc:  # noqa: BLE001
                try:
                    connection.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                if isinstance(exc, VectorStoreUnavailable):
                    raise
                raise VectorStoreUnavailable(
                    f"删除 sqlite-vec 索引失败：{type(exc).__name__}: {str(exc)[:180]}"
                ) from exc

    @staticmethod
    def _alive_recording_ids(connection, ids: set[int]) -> set[int] | None:
        """Return the subset of ``ids`` that still exist and are not trashed."""
        if not ids:
            return set()
        alive: set[int] = set()
        values = sorted(ids)
        try:
            for offset in range(0, len(values), 500):
                chunk = values[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT id FROM recordings WHERE deleted_at IS NULL AND id IN ({placeholders})",
                    chunk,
                ).fetchall()
                alive.update(int(row[0]) for row in rows)
        except Exception:  # noqa: BLE001 - schema not ready: do not filter
            return None
        return alive

    def search(
        self,
        query: Sequence[float],
        *,
        top_k: int = 40,
        recording_ids: Sequence[int] | None = None,
        min_score: float = 0.0,
        model: str | None = None,
        dim: int | None = None,
    ) -> list[VectorHit]:
        top_k = max(0, int(top_k))
        if top_k == 0:
            return []
        query_array = _normalize(query)
        if query_array is None:
            return []
        if dim is None:
            dim = int(query_array.size)
        elif int(dim) != int(query_array.size):
            return []
        if recording_ids is not None and len(recording_ids) == 0:
            return []
        filter_ids: set[int] | None = None
        if recording_ids is not None:
            filter_ids = {int(value) for value in recording_ids}
        oversample = bool(model) or (
            filter_ids is not None and len(filter_ids) > SQLITE_VEC_MAX_SQL_IDS
        )
        k = min(SQLITE_VEC_MAX_K, max(top_k * SQLITE_VEC_OVERSAMPLE, 64)) if oversample else top_k
        params: list[Any] = [self._blob(query_array), int(k)]
        sql = (
            f'SELECT chunk_id, recording_id, idx, start_ms, end_ms, model, distance '
            f'FROM "{self._table_name(int(dim))}" '
            f"WHERE embedding MATCH ? AND k = ?"
        )
        try:
            connection = self._connect()
            with self._lock:
                self._known_dims = self._fetch_known_dims(connection)
                if int(dim) not in self._known_dims:
                    return []
                if filter_ids is not None and len(filter_ids) <= SQLITE_VEC_MAX_SQL_IDS:
                    placeholders = ",".join("?" for _ in filter_ids)
                    sql += f" AND recording_id IN ({placeholders})"
                    params.extend(sorted(filter_ids))
                sql += " ORDER BY distance"
                rows = connection.execute(sql, params).fetchall()
                alive = self._alive_recording_ids(
                    connection, {int(row[1]) for row in rows}
                )
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(
                f"sqlite-vec 检索失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc
        hits: list[VectorHit] = []
        for row in rows:
            recording_id = int(row[1])
            if filter_ids is not None and recording_id not in filter_ids:
                continue
            if alive is not None and recording_id not in alive:
                continue
            stored_model = (row[5] or "").strip()
            if model and stored_model and stored_model != model:
                continue
            try:
                distance = float(row[6])
            except (TypeError, ValueError):
                continue
            score = max(-1.0, min(1.0, 1.0 - distance))
            if score < min_score:
                continue
            hits.append(
                VectorHit(
                    chunk_id=int(row[0]),
                    recording_id=recording_id,
                    score=score,
                    idx=int(row[2] or 0),
                    start_ms=int(row[3] or 0),
                    end_ms=int(row[4] or 0),
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    def stats(self, *, model: str | None = None, dim: int | None = None) -> VectorStoreStats:
        try:
            connection = self._connect()
            with self._lock:
                self._known_dims = self._fetch_known_dims(connection)
                dims = [int(dim)] if dim else sorted(self._known_dims)
                total = 0
                for current_dim in dims:
                    table = self._table_name(current_dim)
                    if not self._table_exists(connection, table):
                        continue
                    row = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()
                    total += int(row[0] or 0) if row else 0
            return VectorStoreStats(
                backend=self.backend,
                available=True,
                chunks_total=total,
                chunks_indexed=total,
                stale_chunks=0,
                dim=dim,
                model=model,
                fallback_reason=self.fallback_reason,
            )
        except VectorStoreUnavailable as exc:
            return VectorStoreStats(
                backend=self.backend, available=False, fallback_reason=exc.reason
            )
        except Exception as exc:  # noqa: BLE001
            return VectorStoreStats(
                backend=self.backend,
                available=False,
                fallback_reason=f"sqlite-vec 统计失败：{type(exc).__name__}: {str(exc)[:160]}",
            )


# ------------------------------------------------------------------ selection
_STORES: dict[tuple[str, str], Any] = {}
_STORE_LOCK = threading.Lock()


def sqlite_vec_available() -> tuple[bool, str]:
    """Probe whether the optional sqlite-vec backend can load in this process."""
    store = SqliteVecVectorStore()
    return store.health()


def get_vector_store():
    """Return the configured vector store, degrading to numpy when needed.

    ``ECHOIC_VECTOR_BACKEND`` semantics:

    * ``auto`` (default) — pgvector on PostgreSQL when the extension is healthy,
      sqlite-vec on SQLite when the bundled extension loads, numpy otherwise;
    * ``numpy`` — force the in-memory BLOB index;
    * ``sqlite_vec`` — sqlite-vec on a file-backed SQLite database (falls back to
      numpy with a friendly reason when the wheel/extension cannot load);
    * ``pgvector`` — experimental PostgreSQL backend (same fallback rules).
    """
    requested = str(getattr(settings, "vector_backend", "auto") or "auto").strip().lower()
    from ..db import engine as db_engine

    dialect = db_engine.dialect.name
    cache_key = (requested, dialect)
    with _STORE_LOCK:
        existing = _STORES.get(cache_key)
        if existing is not None:
            return existing
        if requested == "pgvector" or (requested in ("auto", "") and dialect == "postgresql"):
            pg_store = PgVectorStore(db_engine)
            available, reason = pg_store.health()
            if available:
                store: Any = pg_store
            else:
                fallback = NumpyVectorStore()
                fallback.fallback_reason = f"pgvector 后端不可用，已降级为 numpy：{reason}"
                store = fallback
        elif requested == "sqlite_vec" or (requested in ("auto", "") and dialect == "sqlite"):
            vec_store = SqliteVecVectorStore()
            available, reason = vec_store.health()
            if available:
                store = vec_store
            else:
                fallback = NumpyVectorStore()
                fallback.fallback_reason = f"sqlite-vec 后端不可用，已降级为 numpy：{reason}"
                store = fallback
        elif requested == "numpy":
            store = NumpyVectorStore()
        else:
            fallback = NumpyVectorStore()
            fallback.fallback_reason = f"未知的向量后端 {requested}，已降级为 numpy"
            store = fallback
        _STORES[cache_key] = store
        return store


def reset_vector_store_cache() -> None:
    """Drop cached backend instances (tests / settings changes)."""
    with _STORE_LOCK:
        _STORES.clear()
    invalidate_vector_cache()


def index_recording_vectors(db, recording_id: int, *, model: str | None = None) -> int:
    """Integration hook for the pipeline after it commits chunk embeddings.

    Stamps ``chunks.embedding_model`` for the recording's embedded chunks that do
    not carry a model yet (the v0.2 pipeline wrote BLOB+dim only), then refreshes
    the active backend (numpy cache invalidation / pgvector / sqlite-vec mirror
    upsert) so semantic search picks the fresh vectors up without a restart.
    Never raises for an ANN mirror sync failure: the BLOB index remains the
    source of truth.

    Returns the number of chunks whose ``embedding_model`` was stamped.
    """
    updated = 0
    if model:
        rows = db.scalars(
            select(Chunk)
            .where(
                Chunk.recording_id == int(recording_id),
                Chunk.embedding.is_not(None),
                or_(Chunk.embedding_model.is_(None), Chunk.embedding_model == ""),
            )
        ).all()
        for chunk in rows:
            chunk.embedding_model = model[:120]
            updated += 1
        if updated:
            db.commit()
    store = get_vector_store()
    store.invalidate()
    if getattr(store, "backend", "numpy") in ("pgvector", "sqlite_vec"):
        try:
            store.sync_recording(db, int(recording_id))
        except VectorStoreUnavailable as exc:
            logger.warning(
                "%s sync after pipeline embed failed for recording %s: %s",
                getattr(store, "backend", "vector"),
                recording_id,
                exc.reason,
            )
    return updated


def vector_index_chunks(db, recording_id: int, *, model: str | None = None) -> int:
    """Alias for :func:`index_recording_vectors` (pipeline integration contract)."""
    return index_recording_vectors(db, recording_id, model=model)


# ------------------------------------------------------------------ reindex job
def _sync_store_after_write(store, db, recording_id: int) -> None:
    """Mirror a reindexed recording into ANN backends; numpy only needs invalidation.

    ``pgvector`` / ``sqlite_vec`` copy the already committed ``chunks.embedding``
    BLOBs into their ANN mirror, so switching backends never re-calls embeddings.
    """
    if getattr(store, "backend", "numpy") in ("pgvector", "sqlite_vec"):
        sync = getattr(store, "sync_recording", None)
        if callable(sync):
            sync(db, recording_id)
    else:
        store.invalidate()


def _try_sync_store(store, db, recording_id: int, result: ReindexResult) -> None:
    """Mirror a recording into the ANN backend, collecting failures as errors."""
    try:
        _sync_store_after_write(store, db, recording_id)
    except Exception as exc:  # noqa: BLE001 - BLOB index remains authoritative
        reason = getattr(exc, "reason", None) or f"{type(exc).__name__}: {str(exc)[:160]}"
        logger.warning("vector mirror sync failed for recording %s: %s", recording_id, reason)
        result.errors.append(f"录音 {recording_id} 向量索引同步失败：{reason}")


def reindex_recordings(
    db,
    *,
    provider,
    recording_ids: Sequence[int] | None = None,
    force: bool = False,
    dry_run: bool = False,
    model: str | None = None,
    dim: int | None = None,
    max_recordings: int = MAX_REINDEX_RECORDINGS,
) -> ReindexResult:
    """Embed missing/stale chunks for the target recordings (synchronous).

    ``recording_ids=None`` means "all non-trashed recordings", capped to
    ``max_recordings`` (the API enforces ≤200 per request; the CLI loops).
    ``dry_run`` reports what would be embedded without calling the provider or
    writing anything.  Individual recording failures are collected in
    ``errors`` (partial success, no global rollback).
    """
    result = ReindexResult()
    model_name = _embedding_model_name(provider, model)
    expected_dim = int(dim or getattr(provider, "dim", 0) or 0) or None

    if recording_ids is not None:
        raw_ids = [int(value) for value in recording_ids]
        if len(raw_ids) > max_recordings:
            raise ValueError(f"单次最多处理 {max_recordings} 条录音，请拆分后重试")
        wanted: list[int] = []
        seen: set[int] = set()
        for recording_id in raw_ids:
            if recording_id not in seen:
                seen.add(recording_id)
                wanted.append(recording_id)
        if wanted:
            found = {
                int(row[0])
                for row in db.execute(
                    select(Recording.id).where(
                        Recording.id.in_(wanted), Recording.deleted_at.is_(None)
                    )
                ).all()
            }
            result.errors.extend(
                f"录音 {recording_id} 不存在或已删除" for recording_id in wanted if recording_id not in found
            )
            target_ids = [recording_id for recording_id in wanted if recording_id in found]
        else:
            target_ids = []
    else:
        all_ids = [
            int(value)
            for value in db.scalars(
                select(Recording.id)
                .where(Recording.deleted_at.is_(None))
                .order_by(Recording.id.asc())
            ).all()
        ]
        if len(all_ids) > max_recordings:
            result.errors.append(
                f"超过单次上限 {max_recordings} 条录音，仅处理前 {max_recordings} 条，请再次执行以继续"
            )
            all_ids = all_ids[:max_recordings]
        target_ids = all_ids

    if not target_ids:
        return result

    store = get_vector_store()
    for position, recording_id in enumerate(target_ids):
        chunks = list(
            db.scalars(
                select(Chunk)
                .where(Chunk.recording_id == int(recording_id))
                .order_by(Chunk.idx.asc(), Chunk.id.asc())
            ).all()
        )
        candidates = [
            chunk
            for chunk in chunks
            if (chunk.text or "").strip()
            and (force or _is_stale(chunk, model=model_name or None, dim=expected_dim))
        ]
        result.skipped += len(chunks) - len(candidates)
        if dry_run:
            result.processed_recordings += 1
            result.chunks += len(candidates)
            continue
        if not candidates:
            result.processed_recordings += 1
            db.commit()
            # Backend switch backfill: mirror already-embedded BLOBs without
            # re-calling the embedding provider (numpy path is a cheap no-op).
            _try_sync_store(store, db, recording_id, result)
            continue
        try:
            texts = [(chunk.text or "").strip() for chunk in candidates]
            vectors = _embed_texts(provider, texts)
            if len(vectors) != len(candidates):
                raise RuntimeError(
                    f"Embedding 返回数量不匹配（期望 {len(candidates)}，实际 {len(vectors)}）"
                )
            for chunk, vector in zip(candidates, vectors):
                normalized = _normalize(vector)
                if normalized is None:
                    raise RuntimeError("Embedding 服务返回了空向量")
                chunk.embedding = normalized.astype(np.float32, copy=False).tobytes()
                chunk.dim = int(normalized.size)
                chunk.embedding_model = model_name or None
            db.commit()
            _try_sync_store(store, db, recording_id, result)
            result.processed_recordings += 1
            result.chunks += len(candidates)
        except Exception as exc:  # noqa: BLE001 - keep going with remaining recordings
            db.rollback()
            logger.warning("reindex recording %s failed: %s", recording_id, exc)
            result.errors.append(f"录音 {recording_id}：{type(exc).__name__}: {str(exc)[:180]}")
        if position + 1 == len(target_ids) or (position + 1) % 25 == 0:
            logger.info(
                "vector reindex progress %s/%s recordings, %s chunks",
                position + 1,
                len(target_ids),
                result.chunks,
            )
    if not dry_run:
        store.invalidate()
    return result


def _embed_texts(provider, texts: Sequence[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for offset in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = [text or " " for text in texts[offset : offset + EMBED_BATCH_SIZE]]
        part = run_coroutine_sync(provider.embed(batch))
        if not isinstance(part, list):
            raise RuntimeError("Embedding 服务返回值格式错误")
        vectors.extend(part)
    if len(vectors) != len(texts):
        raise RuntimeError(f"Embedding 返回数量不匹配（期望 {len(texts)}，实际 {len(vectors)}）")
    return vectors
