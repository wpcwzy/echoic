import { create } from 'zustand'

export type ThemeMode = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'echoic:theme-mode'

export function normalizeThemeMode(value: string | null | undefined): ThemeMode {
  return value === 'light' || value === 'dark' || value === 'system' ? value : 'system'
}

export function readStoredThemeMode(): ThemeMode {
  try {
    return normalizeThemeMode(localStorage.getItem(THEME_STORAGE_KEY))
  } catch {
    return 'system'
  }
}

export function systemPrefersDark(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  } catch {
    return false
  }
}

export function resolveThemeMode(mode: ThemeMode, prefersDark: boolean): ResolvedTheme {
  if (mode === 'dark') return 'dark'
  if (mode === 'light') return 'light'
  return prefersDark ? 'dark' : 'light'
}

/** Writes the resolved theme onto <html> so CSS variables & color-scheme apply. */
export function applyResolvedTheme(resolved: ResolvedTheme): void {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  root.setAttribute('data-theme', resolved)
  root.style.colorScheme = resolved
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute('content', resolved === 'dark' ? '#0f1115' : '#ffffff')
}

interface ThemeState {
  mode: ThemeMode
  systemDark: boolean
  setMode: (mode: ThemeMode) => void
  setSystemDark: (dark: boolean) => void
}

export const useThemeStore = create<ThemeState>((set) => ({
  mode: readStoredThemeMode(),
  systemDark: systemPrefersDark(),
  setMode: (mode) => {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, mode)
    } catch {
      /* ignore storage failures (private mode) */
    }
    set({ mode })
  },
  setSystemDark: (systemDark) => set({ systemDark }),
}))

/** Resolved theme for components outside the ConfigProvider root (e.g. mind map). */
export function useResolvedTheme(): ResolvedTheme {
  const mode = useThemeStore((state) => state.mode)
  const systemDark = useThemeStore((state) => state.systemDark)
  return resolveThemeMode(mode, systemDark)
}
