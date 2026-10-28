import { useEffect, useMemo, useState } from 'react'
import { AutoComplete, Button, Input, Table, Tag, Tooltip } from 'antd'
import type { TableProps } from 'antd'
import {
  AuditOutlined,
  ReloadOutlined,
  SearchOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useAuditLogs } from '../api/audit'
import { useDebouncedValue } from '../hooks/useDebounce'
import type { AuditLog } from '../types'
import { formatDate, formatRelative } from '../utils/format'
import { EmptyState } from '../components/common/EmptyState'
import { ErrorState } from '../components/common/ErrorState'

const PAGE_SIZE = 20

// Backend action names follow `<domain>.<verb>` (see app/services/audit.py).
const ACTION_LABELS: Record<string, string> = {
  'auth.login': '登录',
  'auth.login_failed': '登录失败',
  'auth.logout': '退出登录',
  'auth.register': '注册账号',
  'auth.change_password': '修改密码',
  'auth.profile_update': '更新个人资料',
  'user.create': '创建用户',
  'user.update': '更新用户',
  'user.delete': '删除用户',
  'user.reset_password': '重置密码',
  'recording.upload': '上传录音',
  'recording.update': '更新录音',
  'recording.delete': '删除录音',
  'recording.trash': '移入回收站',
  'recording.restore': '恢复录音',
  'recording.purge': '彻底删除录音',
  'recording.reprocess': '重新处理录音',
  'recording.cancel': '取消处理',
  'recording.speaker_rename': '修改说话人',
  'recording.segment_update': '编辑转录',
  'recording.owner_transfer': '转移归属',
  'share.create': '创建分享',
  'share.revoke': '撤销分享',
  'tag.create': '创建标签',
  'tag.update': '更新标签',
  'tag.delete': '删除标签',
  'settings.update': '修改设置',
  'settings.test_provider': '测试 AI 服务',
  'settings.prompts_reset': '恢复默认提示词',
  'system.seed_demo': '载入演示数据',
  'system.consume_scan': '扫描导入目录',
  'vector.reindex': '重建向量索引',
  'live.create': '创建实时会话',
  'live.stop': '结束实时会话',
  'live.cancel': '取消实时会话',
  'chat.ask': 'AI 问答',
  // older/normalised aliases (kept for pre-v1 rows & external tools)
  login: '登录',
  logout: '退出登录',
  register: '注册账号',
  create_user: '创建用户',
  update_user: '更新用户',
  delete_user: '删除用户',
  reset_password: '重置密码',
  change_password: '修改密码',
  upload_recording: '上传录音',
  update_recording: '更新录音',
  delete_recording: '删除录音',
  trash_recording: '移入回收站',
  restore_recording: '恢复录音',
  purge_recording: '彻底删除录音',
  reprocess_recording: '重新处理录音',
  create_share: '创建分享',
  revoke_share: '撤销分享',
  create_tag: '创建标签',
  update_tag: '更新标签',
  delete_tag: '删除标签',
  update_settings: '修改设置',
  seed_demo: '载入演示数据',
  consume_scan: '扫描导入目录',
  vector_reindex: '重建向量索引',
}

const TARGET_LABELS: Record<string, string> = {
  recording: '录音',
  user: '用户',
  share: '分享',
  settings: '设置',
  system: '系统',
  tag: '标签',
  live_session: '实时会话',
  vector: '向量索引',
}

function actionLabel(action: string): string {
  if (!action) return '—'
  if (ACTION_LABELS[action]) return ACTION_LABELS[action]
  // Gracefully handle `<domain>.<verb>` actions that aren't mapped yet.
  const verb = action.includes('.') ? action.split('.').slice(1).join('.') : action
  return ACTION_LABELS[verb] ?? action
}

function targetLabel(record: AuditLog): string {
  if (!record.target_type) return '—'
  const label = TARGET_LABELS[record.target_type] ?? record.target_type
  return record.target_id !== null && record.target_id !== undefined ? `${label} #${record.target_id}` : label
}

type Columns = NonNullable<TableProps<AuditLog>['columns']>

export default function AuditPage() {
  const [searchInput, setSearchInput] = useState('')
  const [action, setAction] = useState('')
  const [targetType, setTargetType] = useState('')
  const [userId, setUserId] = useState('')
  const [page, setPage] = useState(1)

  const search = useDebouncedValue(searchInput, 300)

  const params = useMemo(
    () => ({
      action: action.trim() || undefined,
      target_type: targetType.trim() || undefined,
      user_id: userId ? Number(userId) : undefined,
      q: search.trim() || undefined,
      page,
      page_size: PAGE_SIZE,
    }),
    [action, page, search, targetType, userId],
  )

  const auditQuery = useAuditLogs(params)
  const items = auditQuery.data?.items ?? []
  const total = auditQuery.data?.total ?? 0

  useEffect(() => {
    document.title = '审计日志 · Echoic'
  }, [])

  useEffect(() => {
    setPage(1)
  }, [action, search, targetType, userId])

  // Offer the action/target codes already present on the current page as
  // suggestions, while still allowing free-form inputs.
  const actionOptions = useMemo(
    () =>
      Array.from(new Set(items.map((item) => item.action).filter(Boolean)))
        .slice(0, 20)
        .map((value) => ({ value, label: `${actionLabel(value)}（${value}）` })),
    [items],
  )

  const targetOptions = useMemo(
    () =>
      Array.from(new Set(items.map((item) => item.target_type).filter(Boolean)))
        .slice(0, 20)
        .map((value) => ({ value, label: `${TARGET_LABELS[value] ?? value}（${value}）` })),
    [items],
  )

  const hasFilter = Boolean(action || targetType || userId || search)

  const resetFilters = () => {
    setSearchInput('')
    setAction('')
    setTargetType('')
    setUserId('')
    setPage(1)
  }

  const columns: Columns = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (value: string | null) =>
        value ? (
          <Tooltip title={formatRelative(value)}>
            <span className="audit-time">{formatDate(value)}</span>
          </Tooltip>
        ) : (
          <span className="audit-muted">—</span>
        ),
    },
    {
      title: '用户',
      dataIndex: 'username',
      key: 'user',
      width: 170,
      render: (_value, record) => (
        <span className="audit-user">
          <UserOutlined />
          <span>{record.username || '系统'}</span>
          {record.user_id !== null && record.user_id !== undefined && (
            <span className="audit-muted">#{record.user_id}</span>
          )}
        </span>
      ),
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      width: 210,
      render: (value: string) => (
        <span className="audit-action">
          <span className="audit-action__label">{actionLabel(value)}</span>
          <code className="audit-action__code">{value}</code>
        </span>
      ),
    },
    {
      title: '目标',
      dataIndex: 'target_type',
      key: 'target',
      width: 160,
      render: (_value, record) => <span className="audit-target">{targetLabel(record)}</span>,
    },
    {
      title: 'IP',
      dataIndex: 'ip',
      key: 'ip',
      width: 140,
      render: (value: string) => (value ? <code className="audit-ip">{value}</code> : <span className="audit-muted">—</span>),
    },
    {
      title: '详情',
      key: 'detail',
      width: 90,
      align: 'right',
      render: (_value, record) => {
        const keys = record.detail && typeof record.detail === 'object' ? Object.keys(record.detail).length : 0
        return keys > 0 ? (
          <span className="audit-detail-count">{keys} 项</span>
        ) : (
          <span className="audit-muted">—</span>
        )
      },
    },
  ]

  return (
    <div className="audit-page">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">审计日志</h1>
          <p className="page-header__subtitle">
            登录、录音、分享、设置等关键操作的审计记录；仅管理员可见，按时间倒序排列。
          </p>
        </div>
        <Tag icon={<AuditOutlined />} color="blue">
          管理员
        </Tag>
      </header>

      <section className="users-card audit-card">
        <div className="audit-toolbar">
          <Input
            className="audit-search"
            allowClear
            prefix={<SearchOutlined className="muted-icon" />}
            placeholder="搜索操作、用户、详情等关键词"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
          />
          <AutoComplete
            className="audit-filter"
            allowClear
            options={actionOptions}
            placeholder="操作代码，如 login"
            value={action}
            onChange={(value) => setAction(value ?? '')}
          />
          <AutoComplete
            className="audit-filter"
            allowClear
            options={targetOptions}
            placeholder="目标类型，如 recording"
            value={targetType}
            onChange={(value) => setTargetType(value ?? '')}
          />
          <Input
            className="audit-filter audit-filter--user"
            allowClear
            value={userId}
            placeholder="用户 ID"
            inputMode="numeric"
            onChange={(event) => setUserId(event.target.value.replace(/\D/g, ''))}
          />
          {hasFilter && (
            <Button onClick={resetFilters}>重置筛选</Button>
          )}
          <div className="audit-toolbar__spacer" />
          <span className="audit-toolbar__total">共 {total} 条</span>
          <Tooltip title="刷新">
            <Button
              icon={<ReloadOutlined />}
              aria-label="刷新审计日志"
              loading={auditQuery.isFetching}
              onClick={() => void auditQuery.refetch()}
            />
          </Tooltip>
        </div>

        {auditQuery.isError ? (
          <ErrorState
            title="审计日志加载失败"
            description={auditQuery.error?.message || '请确认管理员权限并稍后重试。'}
            onRetry={() => void auditQuery.refetch()}
          />
        ) : (
          <Table<AuditLog>
            className="users-table audit-table"
            rowKey="id"
            columns={columns}
            dataSource={items}
            loading={auditQuery.isLoading}
            scroll={{ x: 940 }}
            expandable={{
              expandedRowRender: (record) => (
                <div className="audit-detail">
                  {record.detail && Object.keys(record.detail).length > 0 ? (
                    <pre className="audit-json">{JSON.stringify(record.detail, null, 2)}</pre>
                  ) : (
                    <span className="audit-muted">无详情</span>
                  )}
                </div>
              ),
              rowExpandable: (record) => Boolean(record.detail && Object.keys(record.detail).length > 0),
            }}
            locale={{
              emptyText: (
                <EmptyState
                  variant="plain"
                  compact
                  icon={<AuditOutlined />}
                  title={hasFilter ? '没有匹配的审计记录' : '还没有审计记录'}
                  description={hasFilter ? '试试调整筛选条件或清空关键词。' : '登录、上传录音、修改设置等操作会记录在这里。'}
                  actions={hasFilter ? <Button onClick={resetFilters}>重置筛选</Button> : undefined}
                />
              ),
            }}
            pagination={{
              current: page,
              pageSize: PAGE_SIZE,
              total,
              showSizeChanger: false,
              showTotal: (count) => `共 ${count} 条`,
              onChange: (nextPage) => setPage(nextPage),
            }}
          />
        )}
      </section>
    </div>
  )
}
