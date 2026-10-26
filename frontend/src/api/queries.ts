import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type {
  ChatHistory,
  LoginResponse,
  MeResponse,
  OkResponse,
  RecordingDetail,
  RecordingList,
  RecordingListParams,
  Settings,
  Speaker,
  SystemHealth,
  SystemInfo,
  ConsumeScanResponse,
  TagListResponse,
  Transcript,
  Insights,
  UpdateRecordingPayload,
  User,
  Segment,
  SoftDeleteResponse,
  SystemFeatures,
  ProviderTestResult,
  SearchResult,
} from '../types'

/* -------------------------------------------------------------------------- */
/* Query keys                                                                  */
/* -------------------------------------------------------------------------- */

export const qk = {
  me: ['auth', 'me'] as const,
  recordings: (params: RecordingListParams) => ['recordings', params] as const,
  recording: (id: number) => ['recording', id] as const,
  transcript: (id: number) => ['transcript', id] as const,
  insights: (id: number) => ['insights', id] as const,
  chat: (id: number) => ['chat', id] as const,
  tags: ['tags'] as const,
  settings: ['settings'] as const,
  search: (q: string) => ['search', q] as const,
  systemHealth: ['system', 'health'] as const,
  systemInfo: ['system', 'info'] as const,
  features: ['system', 'features'] as const,
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

export function buildQuery(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    search.set(key, String(value))
  })
  const qs = search.toString()
  return qs ? `?${qs}` : ''
}

export function mediaUrl(recordingId: number, download = false): string {
  return `/api/recordings/${recordingId}/media${download ? '?download=1' : ''}`
}

export function exportUrl(recordingId: number, format: 'txt' | 'srt' | 'md' | 'json'): string {
  return `/api/recordings/${recordingId}/transcript/export?format=${format}`
}

export function activePollingStatus(status: string | undefined): boolean {
  return status === 'uploaded' || status === 'queued' || status === 'processing'
}

/* -------------------------------------------------------------------------- */
/* Auth                                                                        */
/* -------------------------------------------------------------------------- */

export function useMe() {
  return useQuery<MeResponse>({
    queryKey: qk.me,
    queryFn: () => api.get<MeResponse>('/api/auth/me'),
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  })
}

export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation<LoginResponse, Error, { username: string; password: string }>({
    mutationFn: (payload) => api.post<LoginResponse>('/api/auth/login', payload),
    onSuccess: (data) => {
      queryClient.setQueryData(qk.me, { user: data.user })
      void queryClient.invalidateQueries()
    },
  })
}

export function useLogout() {
  const queryClient = useQueryClient()
  return useMutation<OkResponse, Error, void>({
    mutationFn: () => api.post<OkResponse>('/api/auth/logout'),
    onSettled: () => {
      queryClient.removeQueries()
      queryClient.setQueryData(qk.me, undefined)
    },
  })
}

export function useChangePassword() {
  return useMutation<OkResponse, Error, { old_password: string; new_password: string }>({
    mutationFn: (payload) => api.post<OkResponse>('/api/auth/change-password', payload),
  })
}

/* -------------------------------------------------------------------------- */
/* Recordings                                                                  */
/* -------------------------------------------------------------------------- */

export function useRecordings(params: RecordingListParams, enabled = true) {
  return useQuery<RecordingList>({
    queryKey: qk.recordings(params),
    queryFn: () => api.get<RecordingList>(`/api/recordings${buildQuery({ ...params })}`),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 10_000,
    refetchInterval: (query) => {
      const data = query.state.data
      if (document.visibilityState === 'hidden') return false
      if (data?.items.some((item) => activePollingStatus(item.status))) return 2500
      return false
    },
  })
}

export function useRecording(id: number, enabled = true) {
  return useQuery<RecordingDetail>({
    queryKey: qk.recording(id),
    queryFn: () => api.get<RecordingDetail>(`/api/recordings/${id}`),
    enabled: enabled && Number.isFinite(id) && id > 0,
    staleTime: 0,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (document.visibilityState === 'hidden') return false
      if (!status) return 2000
      return activePollingStatus(status) ? 2000 : false
    },
  })
}

export function useTranscript(id: number, enabled = true) {
  return useQuery<Transcript>({
    queryKey: qk.transcript(id),
    queryFn: () => api.get<Transcript>(`/api/recordings/${id}/transcript`),
    enabled: enabled && id > 0,
    retry: 1,
    staleTime: 60_000,
  })
}

export function useInsights(id: number, enabled = true, poll = false) {
  return useQuery<Insights>({
    queryKey: qk.insights(id),
    queryFn: () => api.get<Insights>(`/api/recordings/${id}/insights`),
    enabled: enabled && id > 0,
    retry: 1,
    staleTime: 60_000,
    refetchInterval: poll && document.visibilityState === 'visible' ? 3000 : false,
  })
}

export function useChatHistory(id: number, enabled = true) {
  return useQuery<ChatHistory>({
    queryKey: qk.chat(id),
    queryFn: () => api.get<ChatHistory>(`/api/recordings/${id}/chat`),
    enabled: enabled && id > 0,
    staleTime: Infinity,
    retry: 1,
  })
}

export function useUpdateRecording(id: number) {
  const queryClient = useQueryClient()
  return useMutation<RecordingDetail, Error, UpdateRecordingPayload>({
    mutationFn: (payload) => api.patch<RecordingDetail>(`/api/recordings/${id}`, payload),
    onSuccess: (data) => {
      queryClient.setQueryData(qk.recording(id), data)
      void queryClient.invalidateQueries({ queryKey: ['recordings'] })
    },
  })
}

export function useDeleteRecording() {
  const queryClient = useQueryClient()
  return useMutation<SoftDeleteResponse, Error, number>({
    mutationFn: (id) => api.delete<SoftDeleteResponse>(`/api/recordings/${id}`),
    onSuccess: (_data, id) => {
      void queryClient.invalidateQueries({ queryKey: ['recordings'] })
      void queryClient.invalidateQueries({ queryKey: qk.recording(id) })
    },
  })
}

export function useReprocessRecording(id: number) {
  const queryClient = useQueryClient()
  return useMutation<OkResponse, Error, 'all' | 'asr' | 'insights' | 'embeddings'>({
    mutationFn: (from) => api.post<OkResponse>(`/api/recordings/${id}/reprocess`, { from }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.recording(id) })
      void queryClient.invalidateQueries({ queryKey: ['recordings'] })
    },
  })
}

export function useCancelRecording(id: number) {
  const queryClient = useQueryClient()
  return useMutation<OkResponse, Error, void>({
    mutationFn: () => api.post<OkResponse>(`/api/recordings/${id}/cancel`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.recording(id) })
      void queryClient.invalidateQueries({ queryKey: ['recordings'] })
    },
  })
}

export function useUpdateSpeaker(recordingId: number) {
  const queryClient = useQueryClient()
  return useMutation<Speaker, Error, { speakerId: number; display_name: string; color?: string }>({
    mutationFn: ({ speakerId, display_name, color }) =>
      api.patch<Speaker>(`/api/recordings/${recordingId}/speakers/${speakerId}`, {
        display_name,
        ...(color ? { color } : {}),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.transcript(recordingId) })
      void queryClient.invalidateQueries({ queryKey: qk.recording(recordingId) })
    },
  })
}

export function useUpdateSegment(recordingId: number) {
  const queryClient = useQueryClient()
  return useMutation<Segment, Error, { segmentId: number; text?: string; speaker_id?: number }>({
    mutationFn: ({ segmentId, ...payload }) =>
      api.patch<Segment>(`/api/recordings/${recordingId}/segments/${segmentId}`, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.transcript(recordingId) })
    },
  })
}

export function useRegenerateInsights(recordingId: number) {
  const queryClient = useQueryClient()
  return useMutation<OkResponse, Error, string[] | undefined>({
    mutationFn: (kinds) =>
      api.post<OkResponse>(`/api/recordings/${recordingId}/insights/regenerate`, kinds ? { kinds } : {}),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.insights(recordingId) })
    },
  })
}

/* -------------------------------------------------------------------------- */
/* Tags / search / settings / system                                           */
/* -------------------------------------------------------------------------- */

export function useTags(enabled = true) {
  return useQuery<TagListResponse>({
    queryKey: qk.tags,
    queryFn: () => api.get<TagListResponse>('/api/tags'),
    enabled,
    staleTime: 5 * 60_000,
  })
}

export function useSearch(query: string) {
  const trimmed = query.trim()
  return useQuery<SearchResult>({
    queryKey: qk.search(trimmed),
    queryFn: () => api.get<SearchResult>(`/api/search${buildQuery({ q: trimmed, limit: 20 })}`),
    enabled: trimmed.length > 0,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}

export function useSettings(enabled = true) {
  return useQuery<Settings>({
    queryKey: qk.settings,
    queryFn: () => api.get<Settings>('/api/settings'),
    enabled,
    staleTime: 60_000,
    retry: 1,
  })
}

export function useSaveSettings() {
  const queryClient = useQueryClient()
  return useMutation<Settings, Error, Record<string, unknown>>({
    mutationFn: (payload) => api.put<Settings>('/api/settings', payload),
    onSuccess: (data) => {
      queryClient.setQueryData(qk.settings, data)
    },
  })
}

export function useTestProvider() {
  return useMutation<
    ProviderTestResult,
    Error,
    { kind: 'asr' | 'llm' | 'embeddings'; draft: Record<string, unknown> }
  >({
    mutationFn: (payload) => api.post<ProviderTestResult>('/api/settings/test', payload),
  })
}

export function useResetPrompts() {
  const queryClient = useQueryClient()
  return useMutation<Settings, Error, void>({
    mutationFn: () => api.post<Settings>('/api/settings/prompts/reset'),
    onSuccess: (data) => {
      queryClient.setQueryData(qk.settings, data)
    },
  })
}

export function useSystemHealth() {
  return useQuery<SystemHealth>({
    queryKey: qk.systemHealth,
    queryFn: () => api.get<SystemHealth>('/api/system/health'),
    retry: false,
    staleTime: 60_000,
  })
}

export function useSystemFeatures(enabled = true) {
  return useQuery<SystemFeatures>({
    queryKey: qk.features,
    queryFn: () => api.get<SystemFeatures>('/api/system/features'),
    enabled,
    staleTime: 60_000,
    retry: 1,
  })
}

export function useSystemInfo(enabled = true) {
  return useQuery<SystemInfo>({
    queryKey: qk.systemInfo,
    queryFn: () => api.get<SystemInfo>('/api/system/info'),
    enabled,
    retry: false,
    staleTime: 60_000,
  })
}

export function useConsumeScan() {
  const queryClient = useQueryClient()
  return useMutation<ConsumeScanResponse, Error, void>({
    mutationFn: () => api.post<ConsumeScanResponse>('/api/system/consume/scan'),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.systemInfo })
      void queryClient.invalidateQueries({ queryKey: ['recordings'] })
    },
  })
}

export function useSeedDemo() {
  const queryClient = useQueryClient()
  return useMutation<{ ok: boolean; created: number }, Error, void>({
    mutationFn: () => api.post<{ ok: boolean; created: number }>('/api/system/seed-demo'),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['recordings'] })
      void queryClient.invalidateQueries({ queryKey: qk.tags })
    },
  })
}

/* -------------------------------------------------------------------------- */
/* Misc helpers                                                                */
/* -------------------------------------------------------------------------- */

export function useInvalidateRecording() {
  const queryClient = useQueryClient()
  return (id: number) => {
    void queryClient.invalidateQueries({ queryKey: qk.recording(id) })
    void queryClient.invalidateQueries({ queryKey: qk.transcript(id) })
    void queryClient.invalidateQueries({ queryKey: qk.insights(id) })
  }
}

export interface CurrentUserResult {
  user: User | null
  loading: boolean
}

/** Convenience wrapper around useMe that maps auth state for the UI. */
export function useCurrentUser(): CurrentUserResult {
  const { data, isLoading } = useMe()
  return { user: data?.user ?? null, loading: isLoading }
}
