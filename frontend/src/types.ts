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

export interface User {
  id: number
  username: string
  display_name: string
  is_admin: boolean
  created_at?: string
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
  source: 'upload' | 'browser_recording'
  tags: Tag[]
  speakers: Speaker[]
  has_transcript: boolean
  insight_kinds: string[]
  created_at: string
  updated_at: string
  processed_at: string | null
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
  viewpoint_tree: { root: ViewpointNode } | null
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
  ai: { asr: ProviderSettings; llm: ProviderSettings; embeddings: ProviderSettings }
  prompts: Record<string, string>
  prompt_overrides: string[]
  system: {
    demo_mode: boolean
    auto_process: boolean
    max_upload_mb: number
    default_language: string
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
}

export type AiKind = 'asr' | 'llm' | 'embeddings'
export type AnalysisKindName = 'summary' | 'chapters' | 'viewpoint_tree' | 'insights' | 'action_items' | 'suggested_questions' | 'keywords'

export interface UpdateRecordingPayload {
  title?: string
  description?: string
  recorded_at?: string
  language?: string
  tag_ids?: number[]
  speaker_names?: Record<number, string>
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
}

export type ListViewMode = 'grid' | 'list'
export type DetailTabKey = 'transcript' | 'summary' | 'viewpoints' | 'chapters'
