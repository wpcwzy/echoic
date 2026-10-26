import { Button } from 'antd'
import { ReloadOutlined, WarningOutlined } from '@ant-design/icons'

interface ErrorStateProps {
  title?: string
  description?: string
  onRetry?: () => void
  retryLabel?: string
  compact?: boolean
}

export function ErrorState({
  title = '加载失败',
  description = '网络异常或服务暂时不可用，请稍后重试。',
  onRetry,
  retryLabel = '重新加载',
  compact = false,
}: ErrorStateProps) {
  return (
    <div className={`error-state ${compact ? 'error-state--compact' : ''}`} role="alert">
      <div className="error-state__icon">
        <WarningOutlined />
      </div>
      <div className="error-state__title">{title}</div>
      <div className="error-state__desc">{description}</div>
      {onRetry && (
        <Button icon={<ReloadOutlined />} onClick={onRetry}>
          {retryLabel}
        </Button>
      )}
    </div>
  )
}
