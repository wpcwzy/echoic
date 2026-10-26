import type { ReactNode } from 'react'

interface EmptyStateProps {
  icon?: ReactNode
  title: string
  description?: ReactNode
  actions?: ReactNode
  variant?: 'default' | 'dashed' | 'plain'
  compact?: boolean
}

export function EmptyState({ icon, title, description, actions, variant = 'dashed', compact = false }: EmptyStateProps) {
  return (
    <div className={`empty-state empty-state--${variant} ${compact ? 'empty-state--compact' : ''}`}>
      {icon && <div className="empty-state__icon">{icon}</div>}
      <div className="empty-state__title">{title}</div>
      {description && <div className="empty-state__desc">{description}</div>}
      {actions && <div className="empty-state__actions">{actions}</div>}
    </div>
  )
}
