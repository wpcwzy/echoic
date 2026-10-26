import { create } from 'zustand'
import type { User } from '../types'

export type AuthStatus = 'loading' | 'authed' | 'guest'

interface AuthState {
  user: User | null
  status: AuthStatus
  setUser: (user: User | null) => void
  setStatus: (status: AuthStatus) => void
  reset: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  status: 'loading',
  setUser: (user) => set({ user, status: user ? 'authed' : 'guest' }),
  setStatus: (status) => set({ status }),
  reset: () => set({ user: null, status: 'guest' }),
}))
