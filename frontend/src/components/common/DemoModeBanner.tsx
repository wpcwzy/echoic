import { CloseOutlined, InfoCircleOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import { useSettings } from '../../api/queries'
import { useUiStore } from '../../store/ui'
import { useIsAdmin } from '../../utils/roles'

interface DemoModeBannerProps {
  className?: string
}

/** Thin dismissible banner shown when the backend runs with demo_mode enabled. */
export function DemoModeBanner({ className }: DemoModeBannerProps) {
  const isAdmin = useIsAdmin()
  const { data } = useSettings(isAdmin)
  const dismissed = useUiStore((state) => state.demoBannerDismissed)
  const dismiss = useUiStore((state) => state.dismissDemoBanner)

  // Settings are admin-only in v0.2 — members don't see the demo-mode banner.
  if (!isAdmin || !data?.system?.demo_mode || dismissed) return null

  return (
    <div className={`demo-banner ${className ?? ''}`} role="status">
      <InfoCircleOutlined className="demo-banner__icon" />
      <span className="demo-banner__text">
        演示模式：AI 结果为内置示例数据，可在
        <Link to="/settings" className="demo-banner__link">
          设置
        </Link>
        中配置阿里云百炼等模型
      </span>
      <button type="button" className="demo-banner__close" aria-label="关闭演示模式提示" onClick={dismiss}>
        <CloseOutlined />
      </button>
    </div>
  )
}
