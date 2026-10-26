"""R-20 vector API tests: status / reindex / search ACL + fallbacks (no network)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Chunk, User
from app.services.vector_store import reset_vector_store_cache
from tests.test_vector_helpers import (
    UNRELATED_SEGMENTS,
    configure_embeddings,
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


def _admin(db) -> User:
    admin = db.scalar(select(User).where(User.username == "admin"))
    assert admin is not None
    return admin


def _member_token(client, db, username: str = "member1") -> str:
    existing = db.scalar(select(User).where(User.username == username))
    if existing is None:
        create_member(db, username)
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "memberpass123"}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _member_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _chunks(db, recording_id: int) -> list[Chunk]:
    with SessionLocal() as fresh:
        return list(
            fresh.scalars(
                select(Chunk)
                .where(Chunk.recording_id == recording_id)
                .order_by(Chunk.idx.asc())
            ).all()
        )


# ------------------------------------------------------------------ status
def test_status_requires_admin(client, db):
    assert client.get("/api/vector/status").status_code == 401
    token = _member_token(client, db)
    response = client.get("/api/vector/status", headers=_member_headers(token))
    assert response.status_code == 403


def test_status_reports_backend_and_counts(auth_client, db):
    configure_embeddings(db, "mock")
    admin = _admin(db)
    recording = seed_recording(db, title="状态测试", owner_id=admin.id, embed=False)

    response = auth_client.get("/api/vector/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["backend"] == "numpy"
    assert body["available"] is True
    assert body["embeddings_configured"] is True
    assert body["embedding_model"] == "mock-embedding"
    assert body["dim"] == 256
    assert body["chunks_total"] == 3
    assert body["chunks_indexed"] == 0
    assert body["stale_chunks"] == 0
    assert body["fallback_reason"] is None
    assert recording.id > 0


# ------------------------------------------------------------------ reindex
def test_reindex_requires_embeddings(auth_client, db):
    configure_embeddings(db, "none")
    response = auth_client.post("/api/vector/reindex", json={})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "EMBEDDINGS_NOT_CONFIGURED"
    assert "Embedding" in body["detail"] or "embedding" in body["detail"]


def test_reindex_dry_run_then_apply(auth_client, db):
    configure_embeddings(db, "mock")
    admin = _admin(db)
    recording = seed_recording(db, title="重建索引", owner_id=admin.id, embed=False)

    dry = auth_client.post("/api/vector/reindex", json={"dry_run": True})
    assert dry.status_code == 200, dry.text
    dry_body = dry.json()
    assert dry_body["ok"] is True
    assert dry_body["processed_recordings"] == 1
    assert dry_body["chunks"] == 3
    assert dry_body["skipped"] == 0
    assert dry_body["errors"] == []
    assert all(chunk.embedding is None for chunk in _chunks(db, recording.id))

    apply = auth_client.post(
        "/api/vector/reindex", json={"recording_ids": [recording.id]}
    )
    assert apply.status_code == 200, apply.text
    apply_body = apply.json()
    assert apply_body["ok"] is True
    assert apply_body["processed_recordings"] == 1
    assert apply_body["chunks"] == 3
    stored = _chunks(db, recording.id)
    assert all(chunk.embedding and chunk.dim == 256 for chunk in stored)
    assert all(chunk.embedding_model == "mock-embedding" for chunk in stored)

    status_body = auth_client.get("/api/vector/status").json()
    assert status_body["chunks_indexed"] == 3
    assert status_body["stale_chunks"] == 0

    again = auth_client.post("/api/vector/reindex", json={}).json()
    assert again["chunks"] == 0
    assert again["skipped"] == 3

    forced = auth_client.post("/api/vector/reindex", json={"force": True}).json()
    assert forced["chunks"] == 3
    assert forced["skipped"] == 0


def test_reindex_validation(auth_client, db):
    configure_embeddings(db, "mock")
    too_many = auth_client.post(
        "/api/vector/reindex", json={"recording_ids": list(range(1, 202))}
    )
    assert too_many.status_code == 400
    assert "200" in too_many.json()["detail"]

    invalid = auth_client.post("/api/vector/reindex", json={"recording_ids": [0]})
    assert invalid.status_code == 400

    missing = auth_client.post("/api/vector/reindex", json={"recording_ids": [99999]})
    # nonexistent recordings are reported as per-recording errors, not a 400
    assert missing.status_code == 200
    body = missing.json()
    assert body["ok"] is False
    assert any("99999" in error for error in body["errors"])


# ------------------------------------------------------------------ search
def _seed_search_library(db) -> dict:
    configure_embeddings(db, "mock")
    admin = _admin(db)
    member = create_member(db, "member2")
    rec_admin = seed_recording(db, title="管理员会议", owner_id=admin.id, embed=False)
    rec_weather = seed_recording(
        db,
        title="天气讨论",
        owner_id=admin.id,
        segments=UNRELATED_SEGMENTS,
        chunks=[(0, 12_000, UNRELATED_SEGMENTS[0][3])],
        embed=False,
    )
    rec_member = seed_recording(
        db,
        title="成员会议",
        owner_id=member.id,
        segments=MEMBER_SEGMENTS,
        chunks=[(0, 12_000, MEMBER_SEGMENTS[0][3])],
        embed=False,
    )
    return {
        "admin": admin,
        "member": member,
        "rec_admin": rec_admin,
        "rec_weather": rec_weather,
        "rec_member": rec_member,
    }


def test_search_modes_after_reindex(auth_client, db):
    library = _seed_search_library(db)
    reindexed = auth_client.post("/api/vector/reindex", json={}).json()
    assert reindexed["ok"] is True
    assert reindexed["chunks"] == 5  # 3 (admin) + 1 (weather) + 1 (member)

    semantic = auth_client.get(
        "/api/vector/search", params={"q": "两周摘要", "mode": "semantic", "limit": 5}
    )
    assert semantic.status_code == 200, semantic.text
    semantic_body = semantic.json()
    assert semantic_body["mode"] == "semantic"
    assert semantic_body["fallback_reason"] is None
    assert semantic_body["results"]
    top = semantic_body["results"][0]
    assert top["recording_id"] == library["rec_admin"].id
    assert "两周" in top["text"]
    assert top["score"] > 0.2
    assert top["speaker"] == "李梦"
    assert top["segment_id"] is not None
    assert top["end_ms"] > top["start_ms"]

    hybrid = auth_client.get(
        "/api/vector/search", params={"q": "两周", "mode": "hybrid", "limit": 5}
    )
    assert hybrid.status_code == 200
    hybrid_body = hybrid.json()
    assert hybrid_body["mode"] == "hybrid"
    assert hybrid_body["fallback_reason"] is None
    assert hybrid_body["results"]
    segment_ids = [
        result["segment_id"]
        for result in hybrid_body["results"]
        if result["segment_id"] is not None
    ]
    assert len(segment_ids) == len(set(segment_ids))

    keyword = auth_client.get(
        "/api/vector/search", params={"q": "两周", "mode": "keyword", "limit": 5}
    )
    keyword_body = keyword.json()
    assert keyword_body["mode"] == "keyword"
    assert keyword_body["results"]
    assert "两周" in keyword_body["results"][0]["text"]

    limited = auth_client.get(
        "/api/vector/search", params={"q": "两周", "mode": "keyword", "limit": 1}
    ).json()
    assert len(limited["results"]) == 1


def test_search_acl_and_trash(auth_client, db):
    library = _seed_search_library(db)
    assert auth_client.post("/api/vector/reindex", json={}).json()["ok"] is True

    admin_search = auth_client.get(
        "/api/vector/search", params={"q": "两周", "mode": "hybrid"}
    ).json()
    assert {result["recording_id"] for result in admin_search["results"]} == {
        library["rec_admin"].id
    }

    # member login sets the auth cookie, so member checks come after admin ones
    token = _member_token(auth_client, db, "member2")
    headers = _member_headers(token)

    not_mine = auth_client.get(
        "/api/vector/search", params={"q": "两周", "mode": "hybrid"}, headers=headers
    ).json()
    assert not_mine["results"] == []

    own = auth_client.get(
        "/api/vector/search",
        params={"q": "成员专属", "mode": "semantic"},
        headers=headers,
    )
    assert own.status_code == 200
    own_body = own.json()
    assert own_body["results"]
    assert {result["recording_id"] for result in own_body["results"]} == {
        library["rec_member"].id
    }


def test_search_falls_back_without_embeddings(auth_client, db):
    _seed_search_library(db)
    configure_embeddings(db, "none")
    response = auth_client.get(
        "/api/vector/search", params={"q": "两周", "mode": "semantic"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "keyword"
    assert body["fallback_reason"] and "未配置" in body["fallback_reason"]
    assert body["results"]
    assert all("两周" in result["text"] for result in body["results"])


def test_search_requires_auth_and_validates_params(client):
    assert client.get("/api/vector/search", params={"q": "x"}).status_code == 401


def test_search_validation(auth_client):
    assert auth_client.get("/api/vector/search", params={"mode": "bogus"}).status_code == 422
    assert auth_client.get("/api/vector/search", params={"q": ""}).status_code == 422
    assert (
        auth_client.get("/api/vector/search", params={"q": "x", "limit": 51}).status_code
        == 422
    )


# ------------------------------------------------------------------ settings integration
def test_embeddings_mock_selectable_via_settings_api(auth_client, db):
    """The settings page can enable the mock embeddings provider (R-20 demo mode)."""
    update = auth_client.put(
        "/api/settings",
        json={"ai": {"embeddings": {"provider": "mock", "dimensions": 1024}}},
    )
    assert update.status_code == 200, update.text
    assert update.json()["ai"]["embeddings"]["provider"] == "mock"

    probe = auth_client.post("/api/settings/test", json={"kind": "embeddings"})
    assert probe.status_code == 200, probe.text
    body = probe.json()
    assert body["ok"] is True
    assert "256" in body["message"] or "向量" in body["message"]
