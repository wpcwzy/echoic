"""Shared seeding helpers for the v0.3.0 vector tests (no test cases here).

Named ``test_vector_helpers.py`` so it stays inside the
``tests/test_vector_*.py`` scope owned by backend-search; imported by
``test_vector_store.py`` / ``test_vector_retrieval.py`` / ``test_vector_api.py``
/ ``test_vector_cli.py``.
"""

from __future__ import annotations

import uuid as uuidlib

import numpy as np
import pytest

from app.core.utils import utcnow
from app.db import Base, SessionLocal, engine
from app.models import Chunk, Recording, Speaker, TranscriptSegment, User
from app.providers.embeddings_mock import MockEmbeddingProvider
from app.services.settings_store import update_settings

EMBED_MODEL = "mock-embedding"
EMBED_DIM = 256

_PROVIDER = MockEmbeddingProvider()

DEFAULT_SPEAKERS = [
    {"label": "说话人1", "display_name": "吴克强"},
    {"label": "说话人2", "display_name": "李梦"},
]

DEFAULT_SEGMENTS = [
    (0, 10_000, 0, "我们这次主要讨论 Graph500 基准测试的交付节点。"),
    (10_000, 24_000, 1, "摘要部分预计还有两周时间完成并交付。"),
    (24_000, 40_000, 0, "那我们把最终报告定在三周之后提交。"),
]

UNRELATED_SEGMENTS = [
    (0, 12_000, 0, "今天天气很好，下午我们一起去公园散步。"),
]


def reset_database() -> None:
    """Drop and recreate all tables for the shared test engine."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture()
def vdb():
    """Fresh schema + SessionLocal session (service-level vector tests)."""
    reset_database()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def embedding_provider() -> MockEmbeddingProvider:
    return _PROVIDER


def vector_values(text: str) -> list[float]:
    return list(_PROVIDER.embed_one(text))


def vector_bytes(text: str) -> tuple[bytes, int]:
    values = np.asarray(_PROVIDER.embed_one(text), dtype=np.float32)
    return values.tobytes(), int(values.size)


def configure_embeddings(db, provider: str = "mock") -> None:
    """Persist the embeddings provider via the settings service (mock/none)."""
    update_settings(db, {"ai": {"embeddings": {"provider": provider}}})


def make_recording(
    db,
    *,
    title: str = "产品评审会议",
    owner_id: int | None = None,
    deleted_at=None,
    duration_ms: int = 40_000,
) -> Recording:
    recording_uuid = str(uuidlib.uuid4())
    recording = Recording(
        uuid=recording_uuid,
        title=title,
        status="ready",
        stage="done",
        progress=100,
        media_type="audio",
        mime_type="audio/mpeg",
        original_filename=f"{title}.mp3",
        original_ext=".mp3",
        storage_dir=f"media/{recording_uuid}",
        size_bytes=1024,
        duration_ms=duration_ms,
        recorded_at=utcnow(),
        language="zh",
        source="upload",
        owner_id=owner_id,
        deleted_at=deleted_at,
    )
    db.add(recording)
    db.commit()
    db.refresh(recording)
    return recording


def add_transcript(db, recording: Recording, segments=None, speakers=None):
    """Create speakers + transcript segments; returns ``(speakers, segments)``."""
    speaker_rows: list[Speaker] = []
    for index, data in enumerate(speakers or DEFAULT_SPEAKERS):
        speaker = Speaker(
            recording_id=recording.id,
            label=data.get("label", f"说话人{index + 1}"),
            display_name=data.get("display_name", ""),
            color=data.get("color", "#5B8FF9"),
            is_auto=True,
        )
        db.add(speaker)
        speaker_rows.append(speaker)
    db.flush()
    segment_rows: list[TranscriptSegment] = []
    for index, (start_ms, end_ms, speaker_index, text) in enumerate(
        segments or DEFAULT_SEGMENTS
    ):
        speaker_id = (
            speaker_rows[speaker_index % len(speaker_rows)].id if speaker_rows else None
        )
        segment = TranscriptSegment(
            recording_id=recording.id,
            idx=index,
            speaker_id=speaker_id,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            confidence=0.9,
        )
        db.add(segment)
        segment_rows.append(segment)
    db.commit()
    for row in [*speaker_rows, *segment_rows]:
        db.refresh(row)
    return speaker_rows, segment_rows


def make_chunk(
    db,
    recording: Recording,
    *,
    idx: int,
    start_ms: int,
    end_ms: int,
    text: str,
    embedding: bytes | None = None,
    dim: int | None = None,
    model: str | None = EMBED_MODEL,
) -> Chunk:
    chunk = Chunk(
        recording_id=recording.id,
        idx=idx,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        embedding=embedding,
        dim=dim,
        embedding_model=model,
    )
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    return chunk


def add_chunks(db, recording: Recording, items=None, *, embed: bool = True) -> list[Chunk]:
    """Create chunks from ``(start_ms, end_ms, text[, embedding_bytes])`` tuples.

    Defaults to the default transcript texts; ``embed=False`` leaves the
    embedding columns empty (pre-reindex state).
    """
    source = list(items or [(start, end, text) for start, end, _speaker, text in DEFAULT_SEGMENTS])
    rows: list[Chunk] = []
    for index, item in enumerate(source):
        start_ms, end_ms, text = int(item[0]), int(item[1]), str(item[2])
        explicit = item[3] if len(item) > 3 else None
        if explicit is not None:
            embedding = explicit
            dim = max(1, len(explicit) // 4)
            model = EMBED_MODEL
        elif embed:
            embedding, dim = vector_bytes(text)
            model = EMBED_MODEL
        else:
            embedding, dim, model = None, None, None
        rows.append(
            make_chunk(
                db,
                recording,
                idx=index,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                embedding=embedding,
                dim=dim,
                model=model,
            )
        )
    return rows


def seed_recording(
    db,
    *,
    title: str = "产品评审会议",
    owner_id: int | None = None,
    segments=None,
    chunks=None,
    embed: bool = True,
    deleted_at=None,
) -> Recording:
    recording = make_recording(db, title=title, owner_id=owner_id, deleted_at=deleted_at)
    add_transcript(db, recording, segments=segments)
    add_chunks(db, recording, chunks, embed=embed)
    return recording


def create_user(
    db,
    username: str = "member1",
    password: str = "memberpass123",
    *,
    role: str = "member",
    is_admin: bool | None = None,
) -> User:
    from app.core.security import hash_password

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=username,
        email=None,
        role=role,
        is_admin=bool(role == "admin" if is_admin is None else is_admin),
        disabled=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_admin(db, username: str = "admin") -> User:
    return create_user(db, username, role="admin", is_admin=True)


def create_member(db, username: str = "member1") -> User:
    return create_user(db, username, role="member", is_admin=False)
