"""v0.3.10 R-20 Phase 2: sqlite-vec backend contract tests.

Covers the ANN store directly (search/top-k/min-score/ACL/dim+model changes/
upsert/delete/backfill), backend selection semantics, graceful fallback when the
wheel or extension cannot load, numpy↔sqlite-vec retrieval parity and the vector
API status/reindex/search flow with ``ECHOIC_VECTOR_BACKEND=sqlite_vec``.

Tests that genuinely need the bundled extension are skipped cleanly on machines
where ``sqlite_vec.load()`` fails; the fallback tests always run (they monkeypatch
the importer, so they pass with or without the wheel installed).
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3

import numpy as np
import pytest
from sqlalchemy import select

from app.config import settings
from app.core.utils import utcnow
from app.models import Chunk, User
from app.services import vector_store as vector_store_module
from app.services.retrieval import retrieve_for_query
from app.services.vector_store import (
    NumpyVectorStore,
    SqliteVecVectorStore,
    VectorStoreUnavailable,
    get_vector_store,
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
def _probe_sqlite_vec() -> bool:
    """Check the wheel + sqlite3 extension loading on a throwaway in-memory DB."""
    try:
        import sqlite_vec

        connection = sqlite3.connect(":memory:")
        try:
            try:
                connection.enable_load_extension(True)
            except (AttributeError, sqlite3.OperationalError):
                return False
            sqlite_vec.load(connection)
            row = connection.execute("SELECT vec_version()").fetchone()
            return bool(row and row[0])
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - missing wheel / unsupported build
        return False


SQLITE_VEC_AVAILABLE = _probe_sqlite_vec()
requires_sqlite_vec = pytest.mark.skipif(
    not SQLITE_VEC_AVAILABLE,
    reason="sqlite-vec Python 包或扩展加载不可用，跳过 sqlite_vec 单元用例",
)


# ------------------------------------------------------------------ fixtures
def _drop_sqlite_vec_tables() -> None:
    """Remove vec0 tables from the shared test DB (drop_all ignores virtual tables)."""
    reset_vector_store_cache()
    try:
        from sqlalchemy.engine import make_url

        path = make_url(settings.resolved_database_url).database
        if not path:
            return
        connection = sqlite3.connect(path, timeout=10)
    except Exception:  # noqa: BLE001
        return
    try:
        try:
            import sqlite_vec

            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
        except Exception:  # noqa: BLE001 - extension unavailable: no vec tables exist
            pass
        try:
            names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name LIKE 'vec_chunks_%'"
                ).fetchall()
            ]
        except Exception:  # noqa: BLE001
            names = []
        for name in names:
            suffix = name[len("vec_chunks_") :]
            if not suffix.isdigit():
                continue  # skip shadow tables: dropping the vec0 table cascades them
            with contextlib.suppress(Exception):
                connection.execute(f'DROP TABLE IF EXISTS "{name}"')
        connection.commit()
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def _clean_vector_state():
    reset_vector_store_cache()
    _drop_sqlite_vec_tables()
    yield
    reset_vector_store_cache()
    _drop_sqlite_vec_tables()


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


def _store() -> SqliteVecVectorStore:
    return SqliteVecVectorStore()


# ------------------------------------------------------------------ store contract
@requires_sqlite_vec
def test_health_backend_name_and_version():
    store = _store()
    assert store.backend == "sqlite_vec"
    healthy, message = store.health()
    assert healthy is True, message
    assert "sqlite-vec" in message
    assert store.fallback_reason is None


@requires_sqlite_vec
def test_sync_search_ordering_top_k_min_score(vdb):
    recording = make_recording(vdb, title="sqlite-vec 排序")
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

    assert [h.chunk_id for h in store.search([1.0, 0.0, 0.0], top_k=1, dim=3, model=EMBED_MODEL)] == [first.id]
    assert [
        h.chunk_id
        for h in store.search([1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL, min_score=0.9)
    ] == [first.id]
    assert store.search([0.0, 0.0, 1.0], top_k=10, dim=3, model=EMBED_MODEL, min_score=0.1) == []
    assert store.search([1.0, 0.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL) == []  # dim mismatch

    stats = store.stats(dim=3)
    assert stats.backend == "sqlite_vec" and stats.available is True
    assert stats.chunks_indexed == 3

    # ACL: recording scope filter
    scoped = store.search([1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL, recording_ids=[recording.id])
    assert {hit.chunk_id for hit in scoped} == {first.id, second.id, third.id}
    assert store.search([1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL, recording_ids=[]) == []


@requires_sqlite_vec
def test_search_acl_scope_and_trashed_recordings(vdb):
    admin_recording = make_recording(vdb, title="管理员录音")
    admin_chunk = _add_vector_chunk(vdb, admin_recording, idx=0, text="公共内容", vector=[1.0, 0.0])
    member = create_member(vdb, "vec_member")
    member_recording = make_recording(vdb, title="成员录音", owner_id=member.id)
    member_chunk = _add_vector_chunk(vdb, member_recording, idx=0, text="成员内容", vector=[1.0, 0.0])
    trashed = make_recording(vdb, title="已删除", deleted_at=utcnow())
    trashed_chunk = _add_vector_chunk(vdb, trashed, idx=0, text="回收站内容", vector=[1.0, 0.0])

    store = _store()
    for recording in (admin_recording, member_recording, trashed):
        store.sync_recording(vdb, recording.id)

    admin_hits = store.search([1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL)
    ids = {hit.chunk_id for hit in admin_hits}
    assert admin_chunk.id in ids and member_chunk.id in ids
    assert trashed_chunk.id not in ids  # trashed recordings never surface

    member_hits = store.search(
        [1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL, recording_ids=[member_recording.id]
    )
    assert {hit.chunk_id for hit in member_hits} == {member_chunk.id}


@requires_sqlite_vec
def test_dimension_change_drops_old_table_rows(vdb):
    recording = make_recording(vdb, title="维度切换")
    first = _add_vector_chunk(vdb, recording, idx=0, text="三维新一", vector=[1.0, 0.0, 0.0])
    second = _add_vector_chunk(vdb, recording, idx=1, text="三维新二", vector=[0.0, 1.0, 0.0])
    store = _store()
    store.sync_recording(vdb, recording.id)
    assert len(store.search([1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL)) == 2

    # Simulate a provider/dimension change: both chunks are rewritten to dim 2.
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


@requires_sqlite_vec
def test_model_filter_and_legacy_null_vectors(vdb):
    current = make_recording(vdb, title="当前模型")
    current_chunk = _add_vector_chunk(vdb, current, idx=0, text="当前模型向量", vector=[1.0, 0.0])
    stale = make_recording(vdb, title="旧模型")
    _add_vector_chunk(vdb, stale, idx=0, text="旧模型向量", vector=[1.0, 0.0], model="old-model")
    legacy = make_recording(vdb, title="旧版无模型标记")
    legacy_chunk = _add_vector_chunk(vdb, legacy, idx=0, text="无模型向量", vector=[1.0, 0.0], model=None)

    store = _store()
    for recording in (current, stale, legacy):
        store.sync_recording(vdb, recording.id)

    hits = store.search([1.0, 0.0], top_k=10, dim=2, model=EMBED_MODEL)
    ids = {hit.chunk_id for hit in hits}
    assert current_chunk.id in ids
    assert legacy_chunk.id in ids  # legacy NULL model treated as current (numpy parity)
    assert len(hits) == 2  # stale model rows excluded


@requires_sqlite_vec
def test_upsert_and_delete_recording(vdb):
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


# ------------------------------------------------------------------ backend selection
@requires_sqlite_vec
def test_get_vector_store_selects_sqlite_vec_for_explicit_and_auto(monkeypatch):
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "vector_backend", "sqlite_vec")
    reset_vector_store_cache()
    store = get_vector_store()
    assert store.backend == "sqlite_vec"
    assert getattr(store, "fallback_reason", None) is None

    monkeypatch.setattr(app_settings, "vector_backend", "auto")
    reset_vector_store_cache()
    assert get_vector_store().backend == "sqlite_vec"  # SQLite + loadable extension

    monkeypatch.setattr(app_settings, "vector_backend", "numpy")
    reset_vector_store_cache()
    assert isinstance(get_vector_store(), NumpyVectorStore)


def _break_sqlite_vec_import(monkeypatch) -> None:
    def _raise():
        raise VectorStoreUnavailable("未安装 sqlite-vec Python 包（测试模拟）")

    monkeypatch.setattr(vector_store_module, "_import_sqlite_vec", _raise)


def test_missing_sqlite_vec_degrades_to_numpy(monkeypatch):
    _break_sqlite_vec_import(monkeypatch)
    store = SqliteVecVectorStore()
    healthy, reason = store.health()
    assert healthy is False
    assert "sqlite-vec" in reason

    monkeypatch.setattr(settings, "vector_backend", "sqlite_vec")
    reset_vector_store_cache()
    fallback = get_vector_store()
    assert isinstance(fallback, NumpyVectorStore)
    assert fallback.backend == "numpy"
    assert "sqlite-vec" in (fallback.fallback_reason or "")
    assert "已降级为 numpy" in (fallback.fallback_reason or "")


def test_auto_falls_back_to_numpy_when_sqlite_vec_unavailable(monkeypatch):
    _break_sqlite_vec_import(monkeypatch)
    monkeypatch.setattr(settings, "vector_backend", "auto")
    reset_vector_store_cache()
    fallback = get_vector_store()
    assert fallback.backend == "numpy"
    assert "sqlite-vec" in (fallback.fallback_reason or "")


def test_sqlite_vec_store_operations_raise_friendly_unavailable(monkeypatch):
    _break_sqlite_vec_import(monkeypatch)
    store = SqliteVecVectorStore()
    with pytest.raises(VectorStoreUnavailable) as excinfo:
        store.search([1.0, 0.0], top_k=3, dim=2)
    assert "sqlite-vec" in excinfo.value.reason
    stats = store.stats(dim=2)
    assert stats.available is False
    assert stats.fallback_reason


# ------------------------------------------------------------------ reindex backfill
@requires_sqlite_vec
def test_reindex_backfills_mirror_without_reembedding(vdb, monkeypatch):
    monkeypatch.setattr(settings, "vector_backend", "sqlite_vec")
    reset_vector_store_cache()
    configure_embeddings(vdb, "mock")
    recording = seed_recording(vdb, title="已嵌入未镜像", embed=True)

    class NoEmbedProvider:
        model = EMBED_MODEL
        dim = 256

        async def embed(self, texts):  # pragma: no cover - must not be called
            raise AssertionError("reindex 回填不应重新调用 embedding")

        async def embed_query(self, texts):  # pragma: no cover
            raise AssertionError("reindex 回填不应调用 query embedding")

    result = reindex_recordings(vdb, provider=NoEmbedProvider(), recording_ids=[recording.id])
    assert result.chunks == 0
    assert result.skipped == 3
    assert result.errors == []

    store = get_vector_store()
    assert store.backend == "sqlite_vec"
    query = np.asarray(vector_values("两周摘要"), dtype=np.float32)
    hits = store.search(
        query,
        top_k=3,
        dim=256,
        model=EMBED_MODEL,
        min_score=-1.0,
        recording_ids=[recording.id],
    )
    assert len(hits) == 3  # BLOB copied into the vec0 table without new embeddings


@requires_sqlite_vec
def test_retrieval_parity_numpy_vs_sqlite_vec(vdb, monkeypatch):
    configure_embeddings(vdb, "mock")
    admin = create_admin(vdb)
    recording = seed_recording(vdb, title="parity", owner_id=admin.id)

    monkeypatch.setattr(settings, "vector_backend", "numpy")
    reset_vector_store_cache()
    numpy_result = asyncio.run(retrieve_for_query(vdb, admin, "两周摘要", mode="semantic", limit=5))
    assert numpy_result.mode == "semantic" and numpy_result.hits

    monkeypatch.setattr(settings, "vector_backend", "sqlite_vec")
    reset_vector_store_cache()
    store = get_vector_store()
    assert store.backend == "sqlite_vec"
    store.sync_recording(vdb, recording.id)  # BLOB → vec0 mirror, no re-embedding
    sqlite_result = asyncio.run(retrieve_for_query(vdb, admin, "两周摘要", mode="semantic", limit=5))

    assert sqlite_result.mode == "semantic" and sqlite_result.hits
    assert sqlite_result.hits[0].segment_id == numpy_result.hits[0].segment_id
    assert sqlite_result.hits[0].recording_id == numpy_result.hits[0].recording_id
    assert sqlite_result.hits[0].score == pytest.approx(numpy_result.hits[0].score, abs=1e-4)
    numpy_chunks = {hit.chunk_id for hit in numpy_result.hits}
    sqlite_chunks = {hit.chunk_id for hit in sqlite_result.hits}
    assert numpy_chunks & sqlite_chunks  # high overlap / exact for small libraries
    assert sqlite_chunks == numpy_chunks


# ------------------------------------------------------------------ API parity
@requires_sqlite_vec
def test_api_status_reindex_search_with_sqlite_vec(auth_client, db, monkeypatch):
    monkeypatch.setattr(settings, "vector_backend", "sqlite_vec")
    reset_vector_store_cache()
    configure_embeddings(db, "mock")
    admin = db.scalar(select(User).where(User.username == "admin"))
    assert admin is not None
    recording = seed_recording(db, title="API sqlite-vec", owner_id=admin.id, embed=False)

    status = auth_client.get("/api/vector/status")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["backend"] == "sqlite_vec"
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
    assert store.backend == "sqlite_vec"
    assert store.stats(dim=256).chunks_indexed == 3

    search = auth_client.get(
        "/api/vector/search", params={"q": "两周摘要", "mode": "semantic", "limit": 5}
    ).json()
    assert search["mode"] == "semantic"
    assert search["fallback_reason"] is None
    assert search["results"]
    assert "两周" in search["results"][0]["text"]
    assert search["results"][0]["score"] > 0.2


@requires_sqlite_vec
def test_cli_prints_sqlite_vec_backend(vdb, monkeypatch, capsys):
    from app.tools.vector_reindex import main

    monkeypatch.setattr(settings, "vector_backend", "sqlite_vec")
    reset_vector_store_cache()
    configure_embeddings(vdb, "mock")
    seed_recording(vdb, title="CLI sqlite-vec", embed=False)
    assert main(["--all", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "sqlite_vec" in output
    assert "向量后端" in output
