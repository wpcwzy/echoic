"""API tests for the v1.0 audit log (task-27).

Covers the admin audit-log API, the mutation hooks sprinkled across routers, the
sanitization guarantees (no secrets) and the best-effort behaviour (audit
failures never break business operations).
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.core.utils import load_json, utcnow
from app.models import AuditLog
from app.services import audit as audit_module
from tests.test_api_helpers import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    api_request,
    clear_login_throttle,
    create_user,
    ffmpeg_available,
    get_user,
    login_token,
    make_recording,
    make_wav_bytes,
)


@pytest.fixture(autouse=True)
def _reset_throttle():
    clear_login_throttle()
    yield
    clear_login_throttle()


def _admin_token(client) -> str:
    return login_token(client, ADMIN_USERNAME, ADMIN_PASSWORD)


def _audit_page(client, token, **params) -> dict:
    response = api_request(client, token, "get", "/api/system/audit-logs", params=params or None)
    assert response.status_code == 200, response.text
    return response.json()


def _actions(client, token, **params) -> list[str]:
    params.setdefault("page_size", 200)
    return [item["action"] for item in _audit_page(client, token, **params)["items"]]


def _admin_id(db) -> int:
    user = get_user(db, ADMIN_USERNAME)
    assert user is not None
    return user.id


# ================================================================== endpoint basics
def test_audit_logs_require_auth(client):
    response = client.get("/api/system/audit-logs")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_audit_logs_member_forbidden(client, db):
    create_user(db, "audimember", "password123")
    token = login_token(client, "audimember", "password123")
    response = api_request(client, token, "get", "/api/system/audit-logs")
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_audit_list_shape_and_order(client, db):
    token = _admin_token(client)  # -> auth.login
    assert (
        api_request(client, token, "post", "/api/tags", json={"name": "顺序一"}).status_code == 201
    )  # -> tag.create
    assert (
        api_request(
            client, token, "put", "/api/settings", json={"system": {"auto_process": True}}
        ).status_code
        == 200
    )  # -> settings.update

    body = _audit_page(client, token, page_size=200)
    assert body["total"] >= 3
    item = body["items"][0]
    for key in (
        "id",
        "user_id",
        "username",
        "action",
        "target_type",
        "target_id",
        "detail",
        "ip",
        "created_at",
    ):
        assert key in item
    assert isinstance(item["detail"], dict)
    assert item["created_at"]
    assert [entry["action"] for entry in body["items"][:3]] == [
        "settings.update",
        "tag.create",
        "auth.login",
    ]
    ids = [entry["id"] for entry in body["items"]]
    assert ids == sorted(ids, reverse=True)


def test_audit_filters_and_pagination(client, db):
    token = _admin_token(client)
    admin_id = _admin_id(db)
    first = api_request(client, token, "post", "/api/tags", json={"name": "过滤一"})
    second = api_request(client, token, "post", "/api/tags", json={"name": "过滤二"})
    assert first.status_code == 201 and second.status_code == 201
    api_request(client, token, "patch", f"/api/tags/{first.json()['id']}", json={"color": "#ff0000"})

    by_action = _audit_page(client, token, action="tag.create")
    assert by_action["total"] == 2
    by_target = _audit_page(client, token, target_type="tag")
    assert by_target["total"] == 3
    by_user = _audit_page(client, token, user_id=admin_id)
    assert by_user["total"] >= 3
    by_q_action = _audit_page(client, token, q="tag.")
    assert by_q_action["total"] >= 3
    by_q_detail = _audit_page(client, token, q="过滤二")
    assert by_q_detail["total"] >= 1
    by_q_username = _audit_page(client, token, q=ADMIN_USERNAME)
    assert by_q_username["total"] >= 3

    since = (utcnow() - timedelta(minutes=10)).isoformat()
    everything = _audit_page(client, token, since=since, page_size=200)
    assert everything["total"] >= 4
    until = (utcnow() - timedelta(days=1)).isoformat()
    assert _audit_page(client, token, until=until)["total"] == 0

    page_one = _audit_page(client, token, page=1, page_size=2)
    page_two = _audit_page(client, token, page=2, page_size=2)
    assert len(page_one["items"]) == 2
    assert page_one["total"] == page_two["total"] >= 4
    assert {item["id"] for item in page_one["items"]}.isdisjoint(
        {item["id"] for item in page_two["items"]}
    )
    assert len(everything["items"]) <= 200

    for params in ({"page": 0}, {"page_size": 0}, {"page_size": 201}):
        response = api_request(client, token, "get", "/api/system/audit-logs", params=params)
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"


# ================================================================== auth hooks
def test_audit_login_entries_without_passwords(client, db):
    admin_id = _admin_id(db)
    login_token(client, ADMIN_USERNAME, ADMIN_PASSWORD)  # success
    clear_login_throttle()
    bad = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": "super-secret-wrong"}
    )
    assert bad.status_code == 401
    token = _admin_token(client)

    items = _audit_page(client, token, q="login", page_size=200)["items"]
    success = [item for item in items if item["action"] == "auth.login"]
    failed = [item for item in items if item["action"] == "auth.login_failed"]
    assert success
    assert success[0]["target_type"] == "user"
    assert success[0]["target_id"] == admin_id
    assert success[0]["username"] == ADMIN_USERNAME
    assert failed
    assert failed[0]["detail"]["reason"] == "invalid_credentials"
    serialized = json.dumps(items, ensure_ascii=False)
    assert "super-secret-wrong" not in serialized
    assert ADMIN_PASSWORD not in serialized


def test_audit_profile_and_password_hooks(client, db):
    token = _admin_token(client)
    profile = api_request(
        client, token, "patch", "/api/auth/me", json={"display_name": "审计管理员"}
    )
    assert profile.status_code == 200
    changed = api_request(
        client,
        token,
        "post",
        "/api/auth/change-password",
        json={"old_password": ADMIN_PASSWORD, "new_password": "newpass789"},
    )
    assert changed.status_code == 200

    items = _audit_page(client, token, page_size=200)["items"]
    actions = [item["action"] for item in items]
    assert "auth.profile_update" in actions
    assert "auth.change_password" in actions
    profile_entry = next(item for item in items if item["action"] == "auth.profile_update")
    assert profile_entry["target_type"] == "user"
    assert profile_entry["detail"]["fields"] == ["display_name"]
    serialized = json.dumps(items, ensure_ascii=False)
    assert "newpass789" not in serialized
    assert ADMIN_PASSWORD not in serialized


def test_audit_forwarded_for_ip(client, db):
    login = client.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )
    assert login.status_code == 200
    token = login.json()["token"]
    items = _audit_page(client, token, action="auth.login")["items"]
    assert items
    assert items[0]["ip"] == "203.0.113.7"


# ================================================================== resource hooks
def test_audit_recording_lifecycle_hooks(client, db):
    token = _admin_token(client)
    recording = make_recording(db, title="审计录音", owner_id=_admin_id(db))
    recording_id = recording.id

    patched = api_request(
        client, token, "patch", f"/api/recordings/{recording_id}", json={"title": "审计录音改"}
    )
    assert patched.status_code == 200
    reprocessed = api_request(
        client,
        token,
        "post",
        f"/api/recordings/{recording_id}/reprocess",
        json={"from": "insights"},
    )
    assert reprocessed.status_code == 200
    assert api_request(client, token, "delete", f"/api/recordings/{recording_id}").status_code == 200
    assert (
        api_request(client, token, "post", f"/api/recordings/{recording_id}/restore").status_code
        == 200
    )
    assert api_request(client, token, "delete", f"/api/recordings/{recording_id}").status_code == 200
    purged = api_request(client, token, "delete", f"/api/recordings/{recording_id}/purge")
    assert purged.status_code == 200

    # GETs must not create audit entries
    total_before_gets = _audit_page(client, token)["total"]
    api_request(client, token, "get", f"/api/recordings/{recording_id}")
    api_request(client, token, "get", "/api/recordings")
    api_request(client, token, "get", "/api/dashboard")
    api_request(client, token, "get", "/api/system/audit-logs")
    assert _audit_page(client, token)["total"] == total_before_gets

    items = _audit_page(client, token, target_type="recording", page_size=200)["items"]
    actions = {item["action"] for item in items}
    assert {
        "recording.update",
        "recording.reprocess",
        "recording.trash",
        "recording.restore",
        "recording.purge",
    } <= actions
    assert {item["target_id"] for item in items} == {recording_id}
    reprocess_entry = next(item for item in items if item["action"] == "recording.reprocess")
    assert reprocess_entry["detail"]["from"] == "insights"


def test_audit_share_hooks(client, db):
    token = _admin_token(client)
    recording = make_recording(db, title="分享审计", owner_id=_admin_id(db))
    created = api_request(
        client,
        token,
        "post",
        f"/api/recordings/{recording.id}/shares",
        json={"password": "sharepass123"},
    )
    assert created.status_code == 201, created.text
    share = created.json()
    assert api_request(client, token, "delete", f"/api/shares/{share['id']}").status_code == 200
    # idempotent second revoke still records an audit entry
    assert api_request(client, token, "delete", f"/api/shares/{share['id']}").status_code == 200

    items = _audit_page(client, token, target_type="share", page_size=200)["items"]
    creates = [item for item in items if item["action"] == "share.create"]
    revokes = [item for item in items if item["action"] == "share.revoke"]
    assert len(creates) == 1 and creates[0]["target_id"] == share["id"]
    assert creates[0]["detail"]["recording_id"] == recording.id
    assert creates[0]["detail"]["has_password"] is True
    assert len(revokes) == 2
    assert revokes[0]["detail"]["already_revoked"] is True
    assert revokes[1]["detail"]["already_revoked"] is False
    serialized = json.dumps(items, ensure_ascii=False)
    assert "sharepass123" not in serialized


def test_audit_settings_and_tag_hooks_do_not_leak_secrets(client, db):
    token = _admin_token(client)
    secret = "sk-audit-secret-123456"
    tag = api_request(client, token, "post", "/api/tags", json={"name": "审计标签"}).json()
    api_request(client, token, "patch", f"/api/tags/{tag['id']}", json={"color": "#00ff00"})
    api_request(client, token, "delete", f"/api/tags/{tag['id']}")
    updated = api_request(
        client,
        token,
        "put",
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
    assert updated.status_code == 200
    assert api_request(client, token, "post", "/api/settings/prompts/reset").status_code == 200

    page = api_request(client, token, "get", "/api/system/audit-logs", params={"page_size": 200})
    assert page.status_code == 200
    serialized = page.text
    assert secret not in serialized  # never persisted/logged
    actions = [item["action"] for item in page.json()["items"]]
    for expected in ("tag.create", "tag.update", "tag.delete", "settings.update", "settings.prompts_reset"):
        assert expected in actions
    settings_entry = next(
        item for item in page.json()["items"] if item["action"] == "settings.update"
    )
    assert settings_entry["detail"]["sections"] == ["ai"]
    tag_delete = next(item for item in page.json()["items"] if item["action"] == "tag.delete")
    assert tag_delete["detail"]["name"] == "审计标签"


def test_audit_user_hooks(client, db):
    token = _admin_token(client)
    created = api_request(
        client,
        token,
        "post",
        "/api/users",
        json={"username": "audituser", "password": "password123", "role": "member"},
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    patched = api_request(
        client, token, "patch", f"/api/users/{user_id}", json={"display_name": "审计用户"}
    )
    assert patched.status_code == 200
    reset = api_request(
        client,
        token,
        "post",
        f"/api/users/{user_id}/reset-password",
        json={"new_password": "brandnew456"},
    )
    assert reset.status_code == 200
    deleted = api_request(client, token, "delete", f"/api/users/{user_id}")
    assert deleted.status_code == 200

    items = _audit_page(client, token, target_type="user", page_size=200)["items"]
    actions = {item["action"] for item in items}
    assert {"user.create", "user.update", "user.reset_password", "user.delete"} <= actions
    create_entry = next(item for item in items if item["action"] == "user.create")
    assert create_entry["target_id"] == user_id
    assert create_entry["detail"]["username"] == "audituser"
    delete_entry = next(item for item in items if item["action"] == "user.delete")
    assert delete_entry["detail"]["username"] == "audituser"
    serialized = json.dumps(items, ensure_ascii=False)
    assert "password123" not in serialized
    assert "brandnew456" not in serialized


def test_audit_upload_hook(client, db):
    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    token = _admin_token(client)
    payload = make_wav_bytes(duration=0.4)
    uploaded = api_request(
        client,
        token,
        "post",
        "/api/recordings/upload",
        files={"file": ("审计上传.wav", payload, "audio/wav")},
        data={"title": "审计上传"},
    )
    assert uploaded.status_code == 201, uploaded.text
    recording_id = uploaded.json()["id"]
    items = _audit_page(client, token, action="recording.upload")["items"]
    assert len(items) == 1
    assert items[0]["target_id"] == recording_id
    assert items[0]["detail"]["title"] == "审计上传"
    assert items[0]["detail"]["size_bytes"] == len(payload)
    assert items[0]["detail"]["media_type"] == "audio"


def test_audit_batch_hook(client, db):
    token = _admin_token(client)
    first = make_recording(db, title="批量一", owner_id=_admin_id(db))
    second = make_recording(db, title="批量二", owner_id=_admin_id(db))
    response = api_request(
        client,
        token,
        "post",
        "/api/recordings/batch",
        json={"action": "trash", "recording_ids": [first.id, second.id, 9999]},
    )
    assert response.status_code == 200
    items = _audit_page(client, token, action="recording.batch")["items"]
    assert len(items) == 1
    detail = items[0]["detail"]
    assert detail["action"] == "trash"
    assert detail["succeeded"] == 2
    assert detail["failed"] == 1
    assert detail["recording_ids"] == [first.id, second.id, 9999]


def test_audit_system_hooks(client, db):
    token = _admin_token(client)
    scan = api_request(client, token, "post", "/api/system/consume/scan")
    assert scan.status_code in (200, 503)
    seed = api_request(client, token, "post", "/api/system/seed-demo")
    assert seed.status_code == 200
    actions = _actions(client, token, page_size=200)
    # seed demo always records; consume scan entry only when the service is available
    assert "system.seed_demo" in actions
    if scan.status_code == 200:
        assert "system.consume_scan" in actions


# ================================================================== robustness
def test_audit_failure_never_breaks_operations(client, db, monkeypatch):
    token = _admin_token(client)

    def boom(*args, **kwargs):
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(audit_module, "record_audit", boom)

    tag = api_request(client, token, "post", "/api/tags", json={"name": "坏审计"})
    assert tag.status_code == 201
    recording = make_recording(db, title="坏审计录音", owner_id=_admin_id(db))
    assert api_request(client, token, "delete", f"/api/recordings/{recording.id}").status_code == 200
    assert (
        api_request(
            client, token, "put", "/api/settings", json={"system": {"auto_process": True}}
        ).status_code
        == 200
    )
    assert db.query(AuditLog).filter(AuditLog.action == "tag.create").count() == 0


def test_audit_service_sanitizes_detail_and_tolerates_anonymous(db):
    row = audit_module.record_audit(
        db,
        None,
        "test.sanitize",
        target_type="test",
        target_id="42",
        detail={
            "password": "p",
            "api_key": "k",
            "token": "t",
            "authorization": "Bearer abc",
            "cookie": "c",
            "nested": {"secret": "s"},
            "note": "很" * 400,
            "items": list(range(80)),
            "has_password": True,
            "has_api_key": False,
            "bad": object(),
        },
        ip="198.51.100.4",
    )
    assert row is not None
    db.expire_all()
    stored = db.query(AuditLog).filter(AuditLog.id == row.id).one()
    assert stored.user_id is None
    assert stored.username == ""
    assert stored.target_id == 42
    assert stored.ip == "198.51.100.4"
    detail = load_json(stored.detail_json, {})
    assert detail["password"] == "[redacted]"
    assert detail["api_key"] == "[redacted]"
    assert detail["token"] == "[redacted]"
    assert detail["authorization"] == "[redacted]"
    assert detail["cookie"] == "[redacted]"
    assert detail["has_password"] is True  # boolean flags are not secrets
    assert detail["has_api_key"] is False
    assert detail["nested"]["secret"] == "[redacted]"
    assert len(detail["note"]) <= 300
    assert detail["items"][-1].endswith("more)")
    assert isinstance(detail["bad"], str)

    # a broken session/db never raises from the service
    assert audit_module.record_audit(None, None, "test.no_db") is None  # type: ignore[arg-type]
