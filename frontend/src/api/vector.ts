import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, buildQueryUrl } from './client'
import type { SearchMode, VectorReindexPayload, VectorReindexResult, VectorSearchResult, VectorStatus } from '../types'

export const vectorKeys = {
  status: ['vector', 'status'] as const,
  search: (query: string, mode: SearchMode) => ['vector', 'search', mode, query] as const,
}

export function useVectorStatus(enabled = true) {
  return useQuery<VectorStatus>({
    queryKey: vectorKeys.status,
    queryFn: () => api.get<VectorStatus>('/api/vector/status'),
    enabled,
    retry: 1,
    staleTime: 30_000,
  })
}

export function useVectorReindex() {
  const queryClient = useQueryClient()
  return useMutation<VectorReindexResult, Error, VectorReindexPayload>({
    mutationFn: (payload) => api.post<VectorReindexResult>('/api/vector/reindex', payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: vectorKeys.status })
    },
  })
}

/** Semantic / hybrid search; keyword mode keeps using GET /api/search. */
export function useVectorSearch(query: string, mode: Exclude<SearchMode, 'keyword'>, enabled = true) {
  const trimmed = query.trim()
  return useQuery<VectorSearchResult>({
    queryKey: vectorKeys.search(trimmed, mode),
    queryFn: () =>
      api.get<VectorSearchResult>(`/api/vector/search${buildQueryUrl({ q: trimmed, mode, limit: 12 })}`),
    enabled: enabled && trimmed.length > 0,
    placeholderData: keepPreviousData,
    retry: false,
    staleTime: 30_000,
  })
}
