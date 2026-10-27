"""R-20 retrieval tests: keyword/semantic/hybrid RRF, ACL, fallback reasons, chat RAG."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.utils import utcnow
from app.models import Chunk
from app.services.retrieval import (
    retrieve_for_chat,
    retrieve_for_query,
)
from app.services.vector_store import reset_vector_store_cache
from tests.test_vector_helpers import (
    UNRELATED_SEGMENTS,
    vdb,
    configure_embeddings,
    create_admin,
    create_member,
    seed_recording,
)

MEMBER_SEGMENTS = [
    (0, 12_000, 0, "成员专属内容，讨论成员自己的交付节点。"),
]


@pytest.fixture(autouse=True)
def _clean_vector_state():
    reset_vector_store_cache()
    yield
    reset_vector_store_cache()


@pytest.fixture(autouse=True)
def _force_numpy_backend(monkeypatch):
    """Semantic/ACL expectations are contract-tested on the numpy backend; the
    sqlite-vec backend has dedicated parity tests in test_vector_sqlite_vec.py."""
    monkeypatch.setattr(settings, "vector_backend", "numpy")
    reset_vector_store_cache()
    yield
    reset_vector_store_cache()


@pytest.fixture()
def library(vdb):
    configure_embeddings(vdb, "mock")
    admin = create_admin(vdb)
    member = create_member(vdb)
    rec_a = seed_recording(vdb, title="会议A 交付节点", owner_id=admin.id)
    rec_b = seed_recording(
        vdb,
        title="会议B 天气",
        owner_id=admin.id,
        segments=UNRELATED_SEGMENTS,
        chunks=[(0, 12_000, UNRELATED_SEGMENTS[0][3])],
    )
    rec_member = seed_recording(
        vdb,
        title="成员会议",
        owner_id=member.id,
        segments=MEMBER_SEGMENTS,
        chunks=[(0, 12_000, MEMBER_SEGMENTS[0][3])],
    )
    trashed = seed_recording(
        vdb,
        title="已删除的会议",
        owner_id=admin.id,
        deleted_at=utcnow(),
        segments=[(0, 12_000, 0, "两周摘要 已删除的会议内容")],
        chunks=[(0, 12_000, "两周摘要 已删除的会议内容")],
    )
    return {
        "admin": admin,
        "member": member,
        "rec_a": rec_a,
        "rec_b": rec_b,
        "rec_member": rec_member,
        "trashed": trashed,
    }


def test_keyword_mode_hits_and_title_fallback(library, vdb):
    result = asyncio.run(
        retrieve_for_query(vdb, library["admin"], "两周", mode="keyword", limit=10)
    )
    assert result.mode == "keyword"
    assert result.fallback_reason is None
    assert result.hits
    top = result.hits[0]
    assert top.recording_id == library["rec_a"].id
    assert top.segment_id is not None
    assert "两周" in top.text
    assert top.score > 0.5
    assert top.speaker == "李梦"

    # title/description matches surface as recording-level hits (segment_id None)
    title_result = asyncio.run(
        retrieve_for_query(vdb, library["admin"], "会议B", mode="keyword", limit=10)
    )
    assert any(
        hit.segment_id is None and hit.recording_title == "会议B 天气"
        for hit in title_result.hits
    )


def test_keyword_mode_acl_and_trash(library, vdb):
    admin_result = asyncio.run(
        retrieve_for_query(vdb, library["admin"], "两周", mode="keyword", limit=10)
    )
    recording_ids = {hit.recording_id for hit in admin_result.hits}
    assert library["trashed"].id not in recording_ids  # soft-deleted excluded
    assert library["rec_member"].id not in recording_ids  # no match, but also ACL-safe

    member_result = asyncio.run(
        retrieve_for_query(vdb, library["member"], "两周", mode="keyword", limit=10)
    )
    assert member_result.hits == []  # member must not see the admin recording

    own_result = asyncio.run(
        retrieve_for_query(vdb, library["member"], "成员专属", mode="keyword", limit=10)
    )
    assert [hit.recording_id for hit in own_result.hits] == [library["rec_member"].id]


def test_semantic_mode_scores_and_acl(library, vdb):
    result = asyncio.run(
        retrieve_for_query(vdb, library["admin"], "两周摘要", mode="semantic", limit=5)
    )
    assert result.mode == "semantic"
    assert result.fallback_reason is None
    assert result.hits
    top = result.hits[0]
    assert top.recording_id == library["rec_a"].id
    assert "两周" in top.text
    assert top.score > 0.2  # ECHOIC_VECTOR_MIN_SCORE default
    assert top.segment_id is not None
    assert all(hit.score >= 0.2 for hit in result.hits)

    member_result = asyncio.run(
        retrieve_for_query(vdb, library["member"], "成员专属", mode="semantic", limit=5)
    )
    assert member_result.hits
    assert {hit.recording_id for hit in member_result.hits} == {library["rec_member"].id}


def test_hybrid_mode_rrf_fuses_and_dedupes(library, vdb):
    result = asyncio.run(
        retrieve_for_query(vdb, library["admin"], "两周", mode="hybrid", limit=10)
    )
    assert result.mode == "hybrid"
    assert result.fallback_reason is None
    assert result.hits
    segment_ids = [hit.segment_id for hit in result.hits if hit.segment_id is not None]
    assert len(segment_ids) == len(set(segment_ids))  # deduped by segment

    top = result.hits[0]
    assert top.segment_id is not None
    assert "两周" in top.text
    # keyword rank 1 + vector rank 1 → 2 / (RRF_K + 1)
    assert top.score == pytest.approx(2 / 61, abs=1e-3)
    assert top.source == "hybrid"


def test_semantic_mode_falls_back_when_embeddings_unconfigured(library, vdb):
    configure_embeddings(vdb, "none")
    result = asyncio.run(
        retrieve_for_query(vdb, library["admin"], "两周", mode="semantic", limit=5)
    )
    assert result.mode == "keyword"
    assert result.fallback_reason and "未配置" in result.fallback_reason
    assert result.hits  # keyword hits still delivered
    assert all(hit.source == "keyword" for hit in result.hits)


def test_hybrid_falls_back_to_keyword_when_index_empty(vdb):
    configure_embeddings(vdb, "mock")
    seed_recording(vdb, title="没有向量的会议", embed=False)
    result = asyncio.run(
        retrieve_for_query(vdb, None, "两周", mode="hybrid", limit=5)
    )
    assert result.mode == "keyword"
    assert result.fallback_reason and "向量索引为空" in result.fallback_reason
    assert result.hits


def test_result_limit_is_respected(library, vdb):
    result = asyncio.run(
        retrieve_for_query(vdb, library["admin"], "会议", mode="keyword", limit=1)
    )
    assert len(result.hits) <= 1


def test_retrieve_for_chat_vector_mode(library, vdb):
    retrieval = asyncio.run(
        retrieve_for_chat(vdb, library["rec_a"].id, "两周摘要", top_k=3)
    )
    assert retrieval.mode == "vector"
    assert retrieval.fallback_reason is None
    assert len(retrieval.chunks) == 3
    top = retrieval.chunks[0]
    assert "两周" in top.text
    assert top.segment_id is not None
    assert top.speaker == "李梦"
    assert top.recording_id == library["rec_a"].id
    scores = [hit.score for hit in retrieval.chunks]
    assert scores == sorted(scores, reverse=True)
    assert top.score > 0.2

    # only chunks from the requested recording
    chunk_ids = {hit.chunk_id for hit in retrieval.chunks}
    expected = {
        int(value)
        for value in vdb.scalars(
            select(Chunk.id).where(Chunk.recording_id == library["rec_a"].id)
        ).all()
    }
    assert chunk_ids <= expected


def test_retrieve_for_chat_lexical_fallback(library, vdb):
    configure_embeddings(vdb, "none")
    retrieval = asyncio.run(
        retrieve_for_chat(vdb, library["rec_a"].id, "两周摘要", top_k=3)
    )
    assert retrieval.mode == "lexical"
    assert retrieval.fallback_reason and "未配置" in retrieval.fallback_reason
    assert retrieval.chunks
    assert all(hit.recording_id == library["rec_a"].id for hit in retrieval.chunks)


def test_retrieve_for_chat_lexical_when_index_missing(vdb):
    configure_embeddings(vdb, "mock")
    recording = seed_recording(vdb, title="尚未建索引", embed=False)
    retrieval = asyncio.run(retrieve_for_chat(vdb, recording.id, "两周摘要", top_k=3))
    assert retrieval.mode == "lexical"
    assert retrieval.fallback_reason and "尚未生成向量索引" in retrieval.fallback_reason
    assert retrieval.chunks


def test_retrieve_for_chat_rejects_deleted_recording(library, vdb):
    retrieval = asyncio.run(
        retrieve_for_chat(vdb, library["trashed"].id, "两周", top_k=3)
    )
    assert retrieval.mode == "empty"
    assert retrieval.chunks == []
    assert retrieval.fallback_reason and "删除" in retrieval.fallback_reason
