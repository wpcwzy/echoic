import { useEffect, useState } from 'react'

export interface BreakpointInfo {
  width: number
  isMobile: boolean
  isTablet: boolean
  isDesktop: boolean
  isWide: boolean
}

function readWidth(): number {
  if (typeof window === 'undefined') return 1440
  return window.innerWidth
}

/**
 * Viewport width hook.
 * - mobile:  < 768
 * - tablet:  768 – 1023
 * - desktop: >= 1024 (AI panel becomes a drawer)
 * - wide:    >= 1440 (three-column detail layout with inline AI panel)
 */
export function useBreakpoint(): BreakpointInfo {
  const [width, setWidth] = useState<number>(readWidth)

  useEffect(() => {
    let frame = 0
    const onResize = () => {
      if (frame) return
      frame = window.requestAnimationFrame(() => {
        frame = 0
        setWidth(window.innerWidth)
      })
    }
    window.addEventListener('resize', onResize, { passive: true })
    return () => {
      window.removeEventListener('resize', onResize)
      if (frame) window.cancelAnimationFrame(frame)
    }
  }, [])

  return {
    width,
    isMobile: width < 768,
    isTablet: width >= 768 && width < 1024,
    isDesktop: width >= 1024,
    isWide: width >= 1440,
  }
}
