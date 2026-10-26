import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import { buildQuery, qk } from './queries'
import type {
  OkResponse,
  UserAdmin,
  UserCreatePayload,
  UserListParams,
  UserListResponse,
  UserUpdatePayload,
} from '../types'

export const userKeys = {
  all: ['users'] as const,
  list: (params: UserListParams) => ['users', params] as const,
}

/** Admin user directory with server-side search, role filter and pagination. */
export function useUsers(params: UserListParams, enabled = true) {
  return useQuery<UserListResponse>({
    queryKey: userKeys.list(params),
    queryFn: () => api.get<UserListResponse>(`/api/users${buildQuery({ ...params })}`),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 15_000,
  })
}

function invalidateUsers(queryClient: ReturnType<typeof useQueryClient>): void {
  void queryClient.invalidateQueries({ queryKey: userKeys.all })
}

export function useCreateUser() {
  const queryClient = useQueryClient()
  return useMutation<UserAdmin, Error, UserCreatePayload>({
    mutationFn: (payload) => api.post<UserAdmin>('/api/users', payload),
    onSuccess: () => invalidateUsers(queryClient),
  })
}

export function useUpdateUser() {
  const queryClient = useQueryClient()
  return useMutation<UserAdmin, Error, { id: number; payload: UserUpdatePayload }>({
    mutationFn: ({ id, payload }) => api.patch<UserAdmin>(`/api/users/${id}`, payload),
    onSuccess: () => {
      invalidateUsers(queryClient)
      void queryClient.invalidateQueries({ queryKey: qk.me })
    },
  })
}

export function useResetUserPassword() {
  return useMutation<OkResponse, Error, { id: number; new_password: string }>({
    mutationFn: ({ id, new_password }) =>
      api.post<OkResponse>(`/api/users/${id}/reset-password`, { new_password }),
  })
}

export function useDeleteUser() {
  const queryClient = useQueryClient()
  return useMutation<OkResponse, Error, number>({
    mutationFn: (id) => api.delete<OkResponse>(`/api/users/${id}`),
    onSuccess: () => invalidateUsers(queryClient),
  })
}
