"""v1.0 Qdrant vector backend tests.

Store contract tests use qdrant-client's in-process local mode
(``QdrantClient(":memory:")``) so they stay network-free; machines without the
optional ``qdrant-client`` wheel skip those tests cleanly.  Backend-selection /
graceful-degradation tests always run (they monkeypatch the client factory), and
API/CLI integration tests verify the effective backend surfaces correctly.
"""

from __future__ import annotations

import asyncio
import contextlib

import numpy as np
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Chunk, User
from app.services import vector_store as vector_store_module
from app.services.retrieval import retrieve_for_query
from app.services.vector_store import (
    NumpyVectorStore,
    QdrantVectorStore,
    VectorStoreUnavailable,
    get_vector_store,
    qdrant_available,
    reindex_recordings,
    reset_vector_store_cache,
)
from tests.test_vector_helpers import (
    EMBED_MODEL,
    configure_embeddings,
    create_admin,
    create_member,
    make_chunk,
    make_recording,
    seed_recording,
    vdb,
    vector_values,
)


# ------------------------------------------------------------------ availability
def _probe_qdrant() -> bool:
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(":memory:")
        try:
            client.get_collections()
            return True
        finally:
            with contextlib.suppress(Exception):
                client.close()
    except Exception:  # noqa: BLE001 - optional dependency / local mode unsupported
        return False


QDRANT_AVAILABLE = _probe_qdrant()
requires_qdrant = pytest.mark.skipif(
    not QDRANT_AVAILABLE,
    reason="qdrant-client 未安装或本地模式不可用，跳过 Qdrant 单元用例",
)


@pytest.fixture(autouse=True)
def _clean_vector_state():
    reset_vector_store_cache()
    yield
    reset_vector_store_cache()


def _memory_client():
    from qdrant_client import QdrantClient

    return QdrantClient(":memory:")


def _store(prefix: str = "unit_chunks") -> QdrantVectorStore:
    return QdrantVectorStore(client=_memory_client(), collection=prefix)


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
    norm = float(np.linalg.norm(array))
    if norm:
        array = array / norm
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


def _patch_factory(monkeypatch, client) -> None:
    monkeypatch.setattr(vector_store_module, "_build_qdrant_client", lambda: client)


def _configure_qdrant(monkeypatch, *, url: str = "http://qdrant.invalid:6333") -> None:
    monkeypatch.setattr(settings, "qdrant_url", url)
    monkeypatch.setattr(settings, "qdrant_collection", "echoic_chunks")


# ------------------------------------------------------------------ store contract
@requires_qdrant
def test_health_backend_and_collection_naming():
    store = _store("echoic_chunks")
    assert store.backend == "qdrant"
    healthy, message = store.health()
    assert healthy is True, message
    assert "Qdrant" in message
    assert store.fallback_reason is None
    assert store.collection_name(256) == "echoic_chunks_256"
    assert store.collection_name(1024) == "echoic_chunks_1024"


@requires_qdrant
def test_sync_search_ordering_top_k_min_score(vdb):
    recording = make_recording(vdb, title="qdrant 排序")
    first = _add_vector_chunk(vdb, recording, idx=0, text="完全相关", vector=[1.0, 0.0, 0.0])
    second = _add_vector_chunk(vdb, recording, idx=1, text="部分相关", vector=[0.8, 0.6, 0.0])
    third = _add_vector_chunk(vdb, recording, idx=2, text="不相关", vector=[0.0, 1.0, 0.0])
    store = _store()
    store.sync_recording(vdb, recording.id)

    hits = store.search([1.0, 0.0, 0.0], top_k=2, dim=3, model=EMBED_MODEL)
    assert [hit.chunk_id for hit in hits] == [first.id, second.id]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[1].score == pytest.approx(0.8, abs=1e-5)
    assert hits[0].start_ms == 0 and hits[0].end_ms == 1000

    assert [
        hit.chunk_id
        for hit in store.search([1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL, min_score=0.9)
    ] == [first.id]
    assert store.search([0.0, 0.0, 1.0], top_k=10, dim=3, model=EMBED_MODEL, min_score=0.1) == []
    assert store.search([1.0, 0.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL) == []

    scoped = store.search(
        [1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL, recording_ids=[recording.id]
    )
    assert {hit.chunk_id for hit in scoped} == {first.id, second.id, third.id}
    assert store.search([1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL, recording_ids=[]) == []

    stats = store.stats(dim=3)
    assert stats.backend == "qdrant" and stats.available is True
    assert stats.chunks_indexed == 3


@requires_qdrant
def test_acl_scope_stale_model_and_legacy_vectors(vdb):
    owner = create_member(vdb, "qdrant_owner")
    mine = make_recording(vdb, title="成员录音", owner_id=owner.id)
    mine_chunk = _add_vector_chunk(vdb, mine, idx=0, text="成员内容", vector=[1.0, 0.0])
    other = make_recording(vdb, title="他人录音")
    _add_vector_chunk(vdb, other, idx=0, text="他人内容", vector=[1.0, 0.0])
    stale = make_recording(vdb, title="旧模型")
    stale_chunk = _add_vector_chunk(vdb, stale, idx=0, text="旧模型", vector=[1.0, 0.0], model="old-model")
    legacy = make_recording(vdb, title="无模型标记")
    legacy_chunk = _add_vector_chunk(vdb, legacy, idx=0, text="旧版", vector=[1.0, 0.0], model=None)

    store = _store()
    for recording in (mine, other, stale, legacy):
        store.sync_recording(vdb, recording.id)

    scoped = store.search(
        [1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL, recording_ids=[mine.id]
    )
    assert {hit.chunk_id for hit in scoped} == {mine_chunk.id}

    hits = store.search([1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL)
    ids = {hit.chunk_id for hit in hits}
    assert stale_chunk.id not in ids  # explicit old model is stale
    assert legacy_chunk.id in ids  # legacy NULL model treated as current (parity)


@requires_qdrant
def test_dimension_change_switches_collection(vdb):
    recording = make_recording(vdb, title="Qdrant 维度切换")
    first = _add_vector_chunk(vdb, recording, idx=0, text="三维一", vector=[1.0, 0.0, 0.0])
    second = _add_vector_chunk(vdb, recording, idx=1, text="三维二", vector=[0.0, 1.0, 0.0])
    store = _store()
    store.sync_recording(vdb, recording.id)
    assert len(store.search([1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL)) == 2

    for chunk in (first, second):
        array = np.asarray([1.0, 0.0], dtype=np.float32)
        chunk.embedding = array.tobytes()
        chunk.dim = 2
    vdb.commit()
    store.sync_recording(vdb, recording.id)

    assert store.search([1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL) == []
    dim2_hits = store.search([1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL)
    assert {hit.chunk_id for hit in dim2_hits} == {first.id, second.id}
    assert store.stats(dim=3).chunks_indexed == 0


@requires_qdrant
def test_upsert_delete_and_stats(vdb):
    recording = make_recording(vdb, title="upsert/delete")
    chunk = _add_vector_chunk(vdb, recording, idx=0, text="第一条", vector=[1.0, 0.0])
    store = _store()
    store.sync_recording(vdb, recording.id)
    assert len(store.search([1.0, 0.0], top_k=5, dim=2, model=EMBED_MODEL)) == 1

    store.upsert(
        [
            vector_store_module.VectorRecord(
                chunk_id=chunk.id,
                recording_id=recording.id,
                idx=0,
                start_ms=0,
                end_ms=1000,
                vector=[0.0, 1.0],
                model=EMBED_MODEL,
                dim=2,
            )
        ]
    )
    hits = store.search([0.0, 1.0], top_k=5, dim=2, model=EMBED_MODEL)
    assert [hit.chunk_id for hit in hits] == [chunk.id]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)

    assert store.delete_recording(recording.id) == 1
    assert store.search([1.0, 0.0], top_k=5, dim=2, model=EMBED_MODEL) == []


# ------------------------------------------------------------------ selection
@requires_qdrant
def test_get_vector_store_selects_qdrant_explicit_and_auto(monkeypatch):
    client = _memory_client()
    try:
        _patch_factory(monkeypatch, client)
        _configure_qdrant(monkeypatch)

        monkeypatch.setattr(settings, "vector_backend", "qdrant")
        store = get_vector_store()
        assert store.backend == "qdrant"
        assert getattr(store, "fallback_reason", None) is None

        reset_vector_store_cache()
        monkeypatch.setattr(settings, "vector_backend", "auto")
        auto_store = get_vector_store()
        # Qdrant has priority over sqlite-vec whenever it is configured + healthy.
        assert auto_store.backend == "qdrant"
    finally:
        reset_vector_store_cache()
        with contextlib.suppress(Exception):
            client.close()


def test_explicit_qdrant_falls_back_to_numpy(monkeypatch):
    def _boom():
        raise VectorStoreUnavailable("未安装 qdrant-client Python 包（测试模拟）")

    monkeypatch.setattr(vector_store_module, "_build_qdrant_client", _boom)
    _configure_qdrant(monkeypatch)
    monkeypatch.setattr(settings, "vector_backend", "qdrant")
    reset_vector_store_cache()

    fallback = get_vector_store()
    assert isinstance(fallback, NumpyVectorStore)
    assert fallback.backend == "numpy"
    assert "Qdrant" in (fallback.fallback_reason or "")
    assert "已降级为 numpy" in (fallback.fallback_reason or "")

    healthy, reason = qdrant_available()
    assert healthy is False
    assert "Qdrant" in reason or "qdrant" in reason


def test_auto_skips_unhealthy_qdrant(monkeypatch):
    def _boom():
        raise VectorStoreUnavailable("不可用（测试模拟）")

    monkeypatch.setattr(vector_store_module, "_build_qdrant_client", _boom)
    _configure_qdrant(monkeypatch)
    monkeypatch.setattr(settings, "vector_backend", "auto")
    reset_vector_store_cache()

    store = get_vector_store()
    # SQLite deployment: falls through Qdrant → sqlite_vec/numpy.
    assert store.backend in ("sqlite_vec", "numpy")
    assert "Qdrant" in (getattr(store, "fallback_reason", "") or "")


@requires_qdrant
def test_qdrant_config_change_is_respected_by_cache(monkeypatch):
    client = _memory_client()

    def _factory():
        return client

    monkeypatch.setattr(vector_store_module, "_build_qdrant_client", _factory)
    monkeypatch.setattr(settings, "vector_backend", "qdrant")
    monkeypatch.setattr(settings, "qdrant_collection", "echoic_chunks")
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant-a.invalid:6333")
    reset_vector_store_cache()
    first = get_vector_store()
    assert first.backend == "qdrant"

    # Same requested backend but different config → new cache entry, not stale reuse.
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant-b.invalid:6333")
    second = get_vector_store()
    assert second is not first
    assert second.backend == "qdrant"

    monkeypatch.setattr(settings, "qdrant_url", "")
    third = get_vector_store()
    assert third.backend == "numpy"
    assert "Qdrant" in (third.fallback_reason or "")

    with contextlib.suppress(Exception):
        client.close()


@requires_qdrant
def test_qdrant_store_operations_raise_friendly_unavailable(monkeypatch):
    class _BrokenClient:
        def get_collections(self):
            raise RuntimeError("connection refused")

        def collection_exists(self, _name):
            raise RuntimeError("connection refused")

    store = QdrantVectorStore(client=_BrokenClient(), collection="broken")
    healthy, reason = store.health()
    assert healthy is False
    assert "Qdrant" in reason

    with pytest.raises(VectorStoreUnavailable) as excinfo:
        store.search([1.0, 0.0], top_k=3, dim=2)
    assert "Qdrant" in excinfo.value.reason

    stats = store.stats(dim=2)
    assert stats.available is False


# ------------------------------------------------------------------ reindex / retrieval
@requires_qdrant
def test_reindex_backfills_mirror_without_reembedding(vdb, monkeypatch):
    client = _memory_client()
    try:
        _patch_factory(monkeypatch, client)
        _configure_qdrant(monkeypatch)
        monkeypatch.setattr(settings, "vector_backend", "qdrant")
        reset_vector_store_cache()
        configure_embeddings(vdb, "mock")
        recording = seed_recording(vdb, title="已嵌入未镜像 qdrant", embed=True)

        class NoEmbedProvider:
            model = EMBED_MODEL
            dim = 256

            async def embed(self, texts):  # pragma: no cover - must not be called
                raise AssertionError("reindex 回填不应重新调用 embedding")

            async def embed_query(self, texts):  # pragma: no cover
                raise AssertionError("reindex 回填不应调用 query embedding")

        result = reindex_recordings(
            vdb, provider=NoEmbedProvider(), recording_ids=[recording.id]
        )
        assert result.chunks == 0 and result.skipped == 3 and result.errors == []

        store = get_vector_store()
        assert store.backend == "qdrant"
        query = np.asarray(vector_values("两周摘要"), dtype=np.float32)
        hits = store.search(
            query,
            top_k=3,
            dim=256,
            model=EMBED_MODEL,
            min_score=-1.0,
            recording_ids=[recording.id],
        )
        assert len(hits) == 3
    finally:
        reset_vector_store_cache()
        with contextlib.suppress(Exception):
            client.close()


@requires_qdrant
def test_retrieval_parity_numpy_vs_qdrant(vdb, monkeypatch):
    client = _memory_client()
    try:
        _patch_factory(monkeypatch, client)
        _configure_qdrant(monkeypatch)
        configure_embeddings(vdb, "mock")
        admin = create_admin(vdb)
        recording = seed_recording(vdb, title="qdrant parity", owner_id=admin.id)

        monkeypatch.setattr(settings, "vector_backend", "numpy")
        reset_vector_store_cache()
        numpy_result = asyncio.run(
            retrieve_for_query(vdb, admin, "两周摘要", mode="semantic", limit=5)
        )
        assert numpy_result.mode == "semantic" and numpy_result.hits

        monkeypatch.setattr(settings, "vector_backend", "qdrant")
        reset_vector_store_cache()
        store = get_vector_store()
        assert store.backend == "qdrant"
        store.sync_recording(vdb, recording.id)
        qdrant_result = asyncio.run(
            retrieve_for_query(vdb, admin, "两周摘要", mode="semantic", limit=5)
        )

        assert qdrant_result.mode == "semantic" and qdrant_result.hits
        assert qdrant_result.hits[0].segment_id == numpy_result.hits[0].segment_id
        assert qdrant_result.hits[0].score == pytest.approx(
            numpy_result.hits[0].score, abs=1e-4
        )
        numpy_chunks = {hit.chunk_id for hit in numpy_result.hits}
        qdrant_chunks = {hit.chunk_id for hit in qdrant_result.hits}
        assert qdrant_chunks == numpy_chunks
    finally:
        reset_vector_store_cache()
        with contextlib.suppress(Exception):
            client.close()


# ------------------------------------------------------------------ API / CLI
def test_api_status_reindex_search_with_qdrant(auth_client, db, monkeypatch):
    if not QDRANT_AVAILABLE:
        pytest.skip("qdrant-client 未安装，跳过 Qdrant API 集成用例")
    client = _memory_client()
    try:
        _patch_factory(monkeypatch, client)
        _configure_qdrant(monkeypatch)
        monkeypatch.setattr(settings, "vector_backend", "qdrant")
        reset_vector_store_cache()
        configure_embeddings(db, "mock")
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        recording = seed_recording(db, title="API qdrant", owner_id=admin.id, embed=False)

        status = auth_client.get("/api/vector/status")
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["backend"] == "qdrant"
        assert body["available"] is True
        assert body["embeddings_configured"] is True
        assert body["fallback_reason"] is None

        dry = auth_client.post("/api/vector/reindex", json={"dry_run": True}).json()
        assert dry["ok"] is True and dry["chunks"] == 3
        applied = auth_client.post(
            "/api/vector/reindex", json={"recording_ids": [recording.id]}
        ).json()
        assert applied["ok"] is True and applied["chunks"] == 3

        status_after = auth_client.get("/api/vector/status").json()
        assert status_after["chunks_indexed"] == 3
        store = get_vector_store()
        assert store.backend == "qdrant"
        assert store.stats(dim=256).chunks_indexed == 3

        search = auth_client.get(
            "/api/vector/search", params={"q": "两周摘要", "mode": "semantic", "limit": 5}
        ).json()
        assert search["mode"] == "semantic" and search["results"]
        assert "两周" in search["results"][0]["text"]

        features = auth_client.get("/api/system/features")
        assert features.status_code == 200, features.text
        assert features.json()["vector"]["backend"] == "qdrant"
    finally:
        reset_vector_store_cache()
        with contextlib.suppress(Exception):
            client.close()


def test_cli_prints_qdrant_backend(vdb, monkeypatch, capsys):
    if not QDRANT_AVAILABLE:
        pytest.skip("qdrant-client 未安装，跳过 Qdrant CLI 集成用例")
    from app.tools.vector_reindex import main

    client = _memory_client()
    try:
        _patch_factory(monkeypatch, client)
        _configure_qdrant(monkeypatch)
        monkeypatch.setattr(settings, "vector_backend", "qdrant")
        reset_vector_store_cache()
        configure_embeddings(vdb, "mock")
        seed_recording(vdb, title="CLI qdrant", embed=False)
        assert main(["--all", "--dry-run"]) == 0
        output = capsys.readouterr().out
        assert "qdrant" in output
        assert "向量后端" in output
    finally:
        reset_vector_store_cache()
        with contextlib.suppress(Exception):
            client.close()
