import { useEffect, useMemo } from 'react'
import { Avatar, Dropdown, Tooltip } from 'antd'
import type { MenuProps } from 'antd'
import {
  AppstoreOutlined,
  AuditOutlined,
  AudioOutlined,
  BgColorsOutlined,
  CheckOutlined,
  DesktopOutlined,
  LogoutOutlined,
  MoonOutlined,
  ProfileOutlined,
  QuestionCircleOutlined,
  SearchOutlined,
  SettingOutlined,
  SunOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useLogout } from '../api/queries'
import { useAuthStore } from '../store/auth'
import { useThemeStore, type ThemeMode } from '../store/theme'
import { colorFromString, initials } from '../utils/format'
import { isAdminUser, roleLabel } from '../utils/roles'
import { toast } from '../utils/toast'
import { BrandMark } from '../components/common/BrandMark'

interface NavItem {
  to: string
  label: string
  icon: React.ReactNode
  match: (pathname: string) => boolean
}

const PRIMARY_NAV: NavItem[] = [
  { to: '/', label: '资料库', icon: <AppstoreOutlined />, match: (pathname) => pathname === '/' || pathname.startsWith('/r/') },
  { to: '/record', label: '录音', icon: <AudioOutlined />, match: (pathname) => pathname.startsWith('/record') },
  { to: '/search', label: '搜索', icon: <SearchOutlined />, match: (pathname) => pathname.startsWith('/search') },
]

const USERS_NAV: NavItem = {
  to: '/users',
  label: '用户管理',
  icon: <TeamOutlined />,
  match: (pathname) => pathname.startsWith('/users'),
}

const AUDIT_NAV: NavItem = {
  to: '/audit',
  label: '审计日志',
  icon: <AuditOutlined />,
  match: (pathname) => pathname.startsWith('/audit'),
}

const SETTINGS_NAV: NavItem = {
  to: '/settings',
  label: '设置',
  icon: <SettingOutlined />,
  match: (pathname) => pathname.startsWith('/settings'),
}

const SECTION_TITLES: { match: (path: string) => boolean; title: string }[] = [
  { match: (path) => path.startsWith('/record'), title: '录音与上传' },
  { match: (path) => path.startsWith('/search'), title: '全局搜索' },
  { match: (path) => path.startsWith('/users'), title: '用户管理' },
  { match: (path) => path.startsWith('/audit'), title: '审计日志' },
  { match: (path) => path.startsWith('/profile'), title: '个人资料' },
  { match: (path) => path.startsWith('/settings'), title: '设置' },
  { match: () => true, title: '资料库' },
]

/**
 * Application shell: 64px icon rail + top bar + routed content.
 * The detail page (`/r/:id`) renders its own top bar and uses the full height.
 * Admin-only entries (用户管理 / 设置) are hidden for member accounts.
 */
export default function AppShell() {
  const location = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const logout = useLogout()
  const user = useAuthStore((state) => state.user)
  const reset = useAuthStore((state) => state.reset)
  const themeMode = useThemeStore((state) => state.mode)
  const setThemeMode = useThemeStore((state) => state.setMode)

  const isAdmin = isAdminUser(user)
  const isDetail = location.pathname.startsWith('/r/')
  const sectionTitle = useMemo(
    () => SECTION_TITLES.find((entry) => entry.match(location.pathname))?.title ?? 'Echoic',
    [location.pathname],
  )

  const railNav = useMemo<NavItem[]>(
    () => (isAdmin ? [...PRIMARY_NAV, USERS_NAV, AUDIT_NAV] : PRIMARY_NAV),
    [isAdmin],
  )
  const mobileNav = useMemo<NavItem[]>(
    () => (isAdmin ? [...PRIMARY_NAV, USERS_NAV, AUDIT_NAV, SETTINGS_NAV] : PRIMARY_NAV),
    [isAdmin],
  )

  // Reset the content scroll position when the route changes.
  useEffect(() => {
    const scroller = document.querySelector('.app-content')
    if (scroller instanceof HTMLElement) scroller.scrollTo({ top: 0 })
  }, [location.pathname])

  // Global search shortcut: ⌘K / Ctrl+K.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        navigate('/search')
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [navigate])

  const handleLogout = () => {
    logout.mutate(undefined, {
      onSettled: () => {
        reset()
        queryClient.clear()
        toast.success('已退出登录')
        navigate('/login', { replace: true })
      },
    })
  }

  const displayName = user?.display_name || user?.username || '未登录'
  const avatarColor = colorFromString(displayName)
  const currentRole = roleLabel(user?.role)

  const themeMenuLabel = (mode: ThemeMode, label: string) => (
    <span className="theme-menu-item">
      <span>{label}</span>
      {themeMode === mode && <CheckOutlined className="theme-menu-item__check" />}
    </span>
  )

  const userMenu: MenuProps['items'] = [
    {
      key: 'account',
      label: (
        <div className="user-menu__header">
          <div className="user-menu__name">
            {displayName}
            <span className={`role-chip role-chip--${user?.role ?? 'member'}`}>{currentRole}</span>
          </div>
          <div className="user-menu__meta">
            @{user?.username ?? '—'}
            {user?.email ? ` · ${user.email}` : ''}
          </div>
        </div>
      ),
      disabled: true,
    },
    { type: 'divider' },
    {
      key: 'theme',
      icon: <BgColorsOutlined />,
      label: '外观',
      children: [
        { key: 'theme-light', icon: <SunOutlined />, label: themeMenuLabel('light', '浅色') },
        { key: 'theme-dark', icon: <MoonOutlined />, label: themeMenuLabel('dark', '深色') },
        { key: 'theme-system', icon: <DesktopOutlined />, label: themeMenuLabel('system', '跟随系统') },
      ],
    },
    { key: 'profile', icon: <ProfileOutlined />, label: '个人资料' },
    ...(isAdmin ? [{ key: 'settings', icon: <UserOutlined />, label: '账户与设置' } as const] : []),
    { key: 'logout', icon: <LogoutOutlined />, label: '退出登录', danger: true },
  ]

  const onUserMenuClick: MenuProps['onClick'] = ({ key }) => {
    if (key === 'logout') handleLogout()
    if (key === 'profile') navigate('/profile')
    if (key === 'settings') navigate('/settings?tab=account')
    if (key.startsWith('theme-')) {
      const nextMode = key.replace('theme-', '') as ThemeMode
      setThemeMode(nextMode)
      const label = nextMode === 'light' ? '浅色' : nextMode === 'dark' ? '深色' : '跟随系统'
      toast.success(`外观已切换为「${label}」`)
    }
  }

  const navLink = (item: NavItem, className: string) => (
    <Tooltip key={item.to} title={item.label} placement={className.includes('mobile') ? 'top' : 'right'}>
      <NavLink
        to={item.to}
        className={({ isActive }) =>
          `${className} ${isActive || item.match(location.pathname) ? `${className}--active` : ''}`
        }
        aria-label={item.label}
      >
        <span className="app-rail__icon">{item.icon}</span>
        {className.includes('mobile') && <span className="mobile-nav__label">{item.label}</span>}
      </NavLink>
    </Tooltip>
  )

  return (
    <div className={`app-shell ${isDetail ? 'app-shell--detail' : ''}`}>
      <aside className="app-rail" aria-label="主导航">
        <div className="app-rail__brand">
          <NavLink to="/" aria-label="Echoic 首页" className="app-rail__brand-link">
            <BrandMark size={38} />
          </NavLink>
        </div>
        <nav className="app-rail__nav">
          {railNav.map((item) => navLink(item, 'app-rail__item'))}
        </nav>
        <div className="app-rail__bottom">
          {isAdmin && navLink(SETTINGS_NAV, 'app-rail__item')}
          <Dropdown menu={{ items: userMenu, onClick: onUserMenuClick }} trigger={['click']} placement="topRight">
            <button type="button" className="app-rail__user" aria-label="账户菜单">
              <Avatar size={32} style={{ background: avatarColor, fontSize: 13 }}>
                {initials(displayName, 1)}
              </Avatar>
            </button>
          </Dropdown>
        </div>
      </aside>

      <div className="app-shell__body">
        {!isDetail && (
          <header className="app-topbar">
            <div className="app-topbar__left">
              <span className="app-topbar__crumb">{sectionTitle}</span>
            </div>
            <div className="app-topbar__right">
              <button type="button" className="topbar-search" onClick={() => navigate('/search')}>
                <SearchOutlined />
                <span className="topbar-search__text">搜索录音、转录或提问</span>
                <kbd className="topbar-search__kbd">⌘K</kbd>
              </button>
              {isAdmin && (
                <Tooltip title="帮助与关于">
                  <button
                    type="button"
                    className="topbar-icon-btn"
                    aria-label="帮助"
                    onClick={() => navigate('/settings?tab=about')}
                  >
                    <QuestionCircleOutlined />
                  </button>
                </Tooltip>
              )}
              <Dropdown menu={{ items: userMenu, onClick: onUserMenuClick }} trigger={['click']} placement="bottomRight">
                <button type="button" className="topbar-user" aria-label="账户菜单">
                  <Avatar size={28} style={{ background: avatarColor, fontSize: 12 }}>
                    {initials(displayName, 1)}
                  </Avatar>
                  <span className="topbar-user__name">{displayName}</span>
                </button>
              </Dropdown>
            </div>
          </header>
        )}
        <main className={`app-content ${isDetail ? 'app-content--detail' : ''}`}>
          <Outlet />
        </main>
      </div>

      <nav className="mobile-nav" aria-label="移动端导航">
        {mobileNav.map((item) => navLink(item, 'mobile-nav__item'))}
      </nav>
    </div>
  )
}
