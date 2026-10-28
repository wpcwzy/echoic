// Shared API types — keep in sync with docs/API.md and backend/app/schemas.py
export type RecordingStatus = 'uploaded' | 'queued' | 'processing' | 'ready' | 'failed' | 'cancelled'
export type Stage = 'preparing' | 'transcribing' | 'analyzing' | 'embedding' | 'finalizing' | 'done' | 'failed'
export type MediaType = 'audio' | 'video'
export type AnalysisKind =
  | 'summary'
  | 'chapters'
  | 'viewpoint_tree'
  | 'insights'
  | 'action_items'
  | 'suggested_questions'
  | 'keywords'

export type UserRole = 'admin' | 'member'

export interface User {
  id: number
  username: string
  display_name: string
  email: string | null
  role: UserRole
  is_admin: boolean
  disabled: boolean
  created_at?: string | null
  last_login_at?: string | null
}

export interface UserBrief {
  id: number
  username: string
  display_name: string
}

export interface Tag {
  id: number
  name: string
  color: string
}

export interface TagWithCount extends Tag {
  recording_count: number
}

export interface Speaker {
  id: number
  label: string
  display_name: string
  color: string
  is_auto: boolean
}

export interface SpeakerMergeResult {
  speakers: Speaker[]
  merged_segments: number
  deleted_speaker_ids: number[]
}

export interface JobInfo {
  id: number
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  stage: string
  progress: number
  attempts: number
  error: string | null
  started_at: string | null
  finished_at: string | null
}

export interface RecordingSummary {
  id: number
  uuid: string
  title: string
  description: string | null
  status: RecordingStatus
  stage: Stage
  progress: number
  error_message: string | null
  media_type: MediaType
  mime_type: string
  original_filename: string
  size_bytes: number
  duration_ms: number
  recorded_at: string
  language: string
  source: 'upload' | 'browser_recording' | 'consume'
  owner_id: number | null
  owner: UserBrief | null
  tags: Tag[]
  speakers: Speaker[]
  has_transcript: boolean
  insight_kinds: string[]
  created_at: string
  updated_at: string
  processed_at: string | null
  deleted_at: string | null
}

export interface RecordingDetail extends RecordingSummary {
  job: JobInfo | null
  chat_message_count: number
}

export interface LibraryStats {
  total: number
  ready: number
  processing: number
  failed: number
  total_duration_ms: number
  this_week: number
  hours: number
}

export interface RecordingList {
  items: RecordingSummary[]
  total: number
  page: number
  page_size: number
  stats: LibraryStats
}

export interface Segment {
  id: number
  idx: number
  speaker_id: number | null
  start_ms: number
  end_ms: number
  text: string
  confidence: number | null
}

export interface Transcript {
  recording_id: number
  language: string
  duration_ms: number
  speakers: Speaker[]
  segments: Segment[]
}

export interface ViewpointNode {
  id: string
  label: string
  detail?: string
  kind?: 'topic' | 'point' | 'task' | 'risk'
  time_ms?: number | null
  speaker?: string | null
  children: ViewpointNode[]
}

/** A meeting can have one legacy root or a forest of independent topic trees. */
export interface ViewpointTree {
  root?: ViewpointNode | null
  roots?: ViewpointNode[]
}

export interface Chapter {
  id: string
  title: string
  start_ms: number
  end_ms: number
  summary: string
  bullets: string[]
}

export interface Insight {
  id: string
  title: string
  content: string
  category: 'decision' | 'risk' | 'insight' | 'opportunity'
}

export interface ActionItem {
  id: string
  title: string
  owner: string
  due: string
  status: 'open' | 'done'
  detail: string
}

export interface Citation {
  recording_id: number
  segment_id: number | null
  start_ms: number
  end_ms: number
  speaker: string
  text: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  created_at?: string
}

export interface Insights {
  recording_id: number
  generated_at: string | null
  model: string | null
  summary: { markdown: string } | null
  chapters: Chapter[] | null
  viewpoint_tree: ViewpointTree | null
  insights: Insight[] | null
  action_items: ActionItem[] | null
  suggested_questions: string[] | null
  keywords: { name: string; weight: number }[] | null
  missing: string[]
}

export interface ProviderSettings {
  provider: string
  base_url: string
  model: string
  has_api_key: boolean
  api_key_masked: string
  configured: boolean
  language?: string
  diarization?: boolean
  temperature?: number
  dimensions?: number
}

export interface Settings {
  ai: { asr: ProviderSettings; realtime_asr: ProviderSettings; llm: ProviderSettings; embeddings: ProviderSettings }
  prompts: Record<string, string>
  prompt_overrides: string[]
  system: {
    demo_mode: boolean
    auto_process: boolean
    max_upload_mb: number
    default_language: string
    allow_registration: boolean
    consume?: { enabled: boolean; dir: string; interval_sec: number }
    live_enabled?: boolean
    live_merge_mode?: 'replace' | 'promote'
    version: string
    worker: { embedded: boolean; running: boolean; queue_depth: number }
  }
}

export interface ProviderTestResult {
  ok: boolean
  latency_ms: number | null
  message: string
  detail?: string | null
}

export interface SearchResult {
  query: string
  recordings: (RecordingSummary & { hit_count: number })[]
  segments: {
    recording_id: number
    recording_title: string
    segment_id: number
    speaker: string
    start_ms: number
    snippet: string
  }[]
}

export interface Dashboard {
  stats: LibraryStats
  recent: RecordingSummary[]
  processing: RecordingSummary[]
  top_tags: TagWithCount[]
  storage: { media_bytes: number; recordings: number }
}

export type SSEEvent =
  | { type: 'start'; message_id: number }
  | { type: 'delta'; text: string }
  | { type: 'citations'; citations: Citation[] }
  | { type: 'done'; message_id: number }
  | { type: 'error'; message: string }

// ---------------------------------------------------------------------------
// Response envelopes / auxiliary types (see docs/API.md)
// ---------------------------------------------------------------------------

export interface LoginResponse {
  user: User
  token: string
}

export interface RegisterPayload {
  username: string
  password: string
  display_name?: string
  email?: string
}

export interface ProfileUpdatePayload {
  display_name?: string
  email?: string
}

export interface MeResponse {
  user: User
}

export interface OkResponse {
  ok: boolean
  status?: string
}

export interface ChatHistory {
  messages: ChatMessage[]
}

export interface TagListResponse {
  items: TagWithCount[]
}

export interface SystemHealth {
  status: string
  version: string
  db?: string
  media?: string
  ffmpeg?: string
  worker?: string
  demo_mode?: boolean
  allow_registration?: boolean
  time?: string
}

export interface SystemInfo {
  version: string
  data_dir: string
  disk: { total_bytes: number; used_bytes: number; free_bytes: number }
  recordings: number
  segments: number
  chunks: number
  worker: { embedded?: boolean; running?: boolean; queue_depth?: number }
  consume?: ConsumeInfo
}

export type AiKind = 'asr' | 'realtime_asr' | 'llm' | 'embeddings'
export type AnalysisKindName = 'summary' | 'chapters' | 'viewpoint_tree' | 'insights' | 'action_items' | 'suggested_questions' | 'keywords'

export interface UpdateRecordingPayload {
  title?: string
  description?: string
  recorded_at?: string
  language?: string
  tag_ids?: number[]
  speaker_names?: Record<number, string>
  /** Admin only — reassign recording ownership. */
  owner_id?: number
}

export interface ReprocessPayload {
  from: 'all' | 'asr' | 'insights' | 'embeddings'
}

export interface RecordingListParams {
  query?: string
  status?: string
  tag_id?: number | null
  sort?: 'recent' | 'oldest' | 'title' | 'duration'
  page?: number
  page_size?: number
  /** v0.3 trash view: only soft-deleted recordings. */
  trashed?: boolean
}

// ---------------------------------------------------------------------------
// v0.2 — roles / user management (R-18), sharing (R-17), consume import (R-22)
// ---------------------------------------------------------------------------

export interface UserAdmin extends User {
  recording_count: number
  share_count: number
}

export interface UserListResponse {
  items: UserAdmin[]
  total: number
}

export interface UserListParams {
  query?: string
  role?: UserRole
  include_disabled?: boolean
  page?: number
  page_size?: number
}

export interface UserCreatePayload {
  username: string
  password: string
  display_name?: string
  email?: string
  role?: UserRole
}

export interface UserUpdatePayload {
  display_name?: string
  email?: string
  role?: UserRole
  disabled?: boolean
}

export interface ShareInfo {
  id: number
  recording_id: number
  token: string
  url_path: string
  include_transcript: boolean
  include_insights: boolean
  allow_download: boolean
  has_password: boolean
  expires_at: string | null
  expired: boolean
  revoked_at: string | null
  view_count: number
  last_viewed_at: string | null
  created_at: string | null
  created_by: UserBrief | null
}

export interface ShareListResponse {
  items: ShareInfo[]
  total: number
}

export interface ShareCreatePayload {
  password?: string | null
  expires_at?: string | null
  include_transcript?: boolean
  include_insights?: boolean
  allow_download?: boolean
}

export interface PublicShareInfo {
  include_transcript: boolean
  include_insights: boolean
  allow_download: boolean
  has_password: boolean
  expires_at: string | null
  created_at: string | null
  view_count: number
}

export interface PublicRecording {
  uuid: string
  title: string
  description: string | null
  media_type: MediaType
  duration_ms: number
  recorded_at: string
  language: string
  speakers: Speaker[]
  tags: Tag[]
}

export interface PublicShareResponse {
  share: PublicShareInfo
  recording: PublicRecording
}

export interface ShareUnlockResponse {
  ok: boolean
  expires_in: number
}

export interface ConsumeScanResult {
  scanned: number
  imported: number
  duplicates: number
  failed: number
}

export interface ConsumeLastScan extends ConsumeScanResult {
  at: string | null
}

export interface ConsumeInfo {
  enabled: boolean
  dir: string
  interval_sec: number
  pending_files: number
  failed_files: number
  last_scan: ConsumeLastScan | null
}

export interface ConsumeScanResponse {
  ok: boolean
  result: ConsumeScanResult
  at: string | null
}

// ---------------------------------------------------------------------------
// v0.3 — trash & batch operations (R-23), feature flags
// ---------------------------------------------------------------------------

export interface SoftDeleteResponse {
  ok: boolean
  id: number
  deleted_at: string
  restore_before: string
}

export interface PurgeResponse {
  ok: boolean
  purged: boolean
}

export type BatchAction = 'trash' | 'restore' | 'purge' | 'reprocess' | 'tag_add' | 'tag_remove'

export interface BatchActionPayload {
  action: BatchAction
  recording_ids: number[]
  tag_ids?: number[]
  from?: 'all' | 'asr' | 'insights' | 'embeddings'
}

export interface BatchItemResult {
  id: number
  ok: boolean
  error: string | null
}

export interface BatchResponse {
  action: string
  succeeded: number
  failed: number
  results: BatchItemResult[]
}

export interface SystemFeatures {
  live: { enabled: boolean; provider: string; max_minutes: number; merge_mode: 'replace' | 'promote' }
  vector: { backend: string; search_enabled: boolean; embeddings_configured: boolean }
  storage: { backend: string }
  trash: { retention_days: number }
}

export type ListViewMode = 'grid' | 'list'
export type DetailTabKey = 'transcript' | 'summary' | 'viewpoints' | 'chapters'

// ---------------------------------------------------------------------------
// v0.3 Phase 2 — live transcription (R-19) & vector/hybrid search (R-20)
// ---------------------------------------------------------------------------

export type LiveSessionStatus = 'active' | 'ended' | 'merged' | 'failed' | 'cancelled'

export interface LiveSession {
  id: number
  uuid: string
  status: LiveSessionStatus
  provider: string
  model: string
  language: string
  merge_mode: 'replace' | 'promote'
  recording_id: number | null
  started_at: string | null
  ended_at: string | null
  total_audio_ms: number
  error: string | null
  ws_url: string | null
}

export interface LiveSegment {
  id: number
  idx: number
  start_ms: number
  end_ms: number
  text: string
  is_final: boolean
  speaker_label: string | null
}

export interface LiveSessionDetail {
  session: LiveSession
  segments: LiveSegment[]
}

export type RecordingLive = LiveSessionDetail

export interface LiveSessionCreatePayload {
  language?: string
  merge_mode?: 'replace' | 'promote'
}

export type LiveWsEvent =
  | { type: 'ready' }
  | { type: 'partial'; text: string; start_ms: number; end_ms: number }
  | { type: 'final'; text: string; start_ms: number; end_ms: number; speaker_label?: string | null }
  | { type: 'error'; message: string; code?: string }
  | { type: 'done'; total_ms: number; status?: string }

export interface VectorStatus {
  backend: string
  available: boolean
  embeddings_configured: boolean
  embedding_model: string | null
  dim: number | null
  chunks_total: number
  chunks_indexed: number
  stale_chunks: number
  fallback_reason: string | null
}

export interface VectorReindexPayload {
  recording_ids?: number[]
  force?: boolean
  dry_run?: boolean
}

export interface VectorReindexResult {
  ok: boolean
  processed_recordings: number
  chunks: number
  skipped: number
  errors: string[]
}

export interface VectorSearchHit {
  recording_id: number
  recording_title: string
  segment_id: number | null
  speaker: string
  start_ms: number
  end_ms: number
  text: string
  score: number
}

export interface VectorSearchResult {
  query: string
  mode: string
  results: VectorSearchHit[]
  fallback_reason: string | null
}

export type SearchMode = 'keyword' | 'hybrid' | 'semantic'

// ---------------------------------------------------------------------------
// v1.0 — video scenes (filmstrip) & admin audit log
// ---------------------------------------------------------------------------

export interface VideoScene {
  id: number
  recording_id: number
  idx: number
  time_ms: number
  image_filename: string
  width: number
  height: number
}

export interface VideoSceneListResponse {
  items: VideoScene[]
}

export interface AuditLog {
  id: number
  user_id: number | null
  username: string
  action: string
  target_type: string
  target_id: number | null
  detail: Record<string, unknown>
  ip: string
  created_at: string | null
}

export interface AuditLogListResponse {
  items: AuditLog[]
  total: number
}

export interface AuditLogParams {
  action?: string
  user_id?: number
  target_type?: string
  q?: string
  page?: number
  page_size?: number
}
