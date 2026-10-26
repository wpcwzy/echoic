import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import { qk } from './queries'
import type {
  BatchActionPayload,
  BatchResponse,
  OkResponse,
  PurgeResponse,
  RecordingSummary,
} from '../types'

export interface BatchCancelSummary {
  succeeded: number
  failed: number
  failures: { id: number; error: string }[]
}

function invalidateAfterTrashChange(queryClient: ReturnType<typeof useQueryClient>, recordingId?: number): void {
  void queryClient.invalidateQueries({ queryKey: ['recordings'] })
  void queryClient.invalidateQueries({ queryKey: qk.tags })
  if (recordingId) {
    void queryClient.invalidateQueries({ queryKey: qk.recording(recordingId) })
    void queryClient.invalidateQueries({ queryKey: qk.transcript(recordingId) })
    void queryClient.invalidateQueries({ queryKey: qk.insights(recordingId) })
  }
}

/** POST /api/recordings/batch — partial success is expected by design. */
export function useBatchRecordings() {
  const queryClient = useQueryClient()
  return useMutation<BatchResponse, Error, BatchActionPayload>({
    mutationFn: (payload) => api.post<BatchResponse>('/api/recordings/batch', payload),
    onSuccess: () => invalidateAfterTrashChange(queryClient),
  })
}

/** POST /api/recordings/{id}/restore */
export function useRestoreRecording() {
  const queryClient = useQueryClient()
  return useMutation<RecordingSummary, Error, number>({
    mutationFn: (id) => api.post<RecordingSummary>(`/api/recordings/${id}/restore`),
    onSuccess: (_data, id) => invalidateAfterTrashChange(queryClient, id),
  })
}

/** DELETE /api/recordings/{id}/purge — permanent, irreversible. */
export function usePurgeRecording() {
  const queryClient = useQueryClient()
  return useMutation<PurgeResponse, Error, number>({
    mutationFn: (id) => api.delete<PurgeResponse>(`/api/recordings/${id}/purge`),
    onSuccess: (_data, id) => invalidateAfterTrashChange(queryClient, id),
  })
}

/** Cancels processing for the given ids one by one (v0.2 endpoint, no batch variant). */
export function useBatchCancel() {
  const queryClient = useQueryClient()
  return useMutation<BatchCancelSummary, Error, number[]>({
    mutationFn: async (ids) => {
      const summary: BatchCancelSummary = { succeeded: 0, failed: 0, failures: [] }
      for (const id of ids) {
        try {
          await api.post<OkResponse>(`/api/recordings/${id}/cancel`)
          summary.succeeded += 1
        } catch (error) {
          summary.failed += 1
          summary.failures.push({ id, error: error instanceof Error ? error.message : '取消失败' })
        }
      }
      return summary
    },
    onSuccess: () => invalidateAfterTrashChange(queryClient),
  })
}
