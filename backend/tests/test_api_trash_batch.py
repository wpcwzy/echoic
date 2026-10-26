"""API tests for v0.3: trash & batch operations (R-23), feature flags and live binding.

Contract: docs/10-v0.3.0-实现契约.md §1/§4/§6.
"""

from __future__ import annotations

import uuid as uuidlib
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.core.utils import utcnow
from app.models import Job, LiveSegment, LiveSession, Recording, Share, User
from app.services import trash as trash_service
from tests.test_api_helpers import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    add_speakers,
    add_tag,
    add_all_analyses,
    add_segments,
    api_request,
    attach_tag,
    clear_login_throttle,
    clear_share_throttle,
    create_user,
    ffmpeg_available,
    get_user,
    login_token,
    make_chat_message,
    make_job,
    make_recording,
    make_share,
    make_wav_bytes,
    public_request,
    seed_ready_recording,
    write_media,
)


@pytest.fixture(autouse=True)
def _reset_state():
    clear_login_throttle()
    clear_share_throttle()
    trash_service.reset_schedule_state()
    yield
    clear_login_throttle()
    clear_share_throttle()
    trash_service.reset_schedule_state()


# ------------------------------------------------------------------ helpers
def _admin(db) -> User:
    user = get_user(db, ADMIN_USERNAME)
    assert user is not None
    return user


def _admin_token(client) -> str:
    return login_token(client, ADMIN_USERNAME, ADMIN_PASSWORD)


def _member(client, db, username: str = "member1", password: str = "memberpass123"):
    user = create_user(db, username, password, display_name="成员一号")
    token = login_token(client, username, password)
    return user, token


def _media_file(recording: Recording, *, original=b"ORIGINAL", playback=b"PLAYBACK"):
    write_media(recording, original=original, playback=playback)
    return settings.media_path / recording.uuid


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _make_live_session(
    db,
    *,
    user_id: int | None,
    status: str = "ended",
    recording_id: int | None = None,
    merge_mode: str = "replace",
    total_audio_ms: int = 2500,
) -> LiveSession:
    session = LiveSession(
        uuid=str(uuidlib.uuid4()),
        user_id=user_id,
        recording_id=recording_id,
        status=status,
        provider="mock",
        model="mock-live",
        language="zh",
        merge_mode=merge_mode,
        started_at=utcnow(),
        ended_at=None if status == "active" else utcnow(),
        total_audio_ms=total_audio_ms,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _add_live_segments(db, session: LiveSession, texts=("实时片段一", "实时片段二")) -> None:
    for index, text in enumerate(texts):
        db.add(
            LiveSegment(
                session_id=session.id,
                idx=index,
                start_ms=index * 1000,
                end_ms=(index + 1) * 1000,
                text=text,
                is_final=True,
            )
        )
    db.commit()


# ================================================================== soft-delete invariant
def test_soft_delete_hides_recording_everywhere(client, db):
    admin = _admin(db)
    token = _admin_token(client)
    recording = seed_ready_recording(
        db,
        title="Graph500 交付会议",
        owner_id=admin.id,
        with_transcript=True,
        with_insights=True,
    )
    directory = _media_file(recording)
    tag = add_tag(db, "组会")
    attach_tag(db, recording, tag)
    make_chat_message(db, recording, role="user", content="问题")
    make_job(db, recording, status="done", stage="done", progress=100)
    share = make_share(db, recording, created_by=admin.id)

    before = api_request(client, token, "get", "/api/tags").json()["items"]
    assert before[0]["recording_count"] == 1

    deleted = api_request(client, token, "delete", f"/api/recordings/{recording.id}")
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["ok"] is True
    assert body["id"] == recording.id
    assert _parse_iso(body["restore_before"]) > _parse_iso(body["deleted_at"])

    # every recording-scoped endpoint answers 404
    endpoints = [
        ("get", ""),
        ("get", "/media"),
        ("get", "/transcript"),
        ("get", "/transcript/export?format=txt"),
        ("get", "/insights"),
        ("get", "/chat"),
        ("get", "/jobs"),
        ("get", "/shares"),
        ("get", "/live"),
        ("patch", ""),
        ("post", "/reprocess"),
        ("post", "/cancel"),
        ("post", "/chat"),
    ]
    for method, suffix in endpoints:
        kwargs = {}
        if method == "patch":
            kwargs["json"] = {}
        elif suffix == "/chat":
            kwargs["json"] = {"message": "hi"}
        response = api_request(
            client, token, method, f"/api/recordings/{recording.id}{suffix}", **kwargs
        )
        assert response.status_code == 404, f"{method} {suffix} -> {response.status_code}"
        assert response.json()["code"] == "NOT_FOUND"

    normal = api_request(client, token, "get", "/api/recordings").json()
    assert normal["total"] == 0
    assert normal["stats"]["total"] == 0

    trash = api_request(client, token, "get", "/api/recordings", params={"trashed": True}).json()
    assert trash["total"] == 1
    assert trash["items"][0]["id"] == recording.id
    assert trash["items"][0]["deleted_at"] is not None
    assert trash["stats"]["total"] == 1

    search = api_request(client, token, "get", "/api/search", params={"q": "Graph500"}).json()
    assert search["recordings"] == []
    assert search["segments"] == []

    dashboard = api_request(client, token, "get", "/api/dashboard").json()
    assert dashboard["stats"]["total"] == 0
    assert dashboard["recent"] == []
    assert dashboard["storage"]["recordings"] == 0

    tags = api_request(client, token, "get", "/api/tags").json()["items"]
    assert tags[0]["recording_count"] == 0

    assert api_request(client, token, "get", "/api/shares").json()["total"] == 0
    db.expire_all()
    stored_share = db.get(Share, share.id)
    assert stored_share is not None and stored_share.revoked_at is not None
    assert public_request(client, "get", f"/api/public/shares/{share.token}").status_code == 404
    assert directory.exists()

    # restore brings data back; old public links stay revoked (decision: no auto re-enable)
    restored = api_request(client, token, "post", f"/api/recordings/{recording.id}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == recording.id
    assert restored.json()["deleted_at"] is None
    assert api_request(client, token, "get", f"/api/recordings/{recording.id}").status_code == 200
    transcript = api_request(client, token, "get", f"/api/recordings/{recording.id}/transcript")
    assert transcript.status_code == 200 and len(transcript.json()["segments"]) == 3
    insights = api_request(client, token, "get", f"/api/recordings/{recording.id}/insights")
    assert insights.status_code == 200 and insights.json()["summary"] is not None
    assert public_request(client, "get", f"/api/public/shares/{share.token}").status_code == 404

    again = api_request(client, token, "delete", f"/api/recordings/{recording.id}")
    assert again.status_code == 200
    assert api_request(client, token, "delete", f"/api/recordings/{recording.id}").status_code == 404
    assert api_request(client, token, "delete", f"/api/recordings/{recording.id}/purge").status_code == 200


def test_trash_list_scope_and_order(client, db):
    admin = _admin(db)
    member, member_token = _member(client, db)
    admin_token = _admin_token(client)
    older = make_recording(
        db,
        title="管理员已删除",
        owner_id=admin.id,
        deleted_at=utcnow() - timedelta(hours=2),
    )
    newer = make_recording(
        db,
        title="成员已删除",
        owner_id=member.id,
        deleted_at=utcnow() - timedelta(hours=1),
    )
    make_recording(db, title="正常录音", owner_id=admin.id)

    admin_trash = api_request(
        client, admin_token, "get", "/api/recordings", params={"trashed": True}
    ).json()
    assert admin_trash["total"] == 2
    assert [item["id"] for item in admin_trash["items"]] == [newer.id, older.id]
    assert admin_trash["items"][0]["deleted_at"] is not None
    assert admin_trash["stats"]["total"] == 2

    member_trash = api_request(
        client, member_token, "get", "/api/recordings", params={"trashed": True}
    ).json()
    assert member_trash["total"] == 1
    assert member_trash["items"][0]["id"] == newer.id

    admin_normal = api_request(client, admin_token, "get", "/api/recordings").json()
    assert admin_normal["total"] == 1
    assert admin_normal["items"][0]["title"] == "正常录音"


def test_restore_and_purge_permissions(client, db):
    admin = _admin(db)
    member, member_token = _member(client, db)
    admin_token = _admin_token(client)
    admin_trashed = make_recording(
        db, title="管理员回收站", owner_id=admin.id, deleted_at=utcnow()
    )
    member_trashed = make_recording(
        db, title="成员回收站", owner_id=member.id, deleted_at=utcnow()
    )
    admin_trashed2 = make_recording(
        db, title="管理员回收站二", owner_id=admin.id, deleted_at=utcnow()
    )
    active = make_recording(db, title="未删除", owner_id=member.id)

    assert (
        api_request(
            client, member_token, "post", f"/api/recordings/{admin_trashed.id}/restore"
        ).status_code
        == 404
    )
    assert (
        api_request(client, member_token, "delete", f"/api/recordings/{admin_trashed.id}/purge").status_code
        == 404
    )
    assert (
        api_request(client, member_token, "post", f"/api/recordings/{member_trashed.id}/restore").status_code
        == 200
    )
    assert (
        api_request(client, member_token, "post", f"/api/recordings/{active.id}/restore").status_code
        == 404
    )
    # admin can operate on any trash
    assert (
        api_request(client, admin_token, "post", f"/api/recordings/{admin_trashed.id}/restore").status_code
        == 200
    )
    assert (
        api_request(client, admin_token, "delete", f"/api/recordings/{admin_trashed2.id}/purge").status_code
        == 200
    )


def test_purge_flow_cleans_everything(client, db):
    admin = _admin(db)
    token = _admin_token(client)
    recording = make_recording(db, title="待彻底删除", owner_id=admin.id)
    recording_id = recording.id
    directory = _media_file(recording)
    share = make_share(db, recording, created_by=admin.id)
    share_id = share.id
    make_chat_message(db, recording, role="user", content="问题")
    make_job(db, recording)

    assert api_request(client, token, "delete", f"/api/recordings/{recording_id}/purge").status_code == 404
    assert api_request(client, token, "delete", f"/api/recordings/{recording_id}").status_code == 200
    purged = api_request(client, token, "delete", f"/api/recordings/{recording_id}/purge")
    assert purged.status_code == 200
    assert purged.json() == {"ok": True, "purged": True}

    assert not directory.exists()
    db.expire_all()
    assert db.query(Recording).filter(Recording.id == recording_id).all() == []
    assert db.query(Share).filter(Share.id == share_id).all() == []
    assert db.query(Job).filter(Job.recording_id == recording_id).all() == []
    assert (
        api_request(client, token, "delete", f"/api/recordings/{recording_id}/purge").status_code
        == 404
    )


def test_admin_sees_member_trash_member_does_not(client, db):
    admin = _admin(db)
    member, member_token = _member(client, db)
    admin_token = _admin_token(client)
    recording = make_recording(db, title="管理员的录音", owner_id=admin.id, deleted_at=utcnow())
    assert (
        api_request(client, member_token, "get", f"/api/recordings/{recording.id}").status_code
        == 404
    )
    assert (
        api_request(client, admin_token, "get", f"/api/recordings/{recording.id}").status_code
        == 404
    )  # still trashed for normal detail
    assert (
        api_request(
            client, admin_token, "get", "/api/recordings", params={"trashed": True}
        ).json()["total"]
        == 1
    )


# ================================================================== batch operations
def test_batch_trash_partial_success(client, db):
    admin = _admin(db)
    token = _admin_token(client)
    first = make_recording(db, title="一", owner_id=admin.id)
    second = make_recording(db, title="二", owner_id=admin.id)

    response = api_request(
        client,
        token,
        "post",
        "/api/recordings/batch",
        json={"action": "trash", "recording_ids": [first.id, second.id, 9999]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "trash"
    assert body["succeeded"] == 2
    assert body["failed"] == 1
    assert [item["id"] for item in body["results"]] == [first.id, second.id, 9999]
    assert body["results"][2]["ok"] is False
    assert body["results"][2]["error"] == "录音不存在"
    assert (
        api_request(client, token, "get", "/api/recordings", params={"trashed": True}).json()["total"]
        == 2
    )


def test_batch_member_permission_partial(client, db):
    admin = _admin(db)
    member, member_token = _member(client, db)
    own = make_recording(db, title="成员录音", owner_id=member.id)
    foreign = make_recording(db, title="管理员录音", owner_id=admin.id)
    own_id, foreign_id = own.id, foreign.id
    response = api_request(
        client,
        member_token,
        "post",
        "/api/recordings/batch",
        json={"action": "trash", "recording_ids": [own_id, foreign_id, 9999]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 2
    assert body["results"][1]["ok"] is False
    db.expire_all()
    assert db.query(Recording).filter(Recording.id == foreign_id).one().deleted_at is None
    assert db.query(Recording).filter(Recording.id == own_id).one().deleted_at is not None


def test_batch_restore_and_purge(client, db):
    admin = _admin(db)
    token = _admin_token(client)
    first = make_recording(db, title="一", owner_id=admin.id)
    second = make_recording(db, title="二", owner_id=admin.id)
    first_id, second_id = first.id, second.id
    api_request(
        client,
        token,
        "post",
        "/api/recordings/batch",
        json={"action": "trash", "recording_ids": [first_id, second_id]},
    )

    restored = api_request(
        client, token, "post", "/api/recordings/batch", json={"action": "restore", "recording_ids": [first.id]}
    ).json()
    assert restored["succeeded"] == 1
    purged = api_request(
        client, token, "post", "/api/recordings/batch", json={"action": "purge", "recording_ids": [second.id]}
    ).json()
    assert purged["succeeded"] == 1
    db.expire_all()
    assert db.query(Recording).filter(Recording.id == second_id).all() == []
    assert db.query(Recording).filter(Recording.id == first_id).one().deleted_at is None

    active_purge = api_request(
        client, token, "post", "/api/recordings/batch", json={"action": "purge", "recording_ids": [first.id]}
    ).json()
    assert active_purge["failed"] == 1
    assert active_purge["results"][0]["error"] == "录音不在回收站"


def test_batch_reprocess(client, db, monkeypatch):
    admin = _admin(db)
    token = _admin_token(client)
    active = make_recording(db, title="正常", owner_id=admin.id)
    trashed = make_recording(db, title="回收站", owner_id=admin.id, deleted_at=utcnow())
    calls: list[tuple[int, str]] = []

    from app.services import queue as queue_module

    def fake_enqueue(db_session, recording_id, from_="all"):
        calls.append((recording_id, from_))

    monkeypatch.setattr(queue_module, "enqueue", fake_enqueue)
    response = api_request(
        client,
        token,
        "post",
        "/api/recordings/batch",
        json={"action": "reprocess", "recording_ids": [active.id, trashed.id], "from": "insights"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert calls == [(active.id, "insights")]
    db.expire_all()
    assert db.get(Recording, active.id).status == "queued"


def test_batch_tag_actions_admin_only(client, db):
    admin = _admin(db)
    member, member_token = _member(client, db)
    admin_token = _admin_token(client)
    first = make_recording(db, title="一", owner_id=admin.id)
    second = make_recording(db, title="二", owner_id=admin.id)
    tag = add_tag(db, "批量标签")

    forbidden = api_request(
        client,
        member_token,
        "post",
        "/api/recordings/batch",
        json={"action": "tag_add", "recording_ids": [first.id], "tag_ids": [tag.id]},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"

    added = api_request(
        client,
        admin_token,
        "post",
        "/api/recordings/batch",
        json={"action": "tag_add", "recording_ids": [first.id, second.id], "tag_ids": [tag.id]},
    )
    assert added.status_code == 200
    assert added.json()["succeeded"] == 2
    for recording_id in (first.id, second.id):
        tags = api_request(client, admin_token, "get", f"/api/recordings/{recording_id}").json()["tags"]
        assert [item["name"] for item in tags] == ["批量标签"]

    removed = api_request(
        client,
        admin_token,
        "post",
        "/api/recordings/batch",
        json={"action": "tag_remove", "recording_ids": [first.id, second.id], "tag_ids": [tag.id]},
    )
    assert removed.status_code == 200
    assert removed.json()["succeeded"] == 2
    tags = api_request(client, admin_token, "get", f"/api/recordings/{first.id}").json()["tags"]
    assert tags == []

    missing_tags = api_request(
        client,
        admin_token,
        "post",
        "/api/recordings/batch",
        json={"action": "tag_add", "recording_ids": [first.id]},
    )
    assert missing_tags.status_code == 400
    unknown_tags = api_request(
        client,
        admin_token,
        "post",
        "/api/recordings/batch",
        json={"action": "tag_add", "recording_ids": [first.id], "tag_ids": [9999]},
    )
    assert unknown_tags.status_code == 400


def test_batch_limit_and_validation(client, db, monkeypatch):
    admin = _admin(db)
    token = _admin_token(client)
    first = make_recording(db, title="一", owner_id=admin.id)
    second = make_recording(db, title="二", owner_id=admin.id)
    third = make_recording(db, title="三", owner_id=admin.id)

    monkeypatch.setattr(settings, "batch_max_items", 2)
    over_limit = api_request(
        client,
        token,
        "post",
        "/api/recordings/batch",
        json={"action": "trash", "recording_ids": [first.id, second.id, third.id]},
    )
    assert over_limit.status_code == 400
    assert over_limit.json()["code"] == "VALIDATION_ERROR"

    within = api_request(
        client,
        token,
        "post",
        "/api/recordings/batch",
        json={"action": "trash", "recording_ids": [first.id, second.id]},
    )
    assert within.status_code == 200
    assert within.json()["succeeded"] == 2

    invalid_action = api_request(
        client,
        token,
        "post",
        "/api/recordings/batch",
        json={"action": "explode", "recording_ids": [first.id]},
    )
    assert invalid_action.status_code == 422
    empty = api_request(
        client, token, "post", "/api/recordings/batch", json={"action": "trash", "recording_ids": []}
    )
    assert empty.status_code == 422


# ================================================================== retention purge
def test_retention_purge_expired(client, db):
    old = make_recording(
        db, title="过期回收站", deleted_at=utcnow() - timedelta(days=40)
    )
    old_id = old.id
    directory = _media_file(old)
    recent = make_recording(db, title="新回收站", deleted_at=utcnow() - timedelta(days=2))
    recent_id = recent.id
    make_recording(db, title="正常")

    result = trash_service.purge_expired()
    assert result["purged"] == 1
    assert result["ids"] == [old_id]
    assert result["errors"] == 0
    assert result["retention_days"] == 30
    assert result["at"]

    assert not directory.exists()
    db.expire_all()
    assert db.query(Recording).filter(Recording.id == old_id).all() == []
    assert db.query(Recording).filter(Recording.id == recent_id).all() != []
    assert db.query(Recording).filter(Recording.deleted_at.is_(None)).count() == 1


def test_retention_purge_custom_retention(client, db):
    recording = make_recording(
        db, title="十天前删除", deleted_at=utcnow() - timedelta(days=10)
    )
    recording_id = recording.id
    result = trash_service.purge_expired(retention=30)
    assert result["purged"] == 0
    db.expire_all()
    assert db.query(Recording).filter(Recording.id == recording_id).all() != []

    result = trash_service.purge_expired(retention=5)
    assert result["purged"] == 1
    db.expire_all()
    assert db.query(Recording).filter(Recording.id == recording_id).all() == []


def test_maybe_purge_expired_throttled(client, db):
    recording = make_recording(
        db, title="过期", deleted_at=utcnow() - timedelta(days=40)
    )
    recording_id = recording.id
    first = trash_service.maybe_purge_expired(force=True)
    assert first is not None and first["purged"] == 1
    assert trash_service.last_purge_info()["purged"] == 1
    db.expire_all()
    assert db.query(Recording).filter(Recording.id == recording_id).all() == []

    second = trash_service.maybe_purge_expired()
    assert second is None  # throttled for an hour


def test_worker_status_has_trash_info(client):
    from app.worker import worker_status

    status = worker_status()
    assert "last_trash_purge" in status


# ================================================================== feature flags
def test_features_shape(client, db):
    member, token = _member(client, db)
    response = api_request(client, token, "get", "/api/system/features")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body.keys()) == {"live", "vector", "storage", "trash"}
    assert set(body["live"].keys()) == {"enabled", "provider", "max_minutes", "merge_mode"}
    assert body["live"]["enabled"] in (True, False)
    assert body["live"]["provider"] in ("mock", "dashscope_realtime")
    assert body["live"]["merge_mode"] in ("replace", "promote")
    assert set(body["vector"].keys()) == {"backend", "search_enabled", "embeddings_configured"}
    assert body["vector"]["search_enabled"] is True
    assert body["vector"]["backend"] in ("numpy", "pgvector")
    assert set(body["storage"].keys()) == {"backend"}
    assert set(body["trash"].keys()) == {"retention_days"}
    assert body["trash"]["retention_days"] >= 1


def test_features_require_login(client):
    client.cookies.clear()
    response = client.get("/api/system/features")
    assert response.status_code == 401


def test_features_reflect_configuration(client, db, monkeypatch):
    from app.services.settings_store import update_settings

    token = _admin_token(client)
    update_settings(
        db,
        {
            "ai": {
                "asr": {"provider": "dashscope_realtime"},
                "embeddings": {"provider": "mock"},
            }
        },
    )
    monkeypatch.setattr(settings, "live_enabled", True)
    monkeypatch.setattr(settings, "live_provider", "auto")
    monkeypatch.setattr(settings, "live_max_minutes", 45)
    body = api_request(client, token, "get", "/api/system/features").json()
    assert body["live"]["enabled"] is True
    assert body["live"]["provider"] == "dashscope_realtime"
    assert body["live"]["max_minutes"] == 45
    assert body["vector"]["embeddings_configured"] is True

    monkeypatch.setattr(settings, "live_provider", "mock")
    monkeypatch.setattr(settings, "vector_backend", "pgvector")
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "trash_retention_days", 7)
    body = api_request(client, token, "get", "/api/system/features").json()
    assert body["live"]["provider"] == "mock"
    assert body["vector"]["backend"] == "pgvector"
    assert body["storage"]["backend"] == "s3"
    assert body["trash"]["retention_days"] == 7


def test_system_info_trash_counts(client, db):
    make_recording(db, title="正常")
    make_recording(db, title="回收站", deleted_at=utcnow())
    token = _admin_token(client)
    info = api_request(client, token, "get", "/api/system/info").json()
    assert info["recordings"] == 1
    assert info["trashed"] == 1

    member, member_token = _member(client, db)
    assert api_request(client, member_token, "get", "/api/system/info").status_code == 403


# ================================================================== live binding
def test_upload_binds_live_session(client, db):
    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    admin = _admin(db)
    token = _admin_token(client)
    session = _make_live_session(db, user_id=admin.id, status="ended")
    _add_live_segments(db, session)
    payload = make_wav_bytes(duration=0.5)

    response = api_request(
        client,
        token,
        "post",
        "/api/recordings/upload",
        files={"file": ("实时录音.wav", payload, "audio/wav")},
        data={"title": "实时录音", "live_session_id": str(session.id)},
    )
    assert response.status_code == 201, response.text
    recording_id = response.json()["id"]

    db.expire_all()
    stored = db.get(LiveSession, session.id)
    assert stored is not None
    assert stored.recording_id == recording_id
    assert stored.status == "ended"

    live = api_request(client, token, "get", f"/api/recordings/{recording_id}/live")
    assert live.status_code == 200, live.text
    body = live.json()
    assert body["session"]["id"] == session.id
    assert body["session"]["status"] == "ended"
    assert body["session"]["total_audio_ms"] == 2500
    assert [segment["text"] for segment in body["segments"]] == ["实时片段一", "实时片段二"]

    duplicate = api_request(
        client,
        token,
        "post",
        "/api/recordings/upload",
        files={"file": ("again.wav", payload, "audio/wav")},
        data={"live_session_id": str(session.id)},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "CONFLICT"


def test_live_binding_validation(client, db):
    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    admin = _admin(db)
    admin_token = _admin_token(client)
    member, member_token = _member(client, db)
    other, _other_token = _member(client, db, username="member2")
    payload = make_wav_bytes(duration=0.3)

    missing = api_request(
        client,
        admin_token,
        "post",
        "/api/recordings/upload",
        files={"file": ("x.wav", payload, "audio/wav")},
        data={"live_session_id": "9999"},
    )
    assert missing.status_code == 404

    foreign = _make_live_session(db, user_id=other.id, status="ended")
    blocked = api_request(
        client,
        member_token,
        "post",
        "/api/recordings/upload",
        files={"file": ("x.wav", payload, "audio/wav")},
        data={"live_session_id": str(foreign.id)},
    )
    assert blocked.status_code == 404

    failed = _make_live_session(db, user_id=member.id, status="failed")
    invalid = api_request(
        client,
        member_token,
        "post",
        "/api/recordings/upload",
        files={"file": ("x.wav", payload, "audio/wav")},
        data={"live_session_id": str(failed.id)},
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "VALIDATION_ERROR"

    # admin may bind another user's ended session
    member_session = _make_live_session(db, user_id=member.id, status="ended")
    allowed = api_request(
        client,
        admin_token,
        "post",
        "/api/recordings/upload",
        files={"file": ("x.wav", payload, "audio/wav")},
        data={"live_session_id": str(member_session.id)},
    )
    assert allowed.status_code == 201


def test_recording_live_route_permissions(client, db):
    admin = _admin(db)
    admin_token = _admin_token(client)
    member, member_token = _member(client, db)

    plain = make_recording(db, title="无实时会话", owner_id=admin.id)
    assert (
        api_request(client, admin_token, "get", f"/api/recordings/{plain.id}/live").status_code
        == 404
    )

    processing = make_recording(
        db, title="处理中", owner_id=admin.id, status="processing", stage="analyzing", progress=40
    )
    live_session = _make_live_session(db, user_id=admin.id, recording_id=processing.id, status="ended")
    _add_live_segments(db, live_session, texts=("处理中也可见",))
    live = api_request(client, admin_token, "get", f"/api/recordings/{processing.id}/live")
    assert live.status_code == 200
    assert live.json()["segments"][0]["text"] == "处理中也可见"

    trashed = make_recording(db, title="回收站实时", owner_id=admin.id, deleted_at=utcnow())
    _make_live_session(db, user_id=admin.id, recording_id=trashed.id, status="ended")
    assert (
        api_request(client, admin_token, "get", f"/api/recordings/{trashed.id}/live").status_code
        == 404
    )
    assert (
        api_request(client, member_token, "get", f"/api/recordings/{processing.id}/live").status_code
        == 404
    )
    client.cookies.clear()
    client.headers.pop("Authorization", None)
    assert client.get(f"/api/recordings/{processing.id}/live").status_code == 401
