import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type { VideoSceneListResponse } from '../types'

export const sceneKeys = {
  list: (recordingId: number) => ['recording', recordingId, 'scenes'] as const,
}

/** Video scene/slide thumbnails for the filmstrip (auth required). */
export function useRecordingScenes(recordingId: number, enabled = true) {
  return useQuery<VideoSceneListResponse>({
    queryKey: sceneKeys.list(recordingId),
    queryFn: () => api.get<VideoSceneListResponse>(`/api/recordings/${recordingId}/scenes`),
    enabled: enabled && recordingId > 0,
    staleTime: 5 * 60_000,
    retry: 1,
  })
}

/** Authenticated scene thumbnail image URL (same-origin cookie is sent by <img>). */
export function sceneImageUrl(recordingId: number, sceneId: number): string {
  return `/api/recordings/${recordingId}/scenes/${sceneId}/image`
}
