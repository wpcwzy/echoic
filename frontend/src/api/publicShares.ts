import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from './client'
import type { Insights, PublicShareInfo, PublicShareResponse, ShareUnlockResponse, Transcript } from '../types'

export const publicShareKeys = {
  meta: (token: string) => ['public-share', token] as const,
  access: (token: string) => ['public-share-access', token] as const,
  transcript: (token: string) => ['public-share-transcript', token] as const,
  insights: (token: string) => ['public-share-insights', token] as const,
}

export function publicMediaUrl(token: string): string {
  return `/api/public/shares/${encodeURIComponent(token)}/media`
}

export function publicDownloadUrl(token: string): string {
  return `/api/public/shares/${encodeURIComponent(token)}/download`
}

export async function fetchPublicShare(token: string): Promise<PublicShareResponse> {
  return api.get<PublicShareResponse>(`/api/public/shares/${encodeURIComponent(token)}`)
}

export async function unlockPublicShare(token: string, password: string): Promise<ShareUnlockResponse> {
  return api.post<ShareUnlockResponse>(`/api/public/shares/${encodeURIComponent(token)}/unlock`, { password })
}

export async function fetchPublicTranscript(token: string): Promise<Transcript> {
  return api.get<Transcript>(`/api/public/shares/${encodeURIComponent(token)}/transcript`)
}

export async function fetchPublicInsights(token: string): Promise<Insights> {
  return api.get<Insights>(`/api/public/shares/${encodeURIComponent(token)}/insights`)
}

/**
 * Probe whether the current browser already holds a valid share unlock cookie.
 * Uses a protected endpoint that is guaranteed to be enabled for this share.
 * Returns `false` only for "password required" 401s; other errors bubble up.
 */
export async function probePublicAccess(token: string, share: PublicShareInfo): Promise<boolean> {
  try {
    if (share.include_transcript) {
      await fetchPublicTranscript(token)
      return true
    }
    if (share.include_insights) {
      await fetchPublicInsights(token)
      return true
    }
    const response = await fetch(publicMediaUrl(token), {
      credentials: 'include',
      headers: { Range: 'bytes=0-0' },
    })
    if (response.status === 401) return false
    if (response.ok) {
      // Drain a tiny response body so the connection can be reused.
      await response.arrayBuffer().catch(() => undefined)
      return true
    }
    return response.status !== 403
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return false
    throw error
  }
}

export function usePublicShare(token: string) {
  return useQuery<PublicShareResponse>({
    queryKey: publicShareKeys.meta(token),
    queryFn: () => fetchPublicShare(token),
    enabled: Boolean(token),
    retry: false,
    staleTime: 30_000,
  })
}

/** Silent probe used when the share requires a password. */
export function usePublicAccessProbe(token: string, share: PublicShareInfo | undefined) {
  return useQuery<boolean>({
    queryKey: publicShareKeys.access(token),
    queryFn: () => probePublicAccess(token, share as PublicShareInfo),
    enabled: Boolean(token && share?.has_password),
    retry: false,
    staleTime: 60_000,
  })
}

export function useUnlockPublicShare(token: string) {
  const queryClient = useQueryClient()
  return useMutation<ShareUnlockResponse, Error, string>({
    mutationFn: (password) => unlockPublicShare(token, password),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: publicShareKeys.access(token) })
    },
  })
}

export function usePublicTranscript(token: string, enabled: boolean) {
  return useQuery<Transcript>({
    queryKey: publicShareKeys.transcript(token),
    queryFn: () => fetchPublicTranscript(token),
    enabled: enabled && Boolean(token),
    retry: 1,
    staleTime: 60_000,
  })
}

export function usePublicInsights(token: string, enabled: boolean) {
  return useQuery<Insights>({
    queryKey: publicShareKeys.insights(token),
    queryFn: () => fetchPublicInsights(token),
    enabled: enabled && Boolean(token),
    retry: 1,
    staleTime: 60_000,
  })
}
