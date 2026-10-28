from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    """Naive UTC timestamp (safe for SQLite and PostgreSQL timestamptz)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RecordingStatus:
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stage:
    PREPARING = "preparing"
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    EMBEDDING = "embedding"
    FINALIZING = "finalizing"
    DONE = "done"
    FAILED = "failed"


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ANALYSIS_KINDS = (
    "summary",
    "chapters",
    "viewpoint_tree",
    "insights",
    "action_items",
    "suggested_questions",
    "keywords",
)


recording_tags = Table(
    "recording_tags",
    Base.metadata,
    Column("recording_id", ForeignKey("recordings.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    role: Mapped[str] = mapped_column(String(16), default="member", index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def is_admin_role(self) -> bool:
        return self.role == "admin" or bool(self.is_admin)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    color: Mapped[str] = mapped_column(String(16), default="#5B8FF9")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    recordings: Mapped[list["Recording"]] = relationship(
        secondary=recording_tags, back_populates="tags"
    )


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=RecordingStatus.UPLOADED, index=True)
    stage: Mapped[str] = mapped_column(String(24), default=Stage.PREPARING)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    media_type: Mapped[str] = mapped_column(String(12), default="audio")
    mime_type: Mapped[str] = mapped_column(String(120), default="")
    original_filename: Mapped[str] = mapped_column(String(255), default="")
    original_ext: Mapped[str] = mapped_column(String(16), default="")
    storage_dir: Mapped[str] = mapped_column(String(255), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)

    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    language: Mapped[str] = mapped_column(String(16), default="zh")
    source: Mapped[str] = mapped_column(String(24), default="upload")
    asr_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)

    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    tags: Mapped[list[Tag]] = relationship(secondary=recording_tags, back_populates="recordings")
    speakers: Mapped[list["Speaker"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="Speaker.id"
    )
    segments: Mapped[list["TranscriptSegment"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="TranscriptSegment.idx"
    )
    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="Chunk.idx"
    )
    video_scenes: Mapped[list["VideoScene"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="VideoScene.idx"
    )
    chat_messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="ChatMessage.id"
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="Job.id"
    )
    shares: Mapped[list["Share"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="Share.id"
    )

    def analysis_map(self) -> dict[str, "Analysis"]:
        return {a.kind: a for a in self.analyses}


class Speaker(Base):
    __tablename__ = "speakers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    color: Mapped[str] = mapped_column(String(16), default="#5B8FF9")
    is_auto: Mapped[bool] = mapped_column(Boolean, default=True)

    recording: Mapped[Recording] = relationship(back_populates="speakers")
    segments: Mapped[list["TranscriptSegment"]] = relationship(back_populates="speaker")


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("recording_id", "idx", name="uq_segment_recording_idx"),
        Index("ix_segments_recording_start", "recording_id", "start_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, default=0)
    speaker_id: Mapped[int | None] = mapped_column(
        ForeignKey("speakers.id", ondelete="SET NULL"), nullable=True
    )
    start_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    end_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    recording: Mapped[Recording] = relationship(back_populates="segments")
    speaker: Mapped[Speaker | None] = relationship(back_populates="segments")


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (UniqueConstraint("recording_id", "kind", name="uq_analysis_recording_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    recording: Mapped[Recording] = relationship(back_populates="analyses")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("recording_id", "idx", name="uq_chunk_recording_idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, default=0)
    start_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    end_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    recording: Mapped[Recording] = relationship(back_populates="chunks")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="user")
    content: Mapped[str] = mapped_column(Text, default="")
    citations_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    recording: Mapped[Recording] = relationship(back_populates="chat_messages")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(24), default="process")
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED, index=True)
    stage: Mapped[str] = mapped_column(String(24), default=Stage.PREPARING)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    from_stage: Mapped[str] = mapped_column(String(24), default="all")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    recording: Mapped[Recording] = relationship(back_populates="jobs")


class Share(Base):
    """Public read-only share link for a recording (R-17)."""

    __tablename__ = "shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    include_transcript: Mapped[bool] = mapped_column(Boolean, default=True)
    include_insights: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_download: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    recording: Mapped[Recording] = relationship(back_populates="shares")


class VideoScene(Base):
    """Keyframe/slide extracted from a video recording for timeline navigation."""

    __tablename__ = "video_scenes"
    __table_args__ = (
        UniqueConstraint("recording_id", "idx", name="uq_video_scenes_recording_idx"),
        Index("ix_video_scenes_recording_time", "recording_id", "time_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, default=0)
    time_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    image_filename: Mapped[str] = mapped_column(String(120), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    recording: Mapped[Recording] = relationship(back_populates="video_scenes")


class AuditLog(Base):
    """Append-only administrative/security audit trail (v1.0)."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_action_created", "action", "created_at"),
        Index("ix_audit_logs_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    username: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), index=True, default="")
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ImportedFile(Base):
    """Consume-directory import bookkeeping / sha256 dedupe (R-22)."""

    __tablename__ = "imported_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="imported", index=True)
    recording_id: Mapped[int | None] = mapped_column(
        ForeignKey("recordings.id", ondelete="SET NULL"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LiveSession(Base):
    """Live (in-progress) transcription session (R-19, v0.3.0 beta)."""

    __tablename__ = "live_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    recording_id: Mapped[int | None] = mapped_column(
        ForeignKey("recordings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    provider: Mapped[str] = mapped_column(String(40), default="mock")
    model: Mapped[str] = mapped_column(String(120), default="")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    merge_mode: Mapped[str] = mapped_column(String(16), default="replace")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_audio_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    segments: Mapped[list["LiveSegment"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="LiveSegment.idx"
    )


class LiveSegment(Base):
    __tablename__ = "live_segments"
    __table_args__ = (UniqueConstraint("session_id", "idx", name="uq_live_segment_session_idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("live_sessions.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, default=0)
    start_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    end_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    is_final: Mapped[bool] = mapped_column(Boolean, default=True)
    speaker_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    session: Mapped[LiveSession] = relationship(back_populates="segments")
