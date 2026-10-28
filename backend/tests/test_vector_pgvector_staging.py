"""Golden-path staging test for a real PostgreSQL + pgvector server.

Skipped unless ``ECHOIC_PGVECTOR_TEST_DSN`` is configured (the default SQLite
test DB has no pgvector).  The test creates its **own** SQLAlchemy engine and
tables, so it never touches the app's global SQLite engine/conftest DB.

Run locally / CI::

    # container (pgvector/pgvector:pg16), superuser required for CREATE EXTENSION
    ECHOIC_PGVECTOR_TEST_DSN='postgresql+psycopg://postgres:postgres@127.0.0.1:5433/postgres' \
        backend/.venv/bin/pytest -q backend/tests/test_vector_pgvector_staging.py

Verified in the v1.0 staging workflow: extension creation, chunk_vectors DDL +
optional HNSW index, upsert/search/ACL/delete/stats, model stale filtering and
the dim-change "rebuild required" contract.
"""

from __future__ import annotations

import os
import uuid

import numpy as np
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.utils import utcnow
from app.db import Base
from app.models import Chunk, Recording
from app.services.vector_store import (
    PgVectorStore,
    VectorStoreUnavailable,
)

DSN = (os.environ.get("ECHOIC_PGVECTOR_TEST_DSN") or "").strip()
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="ECHOIC_PGVECTOR_TEST_DSN 未配置，跳过真实 PostgreSQL + pgvector 集成测试",
)

EMBED_MODEL = "mock-embedding"
DIM = 3


def _drop_vector_table(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS chunk_vectors CASCADE"))


@pytest.fixture(scope="module")
def pg_engine():
    if not DSN:  # pragma: no cover - pytest.mark.skipif already skips
        pytest.skip("ECHOIC_PGVECTOR_TEST_DSN 未配置")
    try:
        engine = create_engine(DSN, pool_pre_ping=True)
    except Exception as exc:  # noqa: BLE001 - misconfigured DSN should fail loudly
        pytest.fail(f"无法创建 PostgreSQL 引擎：{type(exc).__name__}: {exc}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            version = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
        if not version:
            pytest.fail("PostgreSQL 未安装 pgvector 扩展（CREATE EXTENSION vector 失败）")
        _drop_vector_table(engine)
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        yield engine
    finally:
        try:
            _drop_vector_table(engine)
            Base.metadata.drop_all(bind=engine)
        finally:
            engine.dispose()


@pytest.fixture()
def pg_session(pg_engine):
    with Session(pg_engine) as session:
        yield session
        session.rollback()
    # Isolate tests: close the session above before any DDL (chunk_vectors carries
    # FK constraints on chunks/recordings, so DROP waits for open transactions).
    _drop_vector_table(pg_engine)
    with Session(pg_engine) as session:
        session.query(Chunk).delete()
        session.query(Recording).delete()
        session.commit()


def _seed_recording(
    session: Session,
    vectors: list[list[float]],
    *,
    models: list[str | None] | None = None,
    title: str = "staging",
) -> tuple[Recording, list[Chunk]]:
    recording = Recording(
        uuid=str(uuid.uuid4()),
        title=f"{title}-{uuid.uuid4().hex[:8]}",
        status="ready",
        stage="done",
        recorded_at=utcnow(),
    )
    session.add(recording)
    session.flush()
    chunks: list[Chunk] = []
    for index, vector in enumerate(vectors):
        array = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(array))
        if norm:
            array = array / norm
        model = models[index] if models is not None else EMBED_MODEL
        chunk = Chunk(
            recording_id=recording.id,
            idx=index,
            start_ms=index * 1000,
            end_ms=index * 1000 + 1000,
            text=f"staging chunk {index}",
            embedding=array.tobytes(),
            dim=int(array.size),
            embedding_model=model,
        )
        session.add(chunk)
        chunks.append(chunk)
    session.commit()
    for chunk in chunks:
        session.refresh(chunk)
    return recording, chunks


def test_health_and_collection_ddl(pg_session, pg_engine):
    store = PgVectorStore(pg_engine)
    healthy, message = store.health()
    assert healthy is True, message
    assert "pgvector" in message

    recording, _chunks = _seed_recording(pg_session, [[1.0, 0.0, 0.0]], title="health")
    store.sync_recording(pg_session, recording.id)
    with pg_engine.begin() as connection:
        index_row = connection.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'chunk_vectors'")
        ).fetchall()
    index_names = {str(row[0]) for row in index_row}
    assert "ix_chunk_vectors_recording" in index_names
    # HNSW is created best-effort; the pgvector/pgvector:pg16 staging image supports it.
    assert "ix_chunk_vectors_hnsw" in index_names


def test_sync_search_acl_delete_stats(pg_session, pg_engine):
    store = PgVectorStore(pg_engine)
    first, first_chunks = _seed_recording(
        pg_session, [[1.0, 0.0, 0.0], [0.8, 0.6, 0.0], [0.0, 1.0, 0.0]], title="acl-a"
    )
    second, second_chunks = _seed_recording(
        pg_session, [[0.0, 0.0, 1.0]], title="acl-b"
    )
    store.sync_recording(pg_session, first.id)
    store.sync_recording(pg_session, second.id)

    hits = store.search([1.0, 0.0, 0.0], top_k=2, dim=DIM, model=EMBED_MODEL)
    assert [hit.chunk_id for hit in hits] == [first_chunks[0].id, first_chunks[1].id]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[1].score == pytest.approx(0.8, abs=1e-5)

    top_one = store.search([1.0, 0.0, 0.0], top_k=1, dim=DIM, model=EMBED_MODEL)
    assert [hit.chunk_id for hit in top_one] == [first_chunks[0].id]
    high = store.search(
        [1.0, 0.0, 0.0], top_k=10, dim=DIM, model=EMBED_MODEL, min_score=0.9
    )
    assert [hit.chunk_id for hit in high] == [first_chunks[0].id]

    scoped = store.search(
        [1.0, 0.0, 0.0], top_k=10, dim=DIM, model=EMBED_MODEL, recording_ids=[first.id]
    )
    assert {hit.chunk_id for hit in scoped} == {chunk.id for chunk in first_chunks}

    stats = store.stats(dim=DIM, model=EMBED_MODEL)
    assert stats.available is True
    assert stats.chunks_indexed == 4

    deleted = store.delete_recording(second.id)
    assert deleted == 1
    remaining = store.search(
        [0.0, 0.0, 1.0], top_k=10, dim=DIM, model=EMBED_MODEL, recording_ids=[second.id]
    )
    assert remaining == []


def test_model_stale_filter(pg_session, pg_engine):
    store = PgVectorStore(pg_engine)
    current = Recording(
        uuid=str(uuid.uuid4()),
        title="model-current",
        status="ready",
        stage="done",
    )
    stale = Recording(
        uuid=str(uuid.uuid4()),
        title="model-stale",
        status="ready",
        stage="done",
    )
    pg_session.add_all([current, stale])
    pg_session.flush()

    def _chunk(recording: Recording, model: str | None) -> Chunk:
        array = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        return Chunk(
            recording_id=recording.id,
            idx=0,
            start_ms=0,
            end_ms=1000,
            text="model",
            embedding=array.tobytes(),
            dim=3,
            embedding_model=model,
        )

    current_chunk = _chunk(current, EMBED_MODEL)
    stale_chunk = _chunk(stale, "old-model")
    pg_session.add_all([current_chunk, stale_chunk])
    pg_session.commit()

    store.sync_recording(pg_session, current.id)
    store.sync_recording(pg_session, stale.id)
    hits = store.search([1.0, 0.0, 0.0], top_k=10, dim=3, model=EMBED_MODEL)
    ids = {hit.chunk_id for hit in hits}
    assert current_chunk.id in ids
    assert stale_chunk.id not in ids


def test_dimension_change_requires_rebuild(pg_session, pg_engine):
    store = PgVectorStore(pg_engine)
    recording, chunks = _seed_recording(pg_session, [[1.0, 0.0, 0.0]], title="dim")
    store.sync_recording(pg_session, recording.id)
    assert store.search([1.0, 0.0, 0.0], top_k=5, dim=3, model=EMBED_MODEL)

    # Same chunk rewritten to dim 2 → existing chunk_vectors(vector(3)) must reject.
    array = np.asarray([1.0, 0.0], dtype=np.float32)
    chunks[0].embedding = array.tobytes()
    chunks[0].dim = 2
    pg_session.commit()
    with pytest.raises(VectorStoreUnavailable) as excinfo:
        store.sync_recording(pg_session, recording.id)
    assert "维度" in excinfo.value.reason

    # Admin rebuild path: close the session transaction before DDL, drop the ANN
    # mirror table, then sync the new dimension.
    pg_session.commit()
    _drop_vector_table(pg_engine)
    store = PgVectorStore(pg_engine)
    refreshed, refreshed_chunks = _seed_recording(
        pg_session, [[0.0, 1.0]], title="dim2"
    )
    # also rewrite the original recording's chunk back to its dim-2 state for the mirror
    store.sync_recording(pg_session, recording.id)
    store.sync_recording(pg_session, refreshed.id)
    dim2_hits = store.search([1.0, 0.0], top_k=5, dim=2, model=EMBED_MODEL)
    assert {hit.chunk_id for hit in dim2_hits} >= {chunks[0].id, refreshed_chunks[0].id}
    assert store.search([1.0, 0.0, 0.0], top_k=5, dim=3, model=EMBED_MODEL) == []
