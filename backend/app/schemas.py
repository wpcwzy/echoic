from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from .core.utils import to_iso


def _serialize_dt(value: datetime) -> str:
    return to_iso(value) or ""


UTCDateTime = Annotated[datetime, PlainSerializer(_serialize_dt, return_type=str)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---------------------------------------------------------------- auth / users
class UserOut(ORMModel):
    id: int
    username: str
    display_name: str = ""
    email: str | None = None
    role: Literal["admin", "member"] = "member"
    is_admin: bool = False
    disabled: bool = False
    created_at: UTCDateTime | None = None
    last_login_at: UTCDateTime | None = None


class UserBriefOut(BaseModel):
    id: int
    username: str
    display_name: str = ""


class UserAdminOut(UserOut):
    recording_count: int = 0
    share_count: int = 0


class UserListOut(BaseModel):
    items: list[UserAdminOut] = Field(default_factory=list)
    total: int = 0


class UserCreateIn(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=255)
    role: Literal["admin", "member"] = "member"


class UserUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=255)
    role: Literal["admin", "member"] | None = None
    disabled: bool | None = None


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=200)


class RegisterIn(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=255)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class LoginOut(BaseModel):
    user: UserOut
    token: str


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=6, max_length=200)


class ProfileUpdateIn(BaseModel):
    """Self-service profile update (``PATCH /api/auth/me``).

    Only ``display_name``/``email`` are accepted; any other field sent by a
    client (role/disabled/username/...) is ignored on purpose.
    """

    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=255)


class OkOut(BaseModel):
    ok: bool = True


# ---------------------------------------------------------------- tags / speakers
class TagOut(ORMModel):
    id: int
    name: str
    color: str = "#5B8FF9"


class TagWithCountOut(TagOut):
    recording_count: int = 0


class TagCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    color: str = "#5B8FF9"


class TagUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    color: str | None = None


class SpeakerOut(ORMModel):
    id: int
    label: str
    display_name: str = ""
    color: str = "#5B8FF9"
    is_auto: bool = True


class SpeakerUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    color: str | None = Field(default=None, max_length=16)


class SpeakerMergeIn(BaseModel):
    """Merge one or more diarization speakers into a target speaker."""

    source_ids: list[int] = Field(min_length=1, max_length=50)
    target_id: int
    target_name: str | None = Field(default=None, max_length=120)


class SpeakerMergeOut(BaseModel):
    speakers: list[SpeakerOut] = Field(default_factory=list)
    merged_segments: int = 0
    deleted_speaker_ids: list[int] = Field(default_factory=list)


# ---------------------------------------------------------------- recordings
class JobInfo(ORMModel):
    id: int
    status: str
    stage: str
    progress: int = 0
    attempts: int = 0
    error: str | None = None
    started_at: UTCDateTime | None = None
    finished_at: UTCDateTime | None = None


class RecordingSummary(ORMModel):
    id: int
    uuid: str
    title: str
    description: str | None = None
    status: str
    stage: str
    progress: int = 0
    error_message: str | None = None
    media_type: str = "audio"
    mime_type: str = ""
    original_filename: str = ""
    size_bytes: int = 0
    duration_ms: int = 0
    recorded_at: UTCDateTime
    language: str = "zh"
    source: str = "upload"
    owner_id: int | None = None
    owner: UserBriefOut | None = None
    tags: list[TagOut] = Field(default_factory=list)
    speakers: list[SpeakerOut] = Field(default_factory=list)
    has_transcript: bool = False
    insight_kinds: list[str] = Field(default_factory=list)
    created_at: UTCDateTime
    updated_at: UTCDateTime
    processed_at: UTCDateTime | None = None
    deleted_at: UTCDateTime | None = None


class RecordingDetail(RecordingSummary):
    job: JobInfo | None = None
    chat_message_count: int = 0


class LibraryStats(BaseModel):
    total: int = 0
    ready: int = 0
    processing: int = 0
    failed: int = 0
    total_duration_ms: int = 0
    this_week: int = 0
    hours: float = 0.0


class RecordingListOut(BaseModel):
    items: list[RecordingSummary]
    total: int
    page: int
    page_size: int
    stats: LibraryStats


class RecordingUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    recorded_at: datetime | None = None
    language: str | None = Field(default=None, max_length=16)
    tag_ids: list[int] | None = None
    speaker_names: dict[int, str] | None = None
    owner_id: int | None = None  # admin only


class ReprocessIn(BaseModel):
    from_: Literal["all", "asr", "insights", "embeddings"] = Field(default="all", alias="from")


class SegmentOut(ORMModel):
    id: int
    idx: int
    speaker_id: int | None = None
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None


class SegmentUpdateIn(BaseModel):
    text: str | None = Field(default=None, max_length=20000)
    speaker_id: int | None = None


class TranscriptOut(BaseModel):
    recording_id: int
    language: str = "zh"
    duration_ms: int = 0
    speakers: list[SpeakerOut] = Field(default_factory=list)
    segments: list[SegmentOut] = Field(default_factory=list)


# ---------------------------------------------------------------- insights
class ChapterOut(BaseModel):
    id: str
    title: str
    start_ms: int
    end_ms: int
    summary: str = ""
    bullets: list[str] = Field(default_factory=list)


class ViewpointNodeOut(BaseModel):
    id: str
    label: str
    detail: str = ""
    kind: Literal["topic", "point", "task", "risk"] = "topic"
    time_ms: int | None = None
    speaker: str | None = None
    children: list["ViewpointNodeOut"] = Field(default_factory=list)


ViewpointNodeOut.model_rebuild()


class ViewpointTreeOut(BaseModel):
    """Viewpoint structure: either a single legacy root or a forest of topic trees."""

    root: ViewpointNodeOut | None = None
    roots: list[ViewpointNodeOut] = Field(default_factory=list)


class InsightOut(BaseModel):
    id: str
    title: str
    content: str
    category: Literal["decision", "risk", "insight", "opportunity"] = "insight"


class ActionItemOut(BaseModel):
    id: str
    title: str
    owner: str = ""
    due: str = ""
    status: Literal["open", "done"] = "open"
    detail: str = ""


class KeywordOut(BaseModel):
    name: str
    weight: float = 0.5


class SummaryOut(BaseModel):
    markdown: str = ""


class InsightsOut(BaseModel):
    recording_id: int
    generated_at: UTCDateTime | None = None
    model: str | None = None
    summary: SummaryOut | None = None
    chapters: list[ChapterOut] | None = None
    viewpoint_tree: ViewpointTreeOut | None = None
    insights: list[InsightOut] | None = None
    action_items: list[ActionItemOut] | None = None
    suggested_questions: list[str] | None = None
    keywords: list[KeywordOut] | None = None
    missing: list[str] = Field(default_factory=list)


class RegenerateInsightsIn(BaseModel):
    kinds: list[
        Literal[
            "summary",
            "chapters",
            "viewpoint_tree",
            "insights",
            "action_items",
            "suggested_questions",
            "keywords",
        ]
    ] | None = None


# ---------------------------------------------------------------- chat
class CitationOut(BaseModel):
    recording_id: int
    segment_id: int | None = None
    start_ms: int
    end_ms: int
    speaker: str = ""
    text: str = ""


class ChatMessageOut(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    citations: list[CitationOut] = Field(default_factory=list)
    created_at: UTCDateTime | None = None


class ChatHistoryOut(BaseModel):
    messages: list[ChatMessageOut] = Field(default_factory=list)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


# ---------------------------------------------------------------- settings
class ProviderTestIn(BaseModel):
    kind: Literal["asr", "realtime_asr", "llm", "embeddings"]
    draft: dict[str, Any] | None = None


class ProviderTestOut(BaseModel):
    ok: bool
    latency_ms: int | None = None
    message: str
    detail: str | None = None


class SettingsUpdateIn(BaseModel):
    ai: dict[str, Any] | None = None
    prompts: dict[str, str] | None = None
    system: dict[str, Any] | None = None


class SettingsOut(BaseModel):
    ai: dict[str, Any]
    prompts: dict[str, str]
    prompt_overrides: list[str] = Field(default_factory=list)
    system: dict[str, Any]


# ---------------------------------------------------------------- search / dashboard / system
class SearchSegmentHit(BaseModel):
    recording_id: int
    recording_title: str
    segment_id: int
    speaker: str = ""
    start_ms: int
    snippet: str


class SearchRecordingHit(RecordingSummary):
    hit_count: int = 0


class SearchOut(BaseModel):
    query: str
    recordings: list[SearchRecordingHit] = Field(default_factory=list)
    segments: list[SearchSegmentHit] = Field(default_factory=list)


class StorageInfo(BaseModel):
    media_bytes: int = 0
    recordings: int = 0


class DashboardOut(BaseModel):
    stats: LibraryStats
    recent: list[RecordingSummary] = Field(default_factory=list)
    processing: list[RecordingSummary] = Field(default_factory=list)
    top_tags: list[TagWithCountOut] = Field(default_factory=list)
    storage: StorageInfo = Field(default_factory=StorageInfo)


class WorkerInfo(BaseModel):
    embedded: bool = True
    running: bool = False
    queue_depth: int = 0


class HealthOut(BaseModel):
    status: str = "ok"
    version: str = "0.3.0"
    db: str = "ok"
    media: str = "ok"
    ffmpeg: str = "ok"
    worker: str = "ok"
    storage: str = "ok"
    storage_backend: str = "local"
    storage_detail: str = ""
    demo_mode: bool = False
    allow_registration: bool = False
    time: str = ""


# ---------------------------------------------------------------- v0.2 shares (R-17)
class ShareCreateIn(BaseModel):
    password: str | None = Field(default=None, min_length=4, max_length=64)
    expires_at: datetime | None = None
    include_transcript: bool = True
    include_insights: bool = True
    allow_download: bool = False


class ShareOut(BaseModel):
    id: int
    recording_id: int
    token: str
    url_path: str
    include_transcript: bool = True
    include_insights: bool = True
    allow_download: bool = False
    has_password: bool = False
    expires_at: UTCDateTime | None = None
    expired: bool = False
    revoked_at: UTCDateTime | None = None
    view_count: int = 0
    last_viewed_at: UTCDateTime | None = None
    created_at: UTCDateTime | None = None
    created_by: UserBriefOut | None = None


class ShareListOut(BaseModel):
    items: list[ShareOut] = Field(default_factory=list)
    total: int = 0


class PublicShareInfoOut(BaseModel):
    include_transcript: bool = True
    include_insights: bool = True
    allow_download: bool = False
    has_password: bool = False
    expires_at: UTCDateTime | None = None
    created_at: UTCDateTime | None = None
    view_count: int = 0


class PublicRecordingOut(BaseModel):
    uuid: str
    title: str
    description: str | None = None
    media_type: str = "audio"
    duration_ms: int = 0
    recorded_at: UTCDateTime
    language: str = "zh"
    speakers: list[SpeakerOut] = Field(default_factory=list)
    tags: list[TagOut] = Field(default_factory=list)


class PublicShareOut(BaseModel):
    share: PublicShareInfoOut
    recording: PublicRecordingOut


class ShareUnlockIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class ShareUnlockOut(BaseModel):
    ok: bool = True
    expires_in: int = 7200


# ---------------------------------------------------------------- v0.2 consume import (R-22)
class ConsumeScanResult(BaseModel):
    scanned: int = 0
    imported: int = 0
    duplicates: int = 0
    failed: int = 0


class ConsumeLastScan(ConsumeScanResult):
    at: UTCDateTime | None = None


class ConsumeInfoOut(BaseModel):
    enabled: bool = True
    dir: str = ""
    interval_sec: int = 15
    pending_files: int = 0
    failed_files: int = 0
    last_scan: ConsumeLastScan | None = None


class ConsumeScanOut(BaseModel):
    ok: bool = True
    result: ConsumeScanResult
    at: UTCDateTime | None = None


# ---------------------------------------------------------------- v0.3 trash / batch (R-23)
class BatchActionIn(BaseModel):
    action: Literal["trash", "restore", "purge", "reprocess", "tag_add", "tag_remove"]
    recording_ids: list[int] = Field(min_length=1, max_length=500)
    tag_ids: list[int] | None = None
    from_: Literal["all", "asr", "insights", "embeddings"] = Field(default="all", alias="from")


class BatchItemResult(BaseModel):
    id: int
    ok: bool
    error: str | None = None


class BatchOut(BaseModel):
    action: str
    succeeded: int = 0
    failed: int = 0
    results: list[BatchItemResult] = Field(default_factory=list)


# ---------------------------------------------------------------- v0.3 live transcription (R-19)
class LiveSessionCreateIn(BaseModel):
    language: str | None = Field(default=None, max_length=16)
    merge_mode: Literal["replace", "promote"] | None = None


class LiveSessionOut(BaseModel):
    id: int
    uuid: str
    status: Literal["active", "ended", "merged", "failed", "cancelled"]
    provider: str = ""
    model: str = ""
    language: str = "zh"
    merge_mode: Literal["replace", "promote"] = "replace"
    recording_id: int | None = None
    started_at: UTCDateTime | None = None
    ended_at: UTCDateTime | None = None
    total_audio_ms: int = 0
    error: str | None = None
    ws_url: str | None = None


class LiveSegmentOut(BaseModel):
    id: int
    idx: int
    start_ms: int
    end_ms: int
    text: str
    is_final: bool = True
    speaker_label: str | None = None


class LiveSessionDetailOut(BaseModel):
    session: LiveSessionOut
    segments: list[LiveSegmentOut] = Field(default_factory=list)


class RecordingLiveOut(BaseModel):
    session: LiveSessionOut
    segments: list[LiveSegmentOut] = Field(default_factory=list)


# ---------------------------------------------------------------- v0.3 vector / hybrid search (R-20)
class VectorStatusOut(BaseModel):
    backend: str = "numpy"
    available: bool = True
    embeddings_configured: bool = False
    embedding_model: str | None = None
    dim: int | None = None
    chunks_total: int = 0
    chunks_indexed: int = 0
    stale_chunks: int = 0
    fallback_reason: str | None = None


class VectorReindexIn(BaseModel):
    recording_ids: list[int] | None = None
    force: bool = False
    dry_run: bool = False


class VectorReindexOut(BaseModel):
    ok: bool = True
    processed_recordings: int = 0
    chunks: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


class VectorSearchHit(BaseModel):
    recording_id: int
    recording_title: str = ""
    segment_id: int | None = None
    speaker: str = ""
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    score: float = 0.0


class VectorSearchOut(BaseModel):
    query: str
    mode: str = "hybrid"
    results: list[VectorSearchHit] = Field(default_factory=list)
    fallback_reason: str | None = None


# ---------------------------------------------------------------- v0.3 feature flags
class LiveFeaturesOut(BaseModel):
    enabled: bool = False
    provider: str = "mock"
    max_minutes: int = 60
    merge_mode: Literal["replace", "promote"] = "replace"


class VectorFeaturesOut(BaseModel):
    backend: str = "numpy"
    search_enabled: bool = True
    embeddings_configured: bool = False


class StorageFeaturesOut(BaseModel):
    backend: str = "local"


class TrashFeaturesOut(BaseModel):
    retention_days: int = 30


class FeaturesOut(BaseModel):
    live: LiveFeaturesOut = Field(default_factory=LiveFeaturesOut)
    vector: VectorFeaturesOut = Field(default_factory=VectorFeaturesOut)
    storage: StorageFeaturesOut = Field(default_factory=StorageFeaturesOut)
    trash: TrashFeaturesOut = Field(default_factory=TrashFeaturesOut)


# ---------------------------------------------------------------- v1.0 video scenes / audit logs
class VideoSceneOut(ORMModel):
    id: int
    recording_id: int
    idx: int
    time_ms: int
    image_filename: str = ""
    width: int = 0
    height: int = 0


class VideoSceneListOut(BaseModel):
    items: list[VideoSceneOut] = Field(default_factory=list)


class AuditLogOut(ORMModel):
    id: int
    user_id: int | None = None
    username: str = ""
    action: str = ""
    target_type: str = ""
    target_id: int | None = None
    detail: dict = Field(default_factory=dict)
    ip: str = ""
    created_at: UTCDateTime | None = None


class AuditLogListOut(BaseModel):
    items: list[AuditLogOut] = Field(default_factory=list)
    total: int = 0
