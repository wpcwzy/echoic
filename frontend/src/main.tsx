import React, { useEffect, useMemo } from 'react'
import ReactDOM from 'react-dom/client'
import { App as AntdApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, useLocation } from 'react-router-dom'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import { buildAntdTheme } from './theme'
import { applyResolvedTheme, resolveThemeMode, useThemeStore } from './store/theme'
import App from './App'
import '@xyflow/react/dist/style.css'
import './styles/global.css'
import './styles/app.css'

dayjs.locale('zh-cn')

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
})

/** Applies theme to <html>, listens to the OS preference and feeds AntD. */
function ThemedRoot() {
  const location = useLocation()
  const mode = useThemeStore((state) => state.mode)
  const systemDark = useThemeStore((state) => state.systemDark)
  const setSystemDark = useThemeStore((state) => state.setSystemDark)

  const isPublicShare = location.pathname.startsWith('/s/')
  // Public share pages always follow the visitor's system preference.
  const resolved = isPublicShare ? (systemDark ? 'dark' : 'light') : resolveThemeMode(mode, systemDark)

  useEffect(() => {
    applyResolvedTheme(resolved)
  }, [resolved])

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches)
    setSystemDark(query.matches)
    if (typeof query.addEventListener === 'function') {
      query.addEventListener('change', onChange)
      return () => query.removeEventListener('change', onChange)
    }
    query.addListener(onChange)
    return () => query.removeListener(onChange)
  }, [setSystemDark])

  const antdTheme = useMemo(() => buildAntdTheme(resolved), [resolved])

  return (
    <ConfigProvider locale={zhCN} theme={antdTheme}>
      <AntdApp>
        <QueryClientProvider client={queryClient}>
          <App />
        </QueryClientProvider>
      </AntdApp>
    </ConfigProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <ThemedRoot />
    </BrowserRouter>
  </React.StrictMode>,
)
