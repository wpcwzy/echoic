"""R-20 vector store tests: numpy search/cache/stats + pgvector graceful fallback."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import select

from app.config import settings
from app.core.utils import utcnow
from app.db import SessionLocal, engine
from app.models import Chunk
from app.services.vector_store import (
    NumpyVectorStore,
    PgVectorStore,
    VectorStoreUnavailable,
    chunk_index_counts,
    get_vector_store,
    invalidate_vector_cache,
    reset_vector_store_cache,
)
from tests.test_vector_helpers import (
    EMBED_MODEL,
    vdb,
    add_transcript,
    configure_embeddings,
    create_member,
    make_chunk,
    make_recording,
    vector_bytes,
)


@pytest.fixture(autouse=True)
def _clean_vector_state():
    reset_vector_store_cache()
    yield
    reset_vector_store_cache()


def _add_vector_chunk(
    db,
    recording,
    *,
    idx: int,
    text: str,
    vector,
    start_ms: int = 0,
    end_ms: int = 1000,
    model: str | None = EMBED_MODEL,
) -> Chunk:
    array = np.asarray(vector, dtype=np.float32)
    return make_chunk(
        db,
        recording,
        idx=idx,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        embedding=array.tobytes(),
        dim=int(array.size),
        model=model,
    )


def _store() -> NumpyVectorStore:
    return NumpyVectorStore(SessionLocal)


def test_numpy_store_ordering_top_k_and_min_score(vdb):
    recording = make_recording(vdb, title="向量排序")
    first = _add_vector_chunk(vdb, recording, idx=0, text="完全相关", vector=[1.0, 0.0, 0.0])
    second = _add_vector_chunk(vdb, recording, idx=1, text="部分相关", vector=[0.8, 0.6, 0.0])
    _add_vector_chunk(vdb, recording, idx=2, text="不相关", vector=[0.0, 1.0, 0.0])

    hits = _store().search([1.0, 0.0, 0.0], top_k=2, dim=3, model=EMBED_MODEL)
    assert [hit.chunk_id for hit in hits] == [first.id, second.id]
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)
    assert hits[1].score == pytest.approx(0.8, abs=1e-6)

    top_one = _store().search([1.0, 0.0, 0.0], top_k=1, dim=3, model=EMBED_MODEL)
    assert [hit.chunk_id for hit in top_one] == [first.id]

    threshold = _store().search(
        [1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL, min_score=0.9
    )
    assert [hit.chunk_id for hit in threshold] == [first.id]

    assert (
        _store().search(
            [0.0, 0.0, 1.0], top_k=10, dim=3, model=EMBED_MODEL, min_score=0.1
        )
        == []
    )


def test_numpy_store_scope_acl_and_deleted(vdb):
    admin_recording = make_recording(vdb, title="管理员录音")
    member = create_member(vdb, "member_a")
    member_recording = make_recording(vdb, title="成员录音", owner_id=member.id)
    trashed = make_recording(vdb, title="已删除录音", deleted_at=utcnow())

    admin_chunk = _add_vector_chunk(vdb, admin_recording, idx=0, text="公共内容", vector=[1.0, 0.0])
    member_chunk = _add_vector_chunk(vdb, member_recording, idx=0, text="成员内容", vector=[1.0, 0.0])
    trashed_chunk = _add_vector_chunk(vdb, trashed, idx=0, text="回收站内容", vector=[1.0, 0.0])

    store = _store()
    admin_hits = store.search([1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL)
    admin_ids = {hit.chunk_id for hit in admin_hits}
    assert admin_chunk.id in admin_ids
    assert member_chunk.id in admin_ids
    assert trashed_chunk.id not in admin_ids  # soft-deleted recordings never indexed

    member_hits = store.search(
        [1.0, 0.0], top_k=10, recording_ids=[member_recording.id], dim=2, model=EMBED_MODEL
    )
    assert {hit.chunk_id for hit in member_hits} == {member_chunk.id}

    assert store.search([1.0, 0.0], top_k=10, recording_ids=[], dim=2, model=EMBED_MODEL) == []


def test_numpy_store_excludes_wrong_dim_and_zero_vectors(vdb):
    recording = make_recording(vdb, title="维度过滤")
    good = _add_vector_chunk(vdb, recording, idx=0, text="同维度", vector=[1.0, 0.0, 0.0])
    _add_vector_chunk(vdb, recording, idx=1, text="不同维度", vector=[1.0, 0.0])
    _add_vector_chunk(vdb, recording, idx=2, text="零向量", vector=[0.0, 0.0, 0.0])

    hits = _store().search([1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL)
    assert [hit.chunk_id for hit in hits] == [good.id]


def test_numpy_store_cache_invalidates_on_writes(vdb):
    recording = make_recording(vdb, title="缓存失效")
    first_chunk = _add_vector_chunk(vdb, recording, idx=0, text="第一条", vector=[1.0, 0.0])
    store = _store()
    assert len(store.search([1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL)) == 1

    # new row → DB signature (count/max id) changes → snapshot reloads
    second_chunk = _add_vector_chunk(vdb, recording, idx=1, text="第二条", vector=[1.0, 0.0])
    hits = store.search([1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL)
    assert {hit.chunk_id for hit in hits} == {first_chunk.id, second_chunk.id}

    # same row count/max id but rewritten embedding → needs explicit invalidation
    first_chunk = vdb.get(Chunk, first_chunk.id)
    first_chunk.embedding = np.asarray([0.0, 1.0], dtype=np.float32).tobytes()
    vdb.commit()
    cached = {
        hit.chunk_id: hit.score
        for hit in store.search([1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL)
    }
    assert cached[first_chunk.id] == pytest.approx(1.0, abs=1e-6)  # stale snapshot

    invalidate_vector_cache()
    refreshed = {
        hit.chunk_id: hit.score
        for hit in store.search([1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL)
    }
    assert refreshed[first_chunk.id] == pytest.approx(0.0, abs=1e-6)


def test_numpy_store_legacy_unlabeled_vectors_are_searchable(vdb):
    recording = make_recording(vdb, title="旧向量")
    array = np.asarray([1.0, 0.0], dtype=np.float32)
    make_chunk(
        vdb,
        recording,
        idx=0,
        start_ms=0,
        end_ms=1000,
        text="pipeline 写入但未标记模型",
        embedding=array.tobytes(),
        dim=2,
        model=None,
    )
    hits = _store().search([1.0, 0.0], top_k=5, dim=2, model=EMBED_MODEL)
    assert len(hits) == 1


def test_chunk_index_counts_and_stale_detection(vdb):
    recording = make_recording(vdb, title="统计")
    add_transcript(vdb, recording)
    make_chunk(
        vdb, recording, idx=0, start_ms=0, end_ms=1000,
        text="已索引", embedding=vector_bytes("已索引")[0], dim=256, model=EMBED_MODEL,
    )
    make_chunk(
        vdb, recording, idx=1, start_ms=0, end_ms=1000,
        text="模型过期", embedding=vector_bytes("模型过期")[0], dim=256, model="old-model",
    )
    make_chunk(
        vdb, recording, idx=2, start_ms=0, end_ms=1000,
        text="维度过期", embedding=np.ones(8, dtype=np.float32).tobytes(), dim=8, model=EMBED_MODEL,
    )
    make_chunk(vdb, recording, idx=3, start_ms=0, end_ms=1000, text="没有向量")

    counts = chunk_index_counts(vdb, model=EMBED_MODEL, dim=256)
    assert counts["total"] == 4
    assert counts["indexed"] == 3
    assert counts["stale"] == 2  # old-model + wrong dim

    assert chunk_index_counts(vdb)["stale"] == 0  # no expectations → no stale


def test_get_vector_store_backend_selection_and_fallback(monkeypatch):
    monkeypatch.setattr(settings, "vector_backend", "numpy")
    store = get_vector_store()
    assert store.backend == "numpy"
    assert getattr(store, "fallback_reason", None) is None

    reset_vector_store_cache()
    monkeypatch.setattr(settings, "vector_backend", "auto")
    auto_store = get_vector_store()
    if auto_store.backend == "sqlite_vec":
        assert getattr(auto_store, "fallback_reason", None) is None
    else:
        # sqlite-vec wheel/extension unavailable → graceful numpy fallback
        assert auto_store.backend == "numpy"
        assert "sqlite-vec" in (auto_store.fallback_reason or "")

    reset_vector_store_cache()
    monkeypatch.setattr(settings, "vector_backend", "pgvector")
    fallback = get_vector_store()
    assert fallback.backend == "numpy"
    assert "pgvector" in (fallback.fallback_reason or "")

    reset_vector_store_cache()
    monkeypatch.setattr(settings, "vector_backend", "qdrant")
    qdrant_fallback = get_vector_store()
    assert qdrant_fallback.backend == "numpy"
    assert "Qdrant" in (qdrant_fallback.fallback_reason or "")
    assert "已降级为 numpy" in (qdrant_fallback.fallback_reason or "")


def test_pgvector_degrades_gracefully_without_postgres():
    store = PgVectorStore(engine)
    healthy, reason = store.health()
    assert healthy is False
    assert "PostgreSQL" in reason or "pgvector" in reason

    with pytest.raises(VectorStoreUnavailable) as excinfo:
        store.search([1.0, 0.0], top_k=5, dim=2, model=EMBED_MODEL)
    assert "pgvector" in excinfo.value.reason or "PostgreSQL" in excinfo.value.reason

    stats = store.stats(model=EMBED_MODEL, dim=2)
    assert stats.available is False
    assert stats.fallback_reason


def test_index_recording_vectors_stamps_legacy_model(vdb):
    from app.services.vector_store import index_recording_vectors

    configure_embeddings(vdb, "mock")
    recording = make_recording(vdb, title="pipeline 集成")
    legacy = make_chunk(
        vdb,
        recording,
        idx=0,
        start_ms=0,
        end_ms=1000,
        text="pipeline 写入的向量",
        embedding=vector_bytes("pipeline 写入的向量")[0],
        dim=256,
        model=None,
    )
    updated = index_recording_vectors(vdb, recording.id, model=EMBED_MODEL)
    assert updated == 1
    refreshed = vdb.get(Chunk, legacy.id)
    vdb.refresh(refreshed)
    assert refreshed.embedding_model == EMBED_MODEL
    # idempotent: second call stamps nothing
    assert index_recording_vectors(vdb, recording.id, model=EMBED_MODEL) == 0
