"""API tests: authentication + session (docs/API.md §2)."""

from __future__ import annotations

import pytest

from app.models import User
from tests.test_api_helpers import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    api_request,
    clear_login_throttle,
    create_user,
    login_token,
)


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    clear_login_throttle()
    yield
    clear_login_throttle()


def test_health_is_public(client):
    response = client.get("/api/system/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ok", "degraded")
    assert body["version"]
    assert body["db"] == "ok"
    for key in ("media", "ffmpeg", "worker", "demo_mode", "time"):
        assert key in body
    # v0.3.10: public discoverability flag for open registration
    assert body["allow_registration"] is False


def test_protected_endpoint_requires_login(client):
    response = client.get("/api/recordings")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert response.json()["detail"]


def test_login_sets_httponly_cookie_and_returns_token(client):
    response = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token"]
    assert body["user"]["username"] == ADMIN_USERNAME
    assert body["user"]["is_admin"] is True

    cookie = response.headers.get("set-cookie", "")
    lowered = cookie.lower()
    assert "echoic_token=" in cookie
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    assert "max-age=604800" in lowered


def test_login_rejects_wrong_password(client):
    response = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_login_rate_limited_after_five_failures(client):
    for _ in range(5):
        response = client.post(
            "/api/auth/login", json={"username": ADMIN_USERNAME, "password": "wrong"}
        )
        assert response.status_code == 401
    blocked = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": "wrong"}
    )
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "RATE_LIMITED"
    assert blocked.headers.get("retry-after") == "60"


def test_me_returns_current_user(auth_client):
    response = auth_client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["user"]["username"] == ADMIN_USERNAME


def test_bearer_token_is_accepted(client):
    login = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    ).json()
    response = client.get(
        "/api/recordings", headers={"Authorization": f"Bearer {login['token']}"}
    )
    assert response.status_code == 200


def test_cookie_session_and_logout(client):
    login = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert login.status_code == 200
    # httpx TestClient keeps the cookie jar: cookie auth works without a header
    assert client.get("/api/auth/me").status_code == 200

    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200
    assert logout.json()["ok"] is True
    assert client.get("/api/auth/me").status_code == 401


def test_logout_requires_login(client):
    assert client.post("/api/auth/logout").status_code == 401


@pytest.mark.parametrize(
    ("old_password", "new_password"),
    [
        ("wrong-old", "brand-new-pass"),
        (ADMIN_PASSWORD, "short"),
    ],
)
def test_change_password_validation(client, old_password, new_password):
    login = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    ).json()
    response = client.post(
        "/api/auth/change-password",
        json={"old_password": old_password, "new_password": new_password},
        headers={"Authorization": f"Bearer {login['token']}"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_change_password_flow(client):
    login = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    ).json()
    headers = {"Authorization": f"Bearer {login['token']}"}
    response = client.post(
        "/api/auth/change-password",
        json={"old_password": ADMIN_PASSWORD, "new_password": "newpass456"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    clear_login_throttle()
    assert (
        client.post(
            "/api/auth/login", json={"username": ADMIN_USERNAME, "password": "newpass456"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        ).status_code
        == 401
    )


def test_change_password_requires_login(client):
    response = client.post(
        "/api/auth/change-password",
        json={"old_password": ADMIN_PASSWORD, "new_password": "newpass456"},
    )
    assert response.status_code == 401


# ------------------------------------------------------------------ self profile (v0.3.10)
def test_patch_me_requires_login(client):
    response = client.patch("/api/auth/me", json={"display_name": "x"})
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_patch_me_updates_self_profile(client, db):
    member = create_user(
        db, "profileuser", "password123", display_name="旧名字", email="old@example.com"
    )
    member_id = member.id
    token = login_token(client, "profileuser", "password123")

    response = api_request(
        client,
        token,
        "patch",
        "/api/auth/me",
        json={
            "display_name": "新名字",
            "email": "new@example.com",
            # must be ignored: self-service profile cannot escalate
            "role": "admin",
            "is_admin": True,
            "disabled": True,
            "username": "hacked",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["display_name"] == "新名字"
    assert body["user"]["email"] == "new@example.com"
    assert body["user"]["role"] == "member"
    assert body["user"]["is_admin"] is False
    assert body["user"]["disabled"] is False
    assert body["user"]["username"] == "profileuser"

    me = api_request(client, token, "get", "/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["display_name"] == "新名字"
    assert me.json()["user"]["email"] == "new@example.com"

    db.expire_all()
    stored = db.query(User).filter(User.id == member_id).one()
    assert stored.display_name == "新名字"
    assert stored.email == "new@example.com"
    assert stored.role == "member"
    assert stored.disabled is False
    assert stored.username == "profileuser"


def test_patch_me_clear_fields(client, db):
    create_user(db, "clearuser", "password123", display_name="有名字", email="a@example.com")
    token = login_token(client, "clearuser", "password123")

    cleared = api_request(
        client, token, "patch", "/api/auth/me", json={"display_name": "", "email": ""}
    )
    assert cleared.status_code == 200
    assert cleared.json()["user"]["display_name"] == "clearuser"  # falls back to username
    assert cleared.json()["user"]["email"] is None

    reset = api_request(
        client, token, "patch", "/api/auth/me", json={"display_name": None, "email": None}
    )
    assert reset.status_code == 200
    assert reset.json()["user"]["display_name"] == "clearuser"
    assert reset.json()["user"]["email"] is None

    untouched = api_request(client, token, "patch", "/api/auth/me", json={})
    assert untouched.status_code == 200
    assert untouched.json()["user"]["display_name"] == "clearuser"


def test_patch_me_validation(client):
    token = login_token(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    too_long_name = api_request(
        client, token, "patch", "/api/auth/me", json={"display_name": "x" * 121}
    )
    assert too_long_name.status_code == 422
    assert too_long_name.json()["code"] == "VALIDATION_ERROR"

    too_long_email = api_request(
        client, token, "patch", "/api/auth/me", json={"email": "x" * 256}
    )
    assert too_long_email.status_code == 422
    assert too_long_email.json()["code"] == "VALIDATION_ERROR"


def test_health_registration_flag_matches_setting(client, db):
    # default: registration disabled, flag is public on the unauthenticated health endpoint
    assert client.get("/api/system/health").json()["allow_registration"] is False

    admin = login_token(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    enabled = api_request(
        client,
        admin,
        "put",
        "/api/settings",
        json={"system": {"allow_registration": True}},
    )
    assert enabled.status_code == 200
    assert enabled.json()["system"]["allow_registration"] is True
    assert client.get("/api/system/health").json()["allow_registration"] is True

    registered = client.post(
        "/api/auth/register",
        json={
            "username": "newprofile",
            "password": "password123",
            "display_name": "新用户",
            "email": "n@example.com",
        },
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["role"] == "member"
    assert registered.json()["email"] == "n@example.com"

    login = client.post(
        "/api/auth/login", json={"username": "newprofile", "password": "password123"}
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "member"
    assert login.json()["user"]["is_admin"] is False

    disabled = api_request(
        client,
        admin,
        "put",
        "/api/settings",
        json={"system": {"allow_registration": False}},
    )
    assert disabled.status_code == 200
    assert client.get("/api/system/health").json()["allow_registration"] is False
    blocked = client.post(
        "/api/auth/register", json={"username": "lateruser", "password": "password123"}
    )
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "FORBIDDEN"
