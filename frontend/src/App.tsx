import { useEffect, useRef } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { App as AntdApp, Spin } from 'antd'
import { bindFeedback, toast } from './utils/toast'
import { useMe } from './api/queries'
import { useAuthStore } from './store/auth'
import { isAdminUser } from './utils/roles'
import AppShell from './layouts/AppShell'
import LoginPage from './pages/LoginPage'
import LibraryPage from './pages/LibraryPage'
import RecordPage from './pages/RecordPage'
import RecordingDetailPage from './pages/RecordingDetailPage'
import SearchPage from './pages/SearchPage'
import SettingsPage from './pages/SettingsPage'
import UsersPage from './pages/UsersPage'
import PublicSharePage from './pages/PublicSharePage'
import { NotFoundPage } from './pages/NotFoundPage'
import { BrandMark } from './components/common/BrandMark'

/** Bridges AntD's context-aware message/modal API into the static helpers. */
function FeedbackBinder() {
  const { message, modal } = AntdApp.useApp()
  useEffect(() => {
    bindFeedback({ message, modal })
  }, [message, modal])
  return null
}

/** Boots the session from GET /api/auth/me and mirrors it into the auth store. */
function AuthBootstrap() {
  const setUser = useAuthStore((state) => state.setUser)
  const setStatus = useAuthStore((state) => state.setStatus)
  const { data, isLoading, isError, isSuccess } = useMe()

  useEffect(() => {
    if (isLoading) {
      setStatus('loading')
      return
    }
    if (data?.user) {
      setUser(data.user)
      return
    }
    if (isError || isSuccess) {
      setUser(null)
    }
  }, [data, isError, isLoading, isSuccess, setStatus, setUser])

  return null
}

function BootSplash() {
  return (
    <div className="boot-splash">
      <BrandMark size={56} />
      <div className="boot-splash__title">Echoic</div>
      <div className="boot-splash__desc">正在加载你的会议知识库…</div>
      <Spin size="small" />
    </div>
  )
}

function RequireAuth() {
  const status = useAuthStore((state) => state.status)
  const location = useLocation()

  if (status === 'loading') return <BootSplash />
  if (status === 'guest') {
    return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}` }} />
  }
  return <Outlet />
}

/**
 * Guards admin-only pages (用户管理 / 设置) for member accounts:
 * redirects to the library and explains why once.
 */
function RequireAdmin() {
  const status = useAuthStore((state) => state.status)
  const user = useAuthStore((state) => state.user)
  const notifiedRef = useRef(false)

  useEffect(() => {
    if (status !== 'loading' && user && !isAdminUser(user) && !notifiedRef.current) {
      notifiedRef.current = true
      toast.warning('该页面仅管理员可访问')
    }
  }, [status, user])

  if (status === 'loading') return <BootSplash />
  if (!isAdminUser(user)) return <Navigate to="/" replace />
  return <Outlet />
}

export default function App() {
  return (
    <>
      <FeedbackBinder />
      <AuthBootstrap />
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        {/* Public share page: no AppShell, no login required, never redirects to /login. */}
        <Route path="/s/:token" element={<PublicSharePage />} />
        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route path="/" element={<LibraryPage />} />
            <Route path="/record" element={<RecordPage />} />
            <Route path="/r/:id" element={<RecordingDetailPage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route element={<RequireAdmin />}>
              <Route path="users" element={<UsersPage />} />
              <Route path="settings" element={<SettingsPage />} />
            </Route>
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Route>
      </Routes>
    </>
  )
}
