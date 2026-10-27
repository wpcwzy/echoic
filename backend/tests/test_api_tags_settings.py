"""API tests: tags CRUD + settings masking/provider test (docs/API.md §5/§6)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.models import AppSetting, User
from app.services.settings_store import DEFAULT_PROMPTS
from tests.test_api_helpers import add_tag, attach_tag, make_recording, seed_ready_recording


def _login_as_non_admin(client, db) -> dict:
    from app.core.security import hash_password

    user = User(
        username="viewer",
        password_hash=hash_password("viewerpass123"),
        display_name="访客",
        is_admin=False,
    )
    db.add(user)
    db.commit()
    response = client.post(
        "/api/auth/login", json={"username": "viewer", "password": "viewerpass123"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


# ------------------------------------------------------------------ tags
def test_tag_crud(auth_client):
    created = auth_client.post("/api/tags", json={"name": "论文", "color": "#1677ff"})
    assert created.status_code == 201
    tag = created.json()
    assert tag["name"] == "论文"
    assert tag["color"] == "#1677ff"

    duplicate = auth_client.post("/api/tags", json={"name": "论文"})
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "CONFLICT"

    updated = auth_client.patch(f"/api/tags/{tag['id']}", json={"name": "论文讨论", "color": "#ff0000"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "论文讨论"
    assert updated.json()["color"] == "#ff0000"

    listing = auth_client.get("/api/tags")
    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()["items"]] == ["论文讨论"]

    deleted = auth_client.delete(f"/api/tags/{tag['id']}")
    assert deleted.status_code == 200
    assert auth_client.get("/api/tags").json()["items"] == []
    assert auth_client.delete(f"/api/tags/{tag['id']}").status_code == 404


def test_tag_list_counts_and_delete_detaches(auth_client, db):
    tag = add_tag(db, "组会")
    first = seed_ready_recording(db, title="会议一")
    second = seed_ready_recording(db, title="会议二")
    attach_tag(db, first, tag)
    attach_tag(db, second, tag)

    items = auth_client.get("/api/tags").json()["items"]
    assert items[0]["recording_count"] == 2

    assert auth_client.delete(f"/api/tags/{tag.id}").status_code == 200
    detail = auth_client.get(f"/api/recordings/{first.id}").json()
    assert detail["tags"] == []


def test_tag_patch_duplicate_and_missing(auth_client, db):
    add_tag(db, "甲")
    second = add_tag(db, "乙")
    duplicate = auth_client.patch(f"/api/tags/{second.id}", json={"name": "甲"})
    assert duplicate.status_code == 409
    assert auth_client.patch("/api/tags/9999", json={"name": "丙"}).status_code == 404


def test_tags_require_auth(client):
    assert client.get("/api/tags").status_code == 401
    assert client.post("/api/tags", json={"name": "x"}).status_code == 401


# ------------------------------------------------------------------ settings masking
def test_get_settings_shape_and_masking(auth_client, db):
    response = auth_client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert set(body["ai"].keys()) == {"asr", "realtime_asr", "llm", "embeddings"}
    asr = body["ai"]["asr"]
    for key in ("provider", "base_url", "model", "language", "diarization", "has_api_key", "api_key_masked", "configured"):
        assert key in asr
    realtime_asr = body["ai"]["realtime_asr"]
    for key in ("provider", "base_url", "model", "language", "has_api_key", "api_key_masked", "configured"):
        assert key in realtime_asr
    assert body["ai"]["llm"]["temperature"] == 0.3
    assert body["ai"]["embeddings"]["dimensions"] == 1024
    assert set(body["prompts"].keys()) == {
        "summary",
        "chapters",
        "viewpoint_tree",
        "suggested_questions",
        "chat",
    }
    assert "prompt_overrides" in body
    system = body["system"]
    assert system["version"] == settings.version
    assert system["max_upload_mb"] == settings.max_upload_mb
    assert "worker" in system

    # no plaintext secret is ever returned
    serialized = response.text
    for value in ("api_key", '"api_key_enc"'):
        assert f'"{value}"' not in serialized


def test_put_settings_stores_encrypted_key_and_masks(auth_client, db):
    secret = "sk-secret123456789"
    response = auth_client.put(
        "/api/settings",
        json={
            "ai": {
                "llm": {
                    "provider": "openai",
                    "base_url": "https://example.com/v1",
                    "model": "qwen-plus",
                    "api_key": secret,
                }
            }
        },
    )
    assert response.status_code == 200, response.text
    llm = response.json()["ai"]["llm"]
    assert llm["provider"] == "openai"
    assert llm["base_url"] == "https://example.com/v1"
    assert llm["model"] == "qwen-plus"
    assert llm["has_api_key"] is True
    assert llm["api_key_masked"] == "sk-****6789"
    assert secret not in response.text

    row = db.get(AppSetting, "ai.llm")
    db.refresh(row)
    assert secret not in (row.value_json or "")
    assert "api_key_enc" in row.value_json

    cleared = auth_client.put(
        "/api/settings", json={"ai": {"llm": {"api_key": "__clear__"}}}
    )
    assert cleared.status_code == 200
    assert cleared.json()["ai"]["llm"]["has_api_key"] is False
    assert cleared.json()["ai"]["llm"]["api_key_masked"] == ""


def test_put_settings_rejects_unknown_provider(auth_client):
    response = auth_client.put(
        "/api/settings", json={"ai": {"llm": {"provider": "bogus-provider"}}}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_put_settings_prompts_and_reset(auth_client):
    custom = "TASK=minutes\n自定义纪要提示词"
    updated = auth_client.put("/api/settings", json={"prompts": {"summary": custom}})
    assert updated.status_code == 200
    assert updated.json()["prompts"]["summary"] == custom
    assert updated.json()["prompt_overrides"] == ["summary"]

    reset = auth_client.post("/api/settings/prompts/reset")
    assert reset.status_code == 200
    assert reset.json()["prompts"]["summary"] == DEFAULT_PROMPTS["summary"]
    assert reset.json()["prompt_overrides"] == []

    unknown = auth_client.put("/api/settings", json={"prompts": {"nope": "x"}})
    assert unknown.status_code == 400


def test_put_settings_system_section(auth_client):
    response = auth_client.put(
        "/api/settings",
        json={"system": {"auto_process": False, "default_language": "en", "max_upload_mb": 512}},
    )
    assert response.status_code == 200
    system = response.json()["system"]
    assert system["auto_process"] is False
    assert system["default_language"] == "en"
    assert system["max_upload_mb"] == 512


def test_provider_test_endpoint(auth_client, monkeypatch):
    async def fake_test(db, kind, draft=None):
        return {"ok": True, "latency_ms": 87, "message": "连接成功，模型响应正常", "detail": None}

    monkeypatch.setattr("app.providers.registry.test_provider", fake_test)
    response = auth_client.post("/api/settings/test", json={"kind": "llm"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["latency_ms"] == 87
    assert "连接成功" in body["message"]

    async def failing_test(db, kind, draft=None):
        return {"ok": False, "latency_ms": None, "message": "鉴权失败：401", "detail": "401"}

    monkeypatch.setattr("app.providers.registry.test_provider", failing_test)
    failed = auth_client.post("/api/settings/test", json={"kind": "asr", "draft": {"provider": "openai"}})
    assert failed.status_code == 200
    assert failed.json()["ok"] is False
    assert "鉴权失败" in failed.json()["message"]




def test_realtime_asr_settings_are_independent(auth_client):
    file_key = "sk-file-asr-1234567890"
    realtime_key = "sk-realtime-asr-0987654321"
    response = auth_client.put(
        "/api/settings",
        json={
            "ai": {
                "asr": {
                    "provider": "dashscope_file",
                    "base_url": "https://dashscope.aliyuncs.com",
                    "model": "qwen-audio-3.1-asr-flash-filetrans",
                    "api_key": file_key,
                    "language": "zh",
                    "diarization": True,
                },
                "realtime_asr": {
                    "provider": "dashscope_realtime",
                    "base_url": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
                    "model": "paraformer-realtime-v2",
                    "api_key": realtime_key,
                    "language": "en",
                },
            }
        },
    )
    assert response.status_code == 200, response.text
    ai = response.json()["ai"]
    assert ai["asr"]["provider"] == "dashscope_file"
    assert ai["asr"]["diarization"] is True
    assert ai["realtime_asr"]["provider"] == "dashscope_realtime"
    assert ai["realtime_asr"]["language"] == "en"
    assert ai["realtime_asr"]["has_api_key"] is True
    assert ("****" in ai["asr"]["api_key_masked"]) and ("****" in ai["realtime_asr"]["api_key_masked"])
    serialized = response.text
    assert file_key not in serialized and realtime_key not in serialized

    # auto mode is accepted for the realtime slot
    auto = auth_client.put("/api/settings", json={"ai": {"realtime_asr": {"provider": "auto"}}})
    assert auto.status_code == 200
    assert auto.json()["ai"]["realtime_asr"]["provider"] == "auto"

    invalid = auth_client.put(
        "/api/settings", json={"ai": {"realtime_asr": {"provider": "dashscope_file"}}}
    )
    assert invalid.status_code == 400


def test_provider_test_accepts_realtime_asr_kind(auth_client, monkeypatch):
    async def fake_test(db, kind, draft=None):
        assert kind == "realtime_asr"
        return {"ok": True, "latency_ms": 12, "message": "实时转写连接成功", "detail": None}

    monkeypatch.setattr("app.providers.registry.test_provider", fake_test)
    response = auth_client.post(
        "/api/settings/test",
        json={"kind": "realtime_asr", "draft": {"provider": "auto"}},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_settings_require_auth(client):
    assert client.get("/api/settings").status_code == 401
    assert client.put("/api/settings", json={"system": {"auto_process": True}}).status_code == 401
    assert client.post("/api/settings/test", json={"kind": "llm"}).status_code == 401


def test_settings_mutations_require_admin(client, db):
    headers = _login_as_non_admin(client, db)
    # v0.2: the whole settings surface (including GET masks) is admin-only.
    assert client.get("/api/settings", headers=headers).status_code == 403
    assert (
        client.put("/api/settings", json={"system": {"auto_process": True}}, headers=headers).status_code
        == 403
    )
    assert client.post("/api/settings/test", json={"kind": "llm"}, headers=headers).status_code == 403
    assert client.post("/api/settings/prompts/reset", headers=headers).status_code == 403
