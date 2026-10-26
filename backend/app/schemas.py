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
    is_admin: bool = True
    created_at: UTCDateTime | None = None


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class LoginOut(BaseModel):
    user: UserOut
    token: str


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=6, max_length=200)


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
    tags: list[TagOut] = Field(default_factory=list)
    speakers: list[SpeakerOut] = Field(default_factory=list)
    has_transcript: bool = False
    insight_kinds: list[str] = Field(default_factory=list)
    created_at: UTCDateTime
    updated_at: UTCDateTime
    processed_at: UTCDateTime | None = None


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
    root: ViewpointNodeOut


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
    kind: Literal["asr", "llm", "embeddings"]
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
    version: str = "0.1.0"
    db: str = "ok"
    media: str = "ok"
    ffmpeg: str = "ok"
    worker: str = "ok"
    demo_mode: bool = False
    time: str = ""
