import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'

interface LevelMeterProps {
  analyserRef: RefObject<AnalyserNode | null>
  active: boolean
  paused?: boolean
  height?: number
  bars?: number
}

/**
 * Canvas level meter driven by a WebAudio AnalyserNode.
 * Draws a symmetric bar spectrum; when idle it shows a calm flat baseline.
 */
export function LevelMeter({ analyserRef, active, paused = false, height = 72, bars = 56 }: LevelMeterProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    let frame = 0
    let data: Uint8Array | null = null

    const resize = () => {
      const dpr = window.devicePixelRatio || 1
      const width = canvas.clientWidth || 480
      canvas.width = Math.max(1, Math.floor(width * dpr))
      canvas.height = Math.max(1, Math.floor(height * dpr))
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()
    const observer = new ResizeObserver(resize)
    observer.observe(canvas)

    const drawIdle = () => {
      const width = canvas.clientWidth || 480
      const gap = 3
      const barWidth = Math.max(2, (width - gap * (bars - 1)) / bars)
      ctx.clearRect(0, 0, width, height)
      const center = height / 2
      for (let i = 0; i < bars; i += 1) {
        const x = i * (barWidth + gap)
        const amplitude = 4 + Math.sin(i * 0.6) * 2
        ctx.fillStyle = 'rgba(22, 119, 255, 0.18)'
        roundRect(ctx, x, center - amplitude / 2, barWidth, Math.max(3, amplitude), barWidth / 2)
        ctx.fill()
      }
    }

    const draw = () => {
      const analyser = analyserRef.current
      const width = canvas.clientWidth || 480
      const gap = 3
      const barWidth = Math.max(2, (width - gap * (bars - 1)) / bars)
      ctx.clearRect(0, 0, width, height)
      const center = height / 2
      const gradient = ctx.createLinearGradient(0, 0, width, 0)
      gradient.addColorStop(0, '#5b9dff')
      gradient.addColorStop(0.5, '#1677ff')
      gradient.addColorStop(1, '#69c0ff')

      if (active && analyser) {
        const buffer = data ?? new Uint8Array(analyser.frequencyBinCount)
        data = buffer
        analyser.getByteFrequencyData(buffer)
        const step = Math.max(1, Math.floor(buffer.length / bars))
        for (let i = 0; i < bars; i += 1) {
          const value = buffer[Math.min(buffer.length - 1, i * step)] / 255
          const amplitude = paused ? 4 : Math.max(4, value * (height - 10))
          const x = i * (barWidth + gap)
          ctx.fillStyle = gradient
          if (paused) ctx.globalAlpha = 0.4
          roundRect(ctx, x, center - amplitude / 2, barWidth, amplitude, barWidth / 2)
          ctx.fill()
          ctx.globalAlpha = 1
        }
      } else {
        drawIdle()
      }
      frame = requestAnimationFrame(draw)
    }

    draw()
    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
    }
  }, [active, analyserRef, bars, height, paused])

  return <canvas ref={canvasRef} className="record-level-meter" style={{ height }} aria-hidden="true" />
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
): void {
  const r = Math.min(radius, width / 2, height / 2)
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + width, y, x + width, y + height, r)
  ctx.arcTo(x + width, y + height, x, y + height, r)
  ctx.arcTo(x, y + height, x, y, r)
  ctx.arcTo(x, y, x + width, y, r)
  ctx.closePath()
}
