"""API tests: transcript read/edit/export (docs/API.md §3)."""

from __future__ import annotations

import json

import pytest

from tests.test_api_helpers import (
    add_speakers,
    add_segments,
    make_recording,
    seed_ready_recording,
)


def _transcript_recording(db):
    recording = make_recording(db, title="算法组讨论", duration_ms=45_000)
    speakers = add_speakers(db, recording)
    segments = add_segments(db, recording, speakers)
    return recording, speakers, segments


def test_get_transcript(auth_client, db):
    recording, speakers, segments = _transcript_recording(db)
    response = auth_client.get(f"/api/recordings/{recording.id}/transcript")
    assert response.status_code == 200
    body = response.json()
    assert body["recording_id"] == recording.id
    assert body["language"] == "zh"
    assert body["duration_ms"] == 45_000
    assert [speaker["display_name"] for speaker in body["speakers"]] == ["吴克强", "李梦"]
    assert [segment["idx"] for segment in body["segments"]] == [0, 1, 2]
    assert body["segments"][0]["text"].startswith("我们这次主要讨论")
    assert body["segments"][0]["speaker_id"] == speakers[0].id


def test_get_transcript_not_found(auth_client):
    assert auth_client.get("/api/recordings/9999/transcript").status_code == 404


def test_patch_speaker(auth_client, db):
    recording, speakers, _ = _transcript_recording(db)
    response = auth_client.patch(
        f"/api/recordings/{recording.id}/speakers/{speakers[0].id}",
        json={"display_name": "王五", "color": "#ff0000"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "王五"
    assert body["color"] == "#ff0000"


def test_patch_speaker_wrong_recording(auth_client, db):
    recording, _, _ = _transcript_recording(db)
    other = make_recording(db, title="另一场会议")
    other_speakers = add_speakers(db, other)
    response = auth_client.patch(
        f"/api/recordings/{recording.id}/speakers/{other_speakers[0].id}",
        json={"display_name": "不应该成功"},
    )
    assert response.status_code == 404


def test_patch_segment_text_and_speaker(auth_client, db):
    recording, speakers, segments = _transcript_recording(db)
    response = auth_client.patch(
        f"/api/recordings/{recording.id}/segments/{segments[0].id}",
        json={"text": "修改后的文本", "speaker_id": speakers[1].id},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "修改后的文本"
    assert body["speaker_id"] == speakers[1].id

    cleared = auth_client.patch(
        f"/api/recordings/{recording.id}/segments/{segments[0].id}",
        json={"speaker_id": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["speaker_id"] is None


def test_patch_segment_invalid_speaker(auth_client, db):
    recording, _, segments = _transcript_recording(db)
    other = make_recording(db, title="另一场会议")
    other_speakers = add_speakers(db, other)
    response = auth_client.patch(
        f"/api/recordings/{recording.id}/segments/{segments[0].id}",
        json={"speaker_id": other_speakers[0].id},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_export_txt(auth_client, db):
    recording, _, _ = _transcript_recording(db)
    response = auth_client.get(
        f"/api/recordings/{recording.id}/transcript/export", params={"format": "txt"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "filename*=UTF-8''" in disposition
    text = response.text
    assert "[00:00:00] 吴克强：我们这次主要讨论 Graph500 基准测试的交付节点。" in text
    assert "[00:00:12] 李梦：摘要部分预计还有两周时间完成并交付。" in text


def test_export_srt(auth_client, db):
    recording, _, _ = _transcript_recording(db)
    response = auth_client.get(
        f"/api/recordings/{recording.id}/transcript/export", params={"format": "srt"}
    )
    assert response.status_code == 200
    body = response.text
    assert body.startswith("1\n00:00:00,000 --> 00:00:12,000\n")
    assert "2\n00:00:12,000 --> 00:00:28,000" in body


def test_export_markdown(auth_client, db):
    recording, _, _ = _transcript_recording(db)
    response = auth_client.get(
        f"/api/recordings/{recording.id}/transcript/export", params={"format": "md"}
    )
    assert response.status_code == 200
    assert response.text.startswith("# 算法组讨论")
    assert "**[00:00:00] 吴克强**：" in response.text


def test_export_json_matches_transcript(auth_client, db):
    recording, _, _ = _transcript_recording(db)
    response = auth_client.get(
        f"/api/recordings/{recording.id}/transcript/export", params={"format": "json"}
    )
    assert response.status_code == 200
    body = json.loads(response.text)
    assert body["recording_id"] == recording.id
    assert len(body["segments"]) == 3
    assert body["speakers"][0]["display_name"] == "吴克强"


def test_export_invalid_format(auth_client, db):
    recording = seed_ready_recording(db)
    response = auth_client.get(
        f"/api/recordings/{recording.id}/transcript/export", params={"format": "docx"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("suffix", ["/transcript", "/transcript/export?format=txt"])
def test_transcript_requires_auth(client, suffix):
    assert client.get(f"/api/recordings/1{suffix}").status_code == 401
