import { useAuthStore } from '../store/auth'
import type { User, UserRole } from '../types'

/** True when the user has the admin role (role is authoritative, is_admin kept for compat). */
export function isAdminUser(user: Pick<User, 'role' | 'is_admin'> | null | undefined): boolean {
  if (!user) return false
  return user.role === 'admin' || user.is_admin === true
}

export const ROLE_META: Record<UserRole, { label: string; color: string }> = {
  admin: { label: '管理员', color: 'blue' },
  member: { label: '成员', color: 'default' },
}

export function roleLabel(role: UserRole | string | null | undefined): string {
  if (role === 'admin') return '管理员'
  if (role === 'member') return '成员'
  return '未知角色'
}

/** Hook form: subscribe to the current session's admin capability. */
export function useIsAdmin(): boolean {
  const user = useAuthStore((state) => state.user)
  return isAdminUser(user)
}
