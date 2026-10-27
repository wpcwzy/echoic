"""API tests: global search, dashboard, system info/seed/logs (docs/API.md §5/§6)."""

from __future__ import annotations

import pytest

from tests.test_api_helpers import (
    add_speakers,
    add_segments,
    add_tag,
    attach_tag,
    make_recording,
    seed_ready_recording,
)


# ------------------------------------------------------------------ search
def test_search_recordings_and_segments(auth_client, db):
    recording = seed_ready_recording(db, title="TraceOpt paper discussion", with_transcript=True)

    response = auth_client.get("/api/search", params={"q": "Graph500"})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "Graph500"
    assert len(body["recordings"]) == 1
    hit = body["recordings"][0]
    assert hit["id"] == recording.id
    assert hit["hit_count"] == 1
    assert hit["has_transcript"] is True

    assert len(body["segments"]) == 1
    segment = body["segments"][0]
    assert segment["recording_id"] == recording.id
    assert segment["recording_title"] == "TraceOpt paper discussion"
    assert segment["speaker"] == "吴克强"
    assert segment["start_ms"] == 0
    assert "Graph500" in segment["snippet"]
    assert len(segment["snippet"]) <= 80


def test_search_title_only_hit(auth_client, db):
    recording = seed_ready_recording(db, title="TraceOpt 论文讨论")
    response = auth_client.get("/api/search", params={"q": "TraceOpt"})
    body = response.json()
    assert [item["id"] for item in body["recordings"]] == [recording.id]
    assert body["recordings"][0]["hit_count"] == 1
    assert body["segments"] == []


def test_search_description_match(auth_client, db):
    recording = make_recording(db, title="无关键词标题", description="讨论 Graph500 榜单优化")
    response = auth_client.get("/api/search", params={"q": "Graph500"})
    body = response.json()
    assert [item["id"] for item in body["recordings"]] == [recording.id]


def test_search_limit_and_empty_query(auth_client, db):
    seed_ready_recording(db, title="会议一", with_transcript=True)
    seed_ready_recording(db, title="会议二", with_transcript=True)

    limited = auth_client.get("/api/search", params={"q": "会议", "limit": 1})
    assert limited.status_code == 200
    assert len(limited.json()["recordings"]) <= 1

    empty = auth_client.get("/api/search", params={"q": ""})
    assert empty.status_code == 200
    assert empty.json() == {"query": "", "recordings": [], "segments": []}

    invalid = auth_client.get("/api/search", params={"q": "x", "limit": 0})
    assert invalid.status_code == 422


def test_search_requires_auth(client):
    assert client.get("/api/search", params={"q": "x"}).status_code == 401


# ------------------------------------------------------------------ dashboard
def test_dashboard_shape(auth_client, db):
    tag = add_tag(db, "组会")
    ready = seed_ready_recording(db, title="已完成", size_bytes=1000)
    attach_tag(db, ready, tag)
    make_recording(db, title="进行中", status="queued", stage="preparing", size_bytes=500)
    make_recording(db, title="失败", status="failed", stage="failed", size_bytes=300)

    response = auth_client.get("/api/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["stats"]["total"] == 3
    assert body["stats"]["ready"] == 1
    assert body["stats"]["processing"] == 1
    assert body["stats"]["failed"] == 1
    assert len(body["recent"]) == 3
    assert [item["title"] for item in body["processing"]] == ["进行中"]
    assert body["top_tags"][0]["name"] == "组会"
    assert body["top_tags"][0]["recording_count"] == 1
    assert body["storage"]["media_bytes"] == 1800
    assert body["storage"]["recordings"] == 3


def test_dashboard_requires_auth(client):
    assert client.get("/api/dashboard").status_code == 401


# ------------------------------------------------------------------ system
def test_system_info(auth_client):
    response = auth_client.get("/api/system/info")
    assert response.status_code == 200
    body = response.json()
    assert body["version"]
    assert body["data_dir"]
    assert set(body["disk"].keys()) == {"total_bytes", "used_bytes", "free_bytes"}
    assert body["recordings"] == 0
    assert body["segments"] == 0
    assert body["chunks"] == 0
    assert "embedded" in body["worker"]
    assert "running" in body["worker"]
    assert "queue_depth" in body["worker"]


def test_system_info_requires_auth(client):
    assert client.get("/api/system/info").status_code == 401


def test_seed_demo_endpoint(auth_client, monkeypatch):
    from app.services import demo as demo_module

    monkeypatch.setattr(demo_module, "seed_demo", lambda db: 1)
    response = auth_client.post("/api/system/seed-demo")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "created": 1}


def test_seed_demo_requires_admin(client, db):
    from app.core.security import hash_password
    from app.models import User

    user = User(
        username="viewer2",
        password_hash=hash_password("viewerpass123"),
        display_name="访客",
        is_admin=False,
    )
    db.add(user)
    db.commit()
    login = client.post("/api/auth/login", json={"username": "viewer2", "password": "viewerpass123"})
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/system/seed-demo", headers=headers).status_code == 403
    assert client.get("/api/system/logs", headers=headers).status_code == 403


def test_seed_demo_requires_auth(client):
    assert client.post("/api/system/seed-demo").status_code == 401
    assert client.get("/api/system/logs").status_code == 401


def test_system_logs_admin_only(auth_client):
    response = auth_client.get("/api/system/logs", params={"lines": 10})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["lines"], list)
    assert body["count"] == len(body["lines"])


def test_health_public(client):
    response = client.get("/api/system/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ok", "degraded")
    assert isinstance(body["allow_registration"], bool)


@pytest.mark.parametrize("path", ["/api/dashboard", "/api/tags", "/api/settings", "/api/auth/me"])
def test_common_endpoints_require_auth(client, path):
    assert client.get(path).status_code == 401
