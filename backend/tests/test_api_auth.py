"""API tests: authentication + session (docs/API.md §2)."""

from __future__ import annotations

import pytest

from tests.test_api_helpers import ADMIN_PASSWORD, ADMIN_USERNAME, clear_login_throttle


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
