"""API tests for v0.2: multi-user roles (R-18) + share links (R-17).

Contract: docs/08-P1-协作与分享设计.md §2/§4.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.utils import utcnow
from app.models import Share, User
from tests.test_api_helpers import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    api_request,
    clear_login_throttle,
    clear_share_throttle,
    create_user,
    ffmpeg_available,
    get_user,
    login_token,
    make_recording,
    make_share,
    make_wav_bytes,
    public_request,
    seed_ready_recording,
    write_media,
)


@pytest.fixture(autouse=True)
def _reset_throttles():
    clear_login_throttle()
    clear_share_throttle()
    yield
    clear_login_throttle()
    clear_share_throttle()


def _admin(db) -> User:
    user = get_user(db, ADMIN_USERNAME)
    assert user is not None
    return user


def _member(client, db, username: str = "member1", password: str = "memberpass123"):
    user = create_user(db, username, password, display_name="成员一号")
    token = login_token(client, username, password)
    return user, token


def _admin_token(client) -> str:
    return login_token(client, ADMIN_USERNAME, ADMIN_PASSWORD)


def _recording_with_media(db, *, owner_id: int | None, title: str = "团队周会", **overrides):
    recording = seed_ready_recording(
        db, title=title, owner_id=owner_id, with_insights=True, **overrides
    )
    write_media(recording, original=b"ORIGINAL-BYTES", playback=b"PLAYBACK-BYTES" * 40)
    return recording


# ================================================================== users (admin)
def test_admin_lists_users_with_counts(client, db):
    admin = _admin(db)
    member = create_user(
        db, "alice", "password123", display_name="Alice", email="alice@example.com"
    )
    recording = make_recording(db, title="Alice 的会议", owner_id=member.id)
    make_share(db, recording, created_by=member.id)
    token = _admin_token(client)

    response = api_request(client, token, "get", "/api/users")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    alice = next(item for item in body["items"] if item["username"] == "alice")
    assert alice["role"] == "member"
    assert alice["is_admin"] is False
    assert alice["disabled"] is False
    assert alice["email"] == "alice@example.com"
    assert alice["recording_count"] == 1
    assert alice["share_count"] == 1
    assert alice["created_at"]
    assert {"id", "username", "display_name", "last_login_at"} <= set(alice.keys())

    filtered = api_request(client, token, "get", "/api/users", params={"query": "alice"})
    assert filtered.json()["total"] == 1
    only_admins = api_request(client, token, "get", "/api/users", params={"role": "admin"})
    assert only_admins.json()["total"] == 1
    assert only_admins.json()["items"][0]["username"] == "admin"


def test_create_user_and_login(client, db):
    token = _admin_token(client)
    created = api_request(
        client,
        token,
        "post",
        "/api/users",
        json={
            "username": "bob",
            "password": "bobpass123",
            "display_name": "Bob",
            "email": "bob@example.com",
            "role": "member",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["username"] == "bob"
    assert body["role"] == "member"
    assert body["is_admin"] is False
    assert body["recording_count"] == 0
    assert body["share_count"] == 0

    duplicate = api_request(
        client, token, "post", "/api/users", json={"username": "bob", "password": "bobpass123"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "CONFLICT"

    too_short = api_request(
        client, token, "post", "/api/users", json={"username": "carl", "password": "short"}
    )
    assert too_short.status_code == 422

    login = client.post("/api/auth/login", json={"username": "bob", "password": "bobpass123"})
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "member"


def test_patch_user_fields(client, db):
    token = _admin_token(client)
    member = create_user(db, "dana", "password123")
    updated = api_request(
        client,
        token,
        "patch",
        f"/api/users/{member.id}",
        json={"display_name": "Dana 新名", "email": "dana@example.com"},
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Dana 新名"
    assert updated.json()["email"] == "dana@example.com"

    promoted = api_request(client, token, "patch", f"/api/users/{member.id}", json={"role": "admin"})
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"
    assert promoted.json()["is_admin"] is True

    disabled = api_request(
        client, token, "patch", f"/api/users/{member.id}", json={"disabled": True}
    )
    assert disabled.status_code == 200
    assert disabled.json()["disabled"] is True

    blocked = client.post("/api/auth/login", json={"username": "dana", "password": "password123"})
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "FORBIDDEN"
    assert "停用" in blocked.json()["detail"]


def test_last_admin_protection(client, db):
    admin = _admin(db)
    token = _admin_token(client)
    demote = api_request(client, token, "patch", f"/api/users/{admin.id}", json={"role": "member"})
    assert demote.status_code == 400
    assert demote.json()["code"] == "VALIDATION_ERROR"
    disable_self = api_request(
        client, token, "patch", f"/api/users/{admin.id}", json={"disabled": True}
    )
    assert disable_self.status_code == 400
    delete_self = api_request(client, token, "delete", f"/api/users/{admin.id}")
    assert delete_self.status_code == 400


def test_delete_user_rules(client, db):
    token = _admin_token(client)
    member = create_user(db, "erin", "password123")
    recording = make_recording(db, title="erin 的录音", owner_id=member.id)
    blocked = api_request(client, token, "delete", f"/api/users/{member.id}")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "CONFLICT"
    assert "转移" in blocked.json()["detail"]

    reassigned = api_request(
        client, token, "patch", f"/api/recordings/{recording.id}", json={"owner_id": _admin(db).id}
    )
    assert reassigned.status_code == 200
    assert reassigned.json()["owner_id"] == _admin(db).id

    deleted = api_request(client, token, "delete", f"/api/users/{member.id}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    assert get_user(db, "erin") is None
    missing = api_request(client, token, "delete", "/api/users/9999")
    assert missing.status_code == 404


def test_delete_user_keeps_shares_but_clears_creator(client, db):
    token = _admin_token(client)
    admin = _admin(db)
    member = create_user(db, "frank", "password123")
    recording = make_recording(db, title="frank 的录音", owner_id=member.id)
    share = make_share(db, recording, created_by=member.id)
    reassign = api_request(
        client, token, "patch", f"/api/recordings/{recording.id}", json={"owner_id": admin.id}
    )
    assert reassign.status_code == 200

    deleted = api_request(client, token, "delete", f"/api/users/{member.id}")
    assert deleted.status_code == 200
    db.expire_all()
    stored = db.get(Share, share.id)
    assert stored is not None and stored.created_by is None


def test_reset_password(client, db):
    token = _admin_token(client)
    member = create_user(db, "gina", "password123")
    reset = api_request(
        client,
        token,
        "post",
        f"/api/users/{member.id}/reset-password",
        json={"new_password": "brandnew123"},
    )
    assert reset.status_code == 200
    assert reset.json()["ok"] is True

    clear_login_throttle()
    assert (
        client.post(
            "/api/auth/login", json={"username": "gina", "password": "brandnew123"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/login", json={"username": "gina", "password": "password123"}
        ).status_code
        == 401
    )


def test_non_admin_cannot_manage_users(client, db):
    _, token = _member(client, db)
    assert api_request(client, token, "get", "/api/users").status_code == 403
    assert (
        api_request(
            client, token, "post", "/api/users", json={"username": "x", "password": "password123"}
        ).status_code
        == 403
    )
    assert api_request(client, token, "patch", "/api/users/1", json={"role": "member"}).status_code == 403
    assert api_request(client, token, "delete", "/api/users/1").status_code == 403


def test_disabled_user_session_invalidated(client, db):
    member, token = _member(client, db)
    admin_token = _admin_token(client)
    response = api_request(
        client, admin_token, "patch", f"/api/users/{member.id}", json={"disabled": True}
    )
    assert response.status_code == 200
    assert api_request(client, token, "get", "/api/recordings").status_code == 401


def test_login_updates_last_login(client, db):
    token = _admin_token(client)
    users = api_request(client, token, "get", "/api/users")
    admin_row = next(item for item in users.json()["items"] if item["username"] == "admin")
    assert admin_row["last_login_at"] is not None


def test_disabled_login_rejected(client, db):
    create_user(db, "henry", "password123", disabled=True)
    response = client.post(
        "/api/auth/login", json={"username": "henry", "password": "password123"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert "停用" in response.json()["detail"]


# ================================================================== registration
def test_user_list_include_disabled_filter(client, db):
    token = _admin_token(client)
    create_user(db, "disabled1", "password123", disabled=True)
    everyone = api_request(client, token, "get", "/api/users")
    assert everyone.json()["total"] == 2
    active_only = api_request(
        client, token, "get", "/api/users", params={"include_disabled": False}
    )
    assert active_only.json()["total"] == 1
    assert active_only.json()["items"][0]["username"] == "admin"


def test_admin_can_demote_self_when_another_admin_exists(client, db):
    admin = _admin(db)
    token = _admin_token(client)
    create_user(db, "second_admin", "password123", role="admin")
    demoted = api_request(
        client, token, "patch", f"/api/users/{admin.id}", json={"role": "member"}
    )
    assert demoted.status_code == 200
    assert demoted.json()["role"] == "member"
    assert demoted.json()["is_admin"] is False


def test_register_flow_and_gate(client, db):
    closed = client.post(
        "/api/auth/register",
        json={"username": "newbie", "password": "password123", "display_name": "新人"},
    )
    assert closed.status_code == 403
    assert closed.json()["code"] == "FORBIDDEN"

    admin_token = _admin_token(client)
    enabled = api_request(
        client,
        admin_token,
        "put",
        "/api/settings",
        json={"system": {"allow_registration": True}},
    )
    assert enabled.status_code == 200
    assert enabled.json()["system"]["allow_registration"] is True

    registered = client.post(
        "/api/auth/register",
        json={"username": "newbie", "password": "password123", "display_name": "新人"},
    )
    assert registered.status_code == 201, registered.text
    body = registered.json()
    assert body["username"] == "newbie"
    assert body["role"] == "member"
    assert body["is_admin"] is False

    duplicate = client.post(
        "/api/auth/register", json={"username": "newbie", "password": "password123"}
    )
    assert duplicate.status_code == 409

    short = client.post(
        "/api/auth/register", json={"username": "shorty", "password": "short"}
    )
    assert short.status_code == 422

    token = login_token(client, "newbie", "password123")
    me = api_request(client, token, "get", "/api/auth/me")
    assert me.json()["user"]["role"] == "member"

    api_request(
        client, admin_token, "put", "/api/settings", json={"system": {"allow_registration": False}}
    )
    blocked = client.post(
        "/api/auth/register", json={"username": "later", "password": "password123"}
    )
    assert blocked.status_code == 403


# ================================================================== permission matrix
def test_member_cannot_reach_admin_recording(client, db):
    admin = _admin(db)
    member, token = _member(client, db)
    admin_recording = _recording_with_media(
        db, owner_id=admin.id, title="TraceOpt paper discussion", with_transcript=True
    )
    own = make_recording(db, title="我自己的会议", owner_id=member.id)

    listing = api_request(client, token, "get", "/api/recordings")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert [item["id"] for item in listing.json()["items"]] == [own.id]

    detail = api_request(client, token, "get", f"/api/recordings/{admin_recording.id}")
    assert detail.status_code == 404
    assert detail.json()["code"] == "NOT_FOUND"

    endpoints = [
        ("get", f"/api/recordings/{admin_recording.id}/media"),
        ("get", f"/api/recordings/{admin_recording.id}/transcript"),
        ("get", f"/api/recordings/{admin_recording.id}/insights"),
        ("get", f"/api/recordings/{admin_recording.id}/chat"),
        ("get", f"/api/recordings/{admin_recording.id}/jobs"),
        ("get", f"/api/recordings/{admin_recording.id}/shares"),
        ("post", f"/api/recordings/{admin_recording.id}/chat"),
        ("post", f"/api/recordings/{admin_recording.id}/reprocess"),
        ("post", f"/api/recordings/{admin_recording.id}/cancel"),
        ("patch", f"/api/recordings/{admin_recording.id}"),
        ("delete", f"/api/recordings/{admin_recording.id}"),
    ]
    for method, path in endpoints:
        kwargs = {}
        if method == "patch":
            kwargs["json"] = {}
        elif method == "post" and path.endswith("/chat"):
            kwargs["json"] = {"message": "hi"}
        response = api_request(client, token, method, path, **kwargs)
        assert response.status_code == 404, f"{method} {path} -> {response.status_code}"
        assert response.json()["code"] == "NOT_FOUND"

    own_detail = api_request(client, token, "get", f"/api/recordings/{own.id}")
    assert own_detail.status_code == 200


def test_member_manages_own_recording_only(client, db):
    admin = _admin(db)
    member, token = _member(client, db)
    own = make_recording(db, title="我的会议", owner_id=member.id)
    admin_recording = make_recording(db, title="管理员会议", owner_id=admin.id)

    updated = api_request(
        client, token, "patch", f"/api/recordings/{own.id}", json={"title": "改名后的会议"}
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "改名后的会议"

    transfer = api_request(
        client, token, "patch", f"/api/recordings/{own.id}", json={"owner_id": admin.id}
    )
    assert transfer.status_code == 403
    assert transfer.json()["code"] == "FORBIDDEN"

    assert (
        api_request(
            client, token, "patch", f"/api/recordings/{admin_recording.id}", json={"title": "x"}
        ).status_code
        == 404
    )
    deleted = api_request(client, token, "delete", f"/api/recordings/{own.id}")
    assert deleted.status_code == 200
    assert api_request(client, token, "delete", f"/api/recordings/{own.id}").status_code == 404


def test_admin_reassigns_recording_owner(client, db):
    admin = _admin(db)
    member = create_user(db, "iris", "password123")
    token = _admin_token(client)
    recording = make_recording(db, title="待转移录音", owner_id=admin.id)

    reassigned = api_request(
        client, token, "patch", f"/api/recordings/{recording.id}", json={"owner_id": member.id}
    )
    assert reassigned.status_code == 200
    body = reassigned.json()
    assert body["owner_id"] == member.id
    assert body["owner"]["username"] == "iris"

    member_token = login_token(client, "iris", "password123")
    assert (
        api_request(client, member_token, "get", f"/api/recordings/{recording.id}").status_code
        == 200
    )

    invalid = api_request(
        client, token, "patch", f"/api/recordings/{recording.id}", json={"owner_id": 9999}
    )
    assert invalid.status_code == 400

    cleared = api_request(
        client, token, "patch", f"/api/recordings/{recording.id}", json={"owner_id": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["owner_id"] is None


def test_list_and_dashboard_include_owner_info(client, db):
    admin = _admin(db)
    member = create_user(db, "jack", "password123")
    make_recording(db, title="jack 的会议", owner_id=member.id)
    token = _admin_token(client)
    listing = api_request(client, token, "get", "/api/recordings")
    item = listing.json()["items"][0]
    assert item["owner_id"] == member.id
    assert item["owner"]["username"] == "jack"

    dashboard = api_request(client, token, "get", "/api/dashboard")
    assert dashboard.json()["stats"]["total"] == 1


def test_member_search_and_dashboard_scoped(client, db):
    admin = _admin(db)
    member, token = _member(client, db)
    _recording_with_media(db, owner_id=admin.id, title="管理员会议", with_transcript=True)
    own = _recording_with_media(db, owner_id=member.id, title="成员会议", with_transcript=True)

    search = api_request(client, token, "get", "/api/search", params={"q": "会议"})
    assert search.status_code == 200
    body = search.json()
    assert [item["id"] for item in body["recordings"]] == [own.id]
    assert all(hit["recording_id"] == own.id for hit in body["segments"])

    dashboard = api_request(client, token, "get", "/api/dashboard")
    dash = dashboard.json()
    assert dash["stats"]["total"] == 1
    assert [item["id"] for item in dash["recent"]] == [own.id]
    assert dash["storage"]["recordings"] == 1


def test_member_tag_catalogue_scoped_counts(client, db):
    from tests.test_api_helpers import add_tag, attach_tag

    admin = _admin(db)
    member, token = _member(client, db)
    tag = add_tag(db, "组会")
    attach_tag(db, make_recording(db, title="管理员录音", owner_id=admin.id), tag)
    attach_tag(db, make_recording(db, title="成员录音", owner_id=member.id), tag)

    member_tags = api_request(client, token, "get", "/api/tags").json()["items"]
    assert member_tags[0]["recording_count"] == 1

    admin_token = _admin_token(client)
    admin_tags = api_request(client, admin_token, "get", "/api/tags").json()["items"]
    assert admin_tags[0]["recording_count"] == 2


def test_member_cannot_use_admin_surfaces(client, db):
    from tests.test_api_helpers import add_tag

    member, token = _member(client, db)
    tag = add_tag(db, "标签")
    assert api_request(client, token, "get", "/api/settings").status_code == 403
    assert (
        api_request(client, token, "put", "/api/settings", json={"system": {"auto_process": True}}).status_code
        == 403
    )
    assert api_request(client, token, "post", "/api/settings/test", json={"kind": "llm"}).status_code == 403
    assert api_request(client, token, "post", "/api/settings/prompts/reset").status_code == 403
    assert api_request(client, token, "get", "/api/users").status_code == 403
    assert api_request(client, token, "get", "/api/system/info").status_code == 403
    assert api_request(client, token, "get", "/api/system/logs").status_code == 403
    assert api_request(client, token, "post", "/api/system/seed-demo").status_code == 403
    assert api_request(client, token, "post", "/api/system/consume/scan").status_code == 403
    # Members may create custom tags, but cannot rename/delete the global catalogue.
    created = api_request(client, token, "post", "/api/tags", json={"name": "成员新标签"})
    assert created.status_code == 201
    assert created.json()["name"] == "成员新标签"
    assert api_request(client, token, "patch", f"/api/tags/{tag.id}", json={"name": "改名"}).status_code == 403
    assert api_request(client, token, "delete", f"/api/tags/{tag.id}").status_code == 403
    # tag catalogue stays readable for members
    assert api_request(client, token, "get", "/api/tags").status_code == 200


def test_upload_assigns_owner(client, db, monkeypatch):
    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    from app.services import queue as queue_module

    monkeypatch.setattr(queue_module, "enqueue", lambda db, recording_id, from_="all": None)
    member, token = _member(client, db)
    payload = make_wav_bytes(duration=0.5)
    response = api_request(
        client,
        token,
        "post",
        "/api/recordings/upload",
        files={"file": ("成员上传.wav", payload, "audio/wav")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["owner_id"] == member.id
    assert body["owner"]["username"] == member.username


# ================================================================== shares (authenticated)
def test_create_and_list_recording_shares(client, db):
    admin = _admin(db)
    token = _admin_token(client)
    recording = make_recording(db, title="分享测试会议", owner_id=admin.id)

    created = api_request(
        client,
        token,
        "post",
        f"/api/recordings/{recording.id}/shares",
        json={
            "password": "sharepass123",
            "expires_at": "2027-01-01T00:00:00Z",
            "include_transcript": True,
            "include_insights": False,
            "allow_download": True,
        },
    )
    assert created.status_code == 201, created.text
    share = created.json()
    assert share["recording_id"] == recording.id
    assert share["token"]
    assert share["url_path"] == f"/s/{share['token']}"
    assert share["has_password"] is True
    assert share["include_transcript"] is True
    assert share["include_insights"] is False
    assert share["allow_download"] is True
    assert share["expired"] is False
    assert share["revoked_at"] is None
    assert share["view_count"] == 0
    assert share["created_by"]["id"] == admin.id

    listed = api_request(client, token, "get", f"/api/recordings/{recording.id}/shares")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    all_shares = api_request(
        client, token, "get", "/api/shares", params={"recording_id": recording.id}
    )
    assert all_shares.status_code == 200
    assert all_shares.json()["total"] == 1

    db.expire_all()
    stored = db.get(Share, share["id"])
    assert stored is not None
    assert stored.password_hash and stored.password_hash.startswith("$2")
    assert "sharepass123" not in stored.password_hash


def test_share_create_validation(client, db):
    admin = _admin(db)
    token = _admin_token(client)
    recording = make_recording(db, title="校验会议", owner_id=admin.id)
    past = api_request(
        client,
        token,
        "post",
        f"/api/recordings/{recording.id}/shares",
        json={"expires_at": "2020-01-01T00:00:00Z"},
    )
    assert past.status_code == 400
    assert past.json()["code"] == "VALIDATION_ERROR"

    short_password = api_request(
        client,
        token,
        "post",
        f"/api/recordings/{recording.id}/shares",
        json={"password": "abc"},
    )
    assert short_password.status_code == 422


def test_share_permissions(client, db):
    admin = _admin(db)
    member, member_token = _member(client, db)
    own = make_recording(db, title="成员录音", owner_id=member.id)
    admin_recording = make_recording(db, title="管理员录音", owner_id=admin.id)

    own_share = api_request(
        client, member_token, "post", f"/api/recordings/{own.id}/shares", json={}
    )
    assert own_share.status_code == 201
    assert own_share.json()["has_password"] is False
    opposite = api_request(
        client, member_token, "post", f"/api/recordings/{admin_recording.id}/shares", json={}
    )
    assert opposite.status_code == 404

    admin_token = _admin_token(client)
    admin_share = api_request(
        client, admin_token, "post", f"/api/recordings/{admin_recording.id}/shares", json={}
    ).json()
    forbidden_revoke = api_request(
        client, member_token, "delete", f"/api/shares/{admin_share['id']}"
    )
    assert forbidden_revoke.status_code == 404

    member_shares = api_request(client, member_token, "get", "/api/shares")
    assert member_shares.json()["total"] == 1
    admin_shares = api_request(client, admin_token, "get", "/api/shares")
    assert admin_shares.json()["total"] == 2

    # admin may revoke anyone's share
    assert api_request(client, admin_token, "delete", f"/api/shares/{own_share.json()['id']}").status_code == 200


def test_revoke_share_is_idempotent_and_public_404(client, db):
    admin = _admin(db)
    token = _admin_token(client)
    recording = make_recording(db, title="撤销分享", owner_id=admin.id)
    share = api_request(
        client, token, "post", f"/api/recordings/{recording.id}/shares", json={}
    ).json()

    public_request(client, "get", f"/api/public/shares/{share['token']}")  # before revoke: 200
    revoked = api_request(client, token, "delete", f"/api/shares/{share['id']}")
    assert revoked.status_code == 200
    again = api_request(client, token, "delete", f"/api/shares/{share['id']}")
    assert again.status_code == 200

    active = api_request(client, token, "get", "/api/shares")
    assert active.json()["total"] == 0
    with_revoked = api_request(
        client, token, "get", "/api/shares", params={"include_revoked": True}
    )
    assert with_revoked.json()["total"] == 1
    assert with_revoked.json()["items"][0]["revoked_at"] is not None
    assert public_request(client, "get", f"/api/public/shares/{share['token']}").status_code == 404


# ================================================================== public share (no auth)
def test_public_metadata_without_auth(client, db):
    from tests.test_api_helpers import add_speakers, add_tag, attach_tag

    recording = make_recording(db, title="公开分享会议", owner_id=_admin(db).id, duration_ms=90_000)
    add_speakers(db, recording)
    tag = add_tag(db, "论文")
    attach_tag(db, recording, tag)
    share = make_share(db, recording)

    response = public_request(client, "get", f"/api/public/shares/{share.token}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["share"]["has_password"] is False
    assert body["share"]["include_transcript"] is True
    assert body["share"]["include_insights"] is True
    assert body["share"]["allow_download"] is False
    assert body["share"]["view_count"] == 1
    public_recording = body["recording"]
    assert public_recording["uuid"] == recording.uuid
    assert public_recording["title"] == "公开分享会议"
    assert public_recording["duration_ms"] == 90_000
    assert public_recording["recorded_at"]
    assert len(public_recording["speakers"]) == 2
    assert public_recording["tags"][0]["name"] == "论文"
    assert "owner_id" not in public_recording
    assert "owner" not in public_recording
    assert "id" not in public_recording  # only the storage uuid is exposed

    second = public_request(client, "get", f"/api/public/shares/{share.token}")
    assert second.json()["share"]["view_count"] == 2


def test_public_invalid_revoked_expired(client, db):
    recording = make_recording(db, title="失效分享", owner_id=_admin(db).id)
    assert public_request(client, "get", "/api/public/shares/does-not-exist").status_code == 404

    revoked = make_share(db, recording, revoked_at=utcnow())
    assert public_request(client, "get", f"/api/public/shares/{revoked.token}").status_code == 404

    expired = make_share(db, recording, expires_at=utcnow() - timedelta(hours=1))
    response = public_request(client, "get", f"/api/public/shares/{expired.token}")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_public_password_unlock_and_content(client, db):
    recording = _recording_with_media(db, owner_id=_admin(db).id, with_transcript=True)
    share = make_share(db, recording, password="sharepass123")
    token = share.token
    base = f"/api/public/shares/{token}"

    metadata = public_request(client, "get", base)
    assert metadata.status_code == 200
    assert metadata.json()["share"]["has_password"] is True

    assert public_request(client, "get", f"{base}/media").status_code == 401
    assert public_request(client, "get", f"{base}/transcript").status_code == 401

    wrong = public_request(client, "post", f"{base}/unlock", json={"password": "wrong-pass"})
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "UNAUTHORIZED"

    unlocked = public_request(
        client, "post", f"{base}/unlock", json={"password": "sharepass123"}
    )
    assert unlocked.status_code == 200, unlocked.text
    assert unlocked.json() == {"ok": True, "expires_in": 7200}
    cookie = unlocked.headers.get("set-cookie", "")
    lowered = cookie.lower()
    assert "echoic_share=" in cookie
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert f"path=/api/public/shares/{token.lower()}" in lowered
    assert "max-age=7200" in lowered

    media = public_request(client, "get", f"{base}/media")
    assert media.status_code == 200
    assert media.content == b"PLAYBACK-BYTES" * 40
    assert media.headers["accept-ranges"] == "bytes"

    ranged = public_request(client, "get", f"{base}/media", headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206
    assert ranged.content == (b"PLAYBACK-BYTES" * 40)[:10]
    assert ranged.headers["content-range"] == "bytes 0-9/560"

    transcript = public_request(client, "get", f"{base}/transcript")
    assert transcript.status_code == 200
    assert len(transcript.json()["segments"]) == 3

    insights = public_request(client, "get", f"{base}/insights")
    assert insights.status_code == 200
    assert insights.json()["recording_id"] == recording.id
    assert insights.json()["summary"] is not None

    # cookie is bound to this share only
    other = make_share(db, recording, password="otherpass123")
    assert public_request(client, "get", f"/api/public/shares/{other.token}/media").status_code == 401


def test_public_unlock_rate_limited(client, db):
    recording = make_recording(db, title="限速分享", owner_id=_admin(db).id)
    share = make_share(db, recording, password="sharepass123")
    base = f"/api/public/shares/{share.token}"
    for _ in range(5):
        response = public_request(client, "post", f"{base}/unlock", json={"password": "wrong"})
        assert response.status_code == 401
    blocked = public_request(client, "post", f"{base}/unlock", json={"password": "sharepass123"})
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "RATE_LIMITED"
    assert blocked.headers.get("retry-after") == "60"


def test_public_include_flags(client, db):
    recording = _recording_with_media(db, owner_id=_admin(db).id, with_transcript=True)
    share = make_share(db, recording, include_transcript=False, include_insights=False)
    base = f"/api/public/shares/{share.token}"

    transcript = public_request(client, "get", f"{base}/transcript")
    assert transcript.status_code == 403
    assert transcript.json()["code"] == "FORBIDDEN"
    insights = public_request(client, "get", f"{base}/insights")
    assert insights.status_code == 403
    assert public_request(client, "get", f"{base}/media").status_code == 200


def test_public_download_gating(client, db):
    recording = _recording_with_media(db, owner_id=_admin(db).id, with_transcript=True)
    blocked_share = make_share(db, recording, allow_download=False)
    base = f"/api/public/shares/{blocked_share.token}"
    assert public_request(client, "get", f"{base}/download").status_code == 403
    assert public_request(client, "get", f"{base}/media", params={"download": 1}).status_code == 403

    allowed_share = make_share(db, recording, allow_download=True)
    allowed_base = f"/api/public/shares/{allowed_share.token}"
    download = public_request(client, "get", f"{allowed_base}/download")
    assert download.status_code == 200
    assert download.content == b"ORIGINAL-BYTES"
    assert download.headers["content-disposition"].startswith("attachment")
    inline_download = public_request(
        client, "get", f"{allowed_base}/media", params={"download": 1}
    )
    assert inline_download.status_code == 200
    assert inline_download.content == b"ORIGINAL-BYTES"


# ================================================================== system / consume
def test_system_info_consume_block_and_scan(client, db):
    token = _admin_token(client)
    info = api_request(client, token, "get", "/api/system/info")
    assert info.status_code == 200
    consume = info.json()["consume"]
    assert set(consume.keys()) == {
        "enabled",
        "dir",
        "interval_sec",
        "pending_files",
        "failed_files",
        "last_scan",
    }
    assert isinstance(consume["enabled"], bool)
    assert isinstance(consume["interval_sec"], int)
    assert consume["pending_files"] == 0
    assert consume["failed_files"] == 0

    scan = api_request(client, token, "post", "/api/system/consume/scan")
    assert scan.status_code == 200, scan.text
    body = scan.json()
    assert body["ok"] is True
    assert set(body["result"].keys()) == {"scanned", "imported", "duplicates", "failed"}
    assert all(isinstance(value, int) for value in body["result"].values())
    assert body["at"]


def test_settings_system_keys(client, db):
    token = _admin_token(client)
    settings_body = api_request(client, token, "get", "/api/settings").json()
    system = settings_body["system"]
    assert "allow_registration" in system
    assert set(system["consume"].keys()) == {"enabled", "dir", "interval_sec"}
    updated = api_request(
        client, token, "put", "/api/settings", json={"system": {"allow_registration": True}}
    )
    assert updated.status_code == 200
    assert updated.json()["system"]["allow_registration"] is True
