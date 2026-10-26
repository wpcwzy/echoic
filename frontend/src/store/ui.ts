import { create } from 'zustand'

const DEMO_BANNER_KEY = 'echoic:demo-banner-dismissed'

function readDemoDismissed(): boolean {
  try {
    return sessionStorage.getItem(DEMO_BANNER_KEY) === '1'
  } catch {
    return false
  }
}

interface UiState {
  /** User preference: is the inline AI panel collapsed on wide screens? */
  aiPanelCollapsed: boolean
  /** Drawer state for narrow screens. */
  aiDrawerOpen: boolean
  demoBannerDismissed: boolean
  toggleAiPanel: () => void
  setAiPanelCollapsed: (collapsed: boolean) => void
  setAiDrawerOpen: (open: boolean) => void
  dismissDemoBanner: () => void
}

export const useUiStore = create<UiState>((set) => ({
  aiPanelCollapsed: false,
  aiDrawerOpen: false,
  demoBannerDismissed: readDemoDismissed(),
  toggleAiPanel: () => set((state) => ({ aiPanelCollapsed: !state.aiPanelCollapsed })),
  setAiPanelCollapsed: (collapsed) => set({ aiPanelCollapsed: collapsed }),
  setAiDrawerOpen: (open) => set({ aiDrawerOpen: open }),
  dismissDemoBanner: () => {
    try {
      sessionStorage.setItem(DEMO_BANNER_KEY, '1')
    } catch {
      /* ignore quota / privacy errors */
    }
    set({ demoBannerDismissed: true })
  },
}))
