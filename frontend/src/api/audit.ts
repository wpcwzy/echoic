import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from './client'
import { buildQuery } from './queries'
import type { AuditLogListResponse, AuditLogParams } from '../types'

export const auditKeys = {
  list: (params: AuditLogParams) => ['audit-logs', params] as const,
}

/** Admin-only audit log list with server-side filters & pagination. */
export function useAuditLogs(params: AuditLogParams, enabled = true) {
  return useQuery<AuditLogListResponse>({
    queryKey: auditKeys.list(params),
    queryFn: () => api.get<AuditLogListResponse>(`/api/system/audit-logs${buildQuery({ ...params })}`),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 10_000,
    retry: 1,
  })
}
