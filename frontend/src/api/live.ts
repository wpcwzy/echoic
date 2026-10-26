import { useQuery } from '@tanstack/react-query'
import { ApiError, api } from './client'
import type { LiveSegment, LiveSession, LiveSessionCreatePayload, LiveSessionDetail, RecordingLive } from '../types'

export const liveKeys = {
  recording: (recordingId: number) => ['live', 'recording', recordingId] as const,
  session: (sessionId: number) => ['live', 'session', sessionId] as const,
}

export async function createLiveSession(payload: LiveSessionCreatePayload): Promise<LiveSession> {
  return api.post<LiveSession>('/api/live/sessions', payload)
}

export async function stopLiveSession(sessionId: number): Promise<{ ok: boolean; status?: string; total_audio_ms?: number }> {
  return api.post<{ ok: boolean; status?: string; total_audio_ms?: number }>(`/api/live/sessions/${sessionId}/stop`)
}

export async function cancelLiveSession(sessionId: number): Promise<{ ok: boolean; status?: string }> {
  return api.post<{ ok: boolean; status?: string }>(`/api/live/sessions/${sessionId}/cancel`)
}

export async function fetchLiveSegments(sessionId: number): Promise<LiveSegment[]> {
  const data = await api.get<{ segments: LiveSegment[] }>(`/api/live/sessions/${sessionId}/segments`)
  return data.segments ?? []
}

export async function fetchSessionDetail(sessionId: number): Promise<LiveSessionDetail> {
  return api.get<LiveSessionDetail>(`/api/live/sessions/${sessionId}`)
}

/** Live transcript bound to a recording; 404 simply means "no live session". */
export function useRecordingLive(recordingId: number, enabled = true) {
  return useQuery<RecordingLive | null>({
    queryKey: liveKeys.recording(recordingId),
    queryFn: async () => {
      try {
        return await api.get<RecordingLive>(`/api/recordings/${recordingId}/live`)
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null
        throw error
      }
    },
    enabled: enabled && recordingId > 0,
    retry: false,
    staleTime: 60_000,
  })
}
