import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { OkResponse, ShareCreatePayload, ShareInfo, ShareListResponse } from '../types'

export const shareKeys = {
  recording: (recordingId: number) => ['shares', 'recording', recordingId] as const,
}

/** Absolute, copyable share URL for a ShareOut.url_path. */
export function shareAbsoluteUrl(urlPath: string): string {
  const path = urlPath.startsWith('/') ? urlPath : `/${urlPath}`
  if (typeof window === 'undefined') return path
  return `${window.location.origin}${path}`
}

export function useRecordingShares(recordingId: number, enabled = true) {
  return useQuery<ShareListResponse>({
    queryKey: shareKeys.recording(recordingId),
    queryFn: () => api.get<ShareListResponse>(`/api/recordings/${recordingId}/shares`),
    enabled: enabled && recordingId > 0,
    staleTime: 10_000,
    retry: 1,
  })
}

export function useCreateShare(recordingId: number) {
  const queryClient = useQueryClient()
  return useMutation<ShareInfo, Error, ShareCreatePayload>({
    mutationFn: (payload) => api.post<ShareInfo>(`/api/recordings/${recordingId}/shares`, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: shareKeys.recording(recordingId) })
    },
  })
}

export function useRevokeShare(recordingId: number) {
  const queryClient = useQueryClient()
  return useMutation<OkResponse, Error, number>({
    mutationFn: (shareId) => api.delete<OkResponse>(`/api/shares/${shareId}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: shareKeys.recording(recordingId) })
    },
  })
}
