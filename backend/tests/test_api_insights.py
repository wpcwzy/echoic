"""API tests: insights aggregation + regenerate (docs/API.md §4)."""

from __future__ import annotations

import pytest

from app.models import Recording
from tests.test_api_helpers import (
    DEMO_ANALYSES,
    add_all_analyses,
    add_analysis,
    make_recording,
    seed_ready_recording,
)

ALL_KINDS = list(DEMO_ANALYSES.keys())


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[tuple[int, str]] = []

    from app.services import queue as queue_module

    def fake_enqueue(db, recording_id, from_="all"):
        calls.append((recording_id, from_))

    monkeypatch.setattr(queue_module, "enqueue", fake_enqueue)
    return calls


def test_insights_full(auth_client, db):
    recording = seed_ready_recording(db, with_insights=True)
    response = auth_client.get(f"/api/recordings/{recording.id}/insights")
    assert response.status_code == 200
    body = response.json()
    assert body["recording_id"] == recording.id
    assert body["missing"] == []
    assert body["generated_at"] is not None
    assert body["model"] == "qwen-plus"
    assert body["summary"]["markdown"].startswith("## 会议概述")
    assert body["chapters"][0]["title"] == "交付节点讨论"
    assert body["chapters"][0]["bullets"]
    root = body["viewpoint_tree"]["root"]
    assert root["label"] == "交付节点"
    assert root["children"][0]["time_ms"] == 12_000
    assert body["insights"][0]["category"] == "decision"
    assert body["action_items"][0]["owner"] == "吴克强"
    assert body["suggested_questions"][0] == "这次会议定了哪些交付节点？"
    assert body["keywords"][0]["name"] == "Graph500"


def test_insights_partial_reports_missing(auth_client, db):
    recording = seed_ready_recording(db)
    add_analysis(db, recording, "summary", {"markdown": "只有纪要"})
    response = auth_client.get(f"/api/recordings/{recording.id}/insights")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["markdown"] == "只有纪要"
    assert body["chapters"] is None
    assert "chapters" in body["missing"]
    assert set(body["missing"]) == set(ALL_KINDS) - {"summary"}


def test_insights_empty_and_malformed(auth_client, db):
    recording = seed_ready_recording(db)
    add_analysis(db, recording, "chapters", "not-json-at-all")
    response = auth_client.get(f"/api/recordings/{recording.id}/insights")
    assert response.status_code == 200
    body = response.json()
    assert body["generated_at"] is not None
    assert len(body["missing"]) == len(ALL_KINDS)


def test_insights_tolerates_alternate_payload_keys(auth_client, db):
    recording = seed_ready_recording(db)
    add_analysis(db, recording, "summary", {"summary_md": "用 summary_md 字段"})
    add_analysis(db, recording, "chapters", {"chapters": DEMO_ANALYSES["chapters"]})
    add_analysis(
        db, recording, "suggested_questions", {"questions": ["问题一", "问题二"]}
    )
    add_analysis(db, recording, "action_items", {"action_items": DEMO_ANALYSES["action_items"]})
    response = auth_client.get(f"/api/recordings/{recording.id}/insights")
    body = response.json()
    assert body["summary"]["markdown"] == "用 summary_md 字段"
    assert body["chapters"][0]["id"] == "ch1"
    assert body["suggested_questions"] == ["问题一", "问题二"]
    assert body["action_items"][0]["title"] == "完成摘要"


def test_insights_not_found(auth_client):
    assert auth_client.get("/api/recordings/9999/insights").status_code == 404


def test_regenerate_insights_enqueues(auth_client, db, enqueued):
    recording = seed_ready_recording(db, with_insights=True)
    response = auth_client.post(
        f"/api/recordings/{recording.id}/insights/regenerate",
        json={"kinds": ["summary", "chapters"]},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "queued"}
    assert enqueued == [(recording.id, "insights")]
    db.expire_all()
    assert db.get(Recording, recording.id).status == "queued"

    no_body = auth_client.post(f"/api/recordings/{recording.id}/insights/regenerate")
    assert no_body.status_code == 200
    assert enqueued[-1] == (recording.id, "insights")


def test_regenerate_invalid_kind(auth_client, db):
    recording = seed_ready_recording(db)
    response = auth_client.post(
        f"/api/recordings/{recording.id}/insights/regenerate",
        json={"kinds": ["does_not_exist"]},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_insights_export_markdown(auth_client, db):
    recording = seed_ready_recording(db, with_insights=True)
    response = auth_client.get(f"/api/recordings/{recording.id}/insights/export")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert "## 关键洞察" in response.text
    assert "完成摘要" in response.text


@pytest.mark.parametrize("path", ["/insights", "/insights/regenerate"])
def test_insights_requires_auth(client, path):
    method = client.get if path == "/insights" else client.post
    assert method(f"/api/recordings/1{path}").status_code == 401
