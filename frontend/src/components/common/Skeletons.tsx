import { Skeleton } from 'antd'

export function StatsCardsSkeleton({ count = 4 }: { count?: number }) {
  return (
    <div className="stats-grid">
      {Array.from({ length: count }).map((_, index) => (
        <div className="stat-card stat-card--skeleton" key={index}>
          <Skeleton active title={{ width: '40%' }} paragraph={{ rows: 1, width: ['70%'] }} />
        </div>
      ))}
    </div>
  )
}

export function RecordingGridSkeleton({ count = 6 }: { count?: number }) {
  return (
    <div className="recording-grid">
      {Array.from({ length: count }).map((_, index) => (
        <div className="recording-card recording-card--skeleton" key={index}>
          <div className="recording-card__head">
            <Skeleton.Button active size="small" style={{ width: 64, height: 22 }} />
            <Skeleton.Button active size="small" style={{ width: 42, height: 22 }} />
          </div>
          <Skeleton active title={{ width: '85%' }} paragraph={{ rows: 2, width: ['100%', '62%'] }} />
          <div className="recording-card__footer">
            <Skeleton.Avatar active size={24} />
            <Skeleton.Avatar active size={24} style={{ marginLeft: -12 }} />
            <Skeleton.Button active size="small" style={{ width: 80, height: 18, marginLeft: 'auto' }} />
          </div>
        </div>
      ))}
    </div>
  )
}

export function RecordingListSkeleton({ count = 5 }: { count?: number }) {
  return (
    <div className="recording-list">
      {Array.from({ length: count }).map((_, index) => (
        <div className="recording-row recording-row--skeleton" key={index}>
          <Skeleton.Avatar active shape="square" size={40} />
          <Skeleton active title={{ width: '38%' }} paragraph={{ rows: 1, width: ['55%'] }} />
        </div>
      ))}
    </div>
  )
}

export function DetailPlayerSkeleton() {
  return (
    <div className="player-card player-card--skeleton">
      <Skeleton active title={{ width: '12%' }} paragraph={{ rows: 1, width: ['100%'] }} />
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginTop: 8 }}>
        <Skeleton.Avatar active size={28} />
        <Skeleton.Avatar active size={28} style={{ marginLeft: -16 }} />
        <Skeleton.Button active shape="round" style={{ width: 220, height: 40, margin: '0 auto' }} />
        <Skeleton.Button active size="small" style={{ width: 52 }} />
      </div>
    </div>
  )
}

export function TabContentSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="tab-skeleton">
      <Skeleton active title={{ width: '30%' }} paragraph={{ rows }} />
      <Skeleton active title={{ width: '24%' }} paragraph={{ rows: 2 }} style={{ marginTop: 32 }} />
      <Skeleton active title={{ width: '34%' }} paragraph={{ rows: 3 }} style={{ marginTop: 32 }} />
    </div>
  )
}

export function SummarySkeleton() {
  return (
    <div className="summary-card summary-card--skeleton">
      <Skeleton active title={{ width: '18%' }} paragraph={{ rows: 4, width: ['100%', '96%', '88%', '92%'] }} />
      <Skeleton active title={{ width: '22%' }} paragraph={{ rows: 3 }} style={{ marginTop: 28 }} />
    </div>
  )
}
