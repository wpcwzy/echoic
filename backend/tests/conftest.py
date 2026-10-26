from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="echoic-tests-")
os.environ["ECHOIC_DATA_DIR"] = _TMP
os.environ.setdefault("ECHOIC_SECRET_KEY", "echoic-test-secret-key-not-for-production")
os.environ.setdefault("ECHOIC_DEMO_SEED", "false")
os.environ.setdefault("ECHOIC_EMBEDDED_WORKER", "false")
os.environ.setdefault("ECHOIC_LOG_LEVEL", "WARNING")
os.environ.setdefault("ECHOIC_ADMIN_USERNAME", "admin")
os.environ.setdefault("ECHOIC_ADMIN_PASSWORD", "admin123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN_USERNAME = os.environ["ECHOIC_ADMIN_USERNAME"]
ADMIN_PASSWORD = os.environ["ECHOIC_ADMIN_PASSWORD"]


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    application = create_app()
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture()
def auth_client(client: TestClient) -> TestClient:
    response = client.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
