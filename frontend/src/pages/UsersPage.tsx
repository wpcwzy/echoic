import { useEffect, useMemo, useState } from 'react'
import { Avatar, Button, Dropdown, Input, Select, Switch, Table, Tag, Tooltip } from 'antd'
import type { MenuProps, TableProps } from 'antd'
import {
  CheckCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  KeyOutlined,
  MoreOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  StopOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'
import {
  useCreateUser,
  useDeleteUser,
  useResetUserPassword,
  useUpdateUser,
  useUsers,
} from '../api/users'
import { useAuthStore } from '../store/auth'
import { useDebouncedValue } from '../hooks/useDebounce'
import { colorFromString, formatDate, formatDateShort, formatRelative, initials } from '../utils/format'
import { ROLE_META } from '../utils/roles'
import { confirmAction, toast, toastError } from '../utils/toast'
import type { UserAdmin, UserCreatePayload, UserUpdatePayload } from '../types'
import { CreateUserModal, EditUserModal, ResetPasswordModal } from '../components/users/UserDialogs'
import { EmptyState } from '../components/common/EmptyState'
import { ErrorState } from '../components/common/ErrorState'

type Columns = NonNullable<TableProps<UserAdmin>['columns']>

const ROLE_FILTER_OPTIONS = [
  { label: '全部角色', value: 'all' },
  { label: '管理员', value: 'admin' },
  { label: '成员', value: 'member' },
]

export default function UsersPage() {
  const currentUser = useAuthStore((state) => state.user)
  const [searchInput, setSearchInput] = useState('')
  const [roleFilter, setRoleFilter] = useState<'all' | 'admin' | 'member'>('all')
  const [includeDisabled, setIncludeDisabled] = useState(true)
  const [page, setPage] = useState(1)
  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<UserAdmin | null>(null)
  const [resetting, setResetting] = useState<UserAdmin | null>(null)

  const search = useDebouncedValue(searchInput, 300)
  const params = useMemo(
    () => ({
      query: search.trim() || undefined,
      role: roleFilter === 'all' ? undefined : roleFilter,
      include_disabled: includeDisabled,
      page,
      page_size: 20,
    }),
    [includeDisabled, page, roleFilter, search],
  )

  const usersQuery = useUsers(params)
  const createUser = useCreateUser()
  const updateUser = useUpdateUser()
  const resetPassword = useResetUserPassword()
  const deleteUser = useDeleteUser()

  useEffect(() => {
    document.title = '用户管理 · Echoic'
  }, [])

  useEffect(() => {
    setPage(1)
  }, [search, roleFilter, includeDisabled])

  const items = usersQuery.data?.items ?? []
  const total = usersQuery.data?.total ?? 0

  const handleCreate = (payload: UserCreatePayload) => {
    createUser.mutate(payload, {
      onSuccess: (user) => {
        toast.success(`用户 ${user.display_name || user.username} 已创建`)
        setCreateOpen(false)
      },
      onError: (error) => toastError(error, '创建用户失败'),
    })
  }

  const handleUpdate = (payload: UserUpdatePayload) => {
    if (!editing) return
    updateUser.mutate(
      { id: editing.id, payload },
      {
        onSuccess: (user) => {
          toast.success(`用户 ${user.display_name || user.username} 已更新`)
          setEditing(null)
        },
        onError: (error) => toastError(error, '更新用户失败'),
      },
    )
  }

  const handleResetPassword = (newPassword: string) => {
    if (!resetting) return
    resetPassword.mutate(
      { id: resetting.id, new_password: newPassword },
      {
        onSuccess: () => {
          toast.success(`已重置 ${resetting.display_name || resetting.username} 的密码`)
          setResetting(null)
        },
        onError: (error) => toastError(error, '重置密码失败'),
      },
    )
  }

  const toggleDisabled = (record: UserAdmin) => {
    const nextDisabled = !record.disabled
    confirmAction({
      title: nextDisabled ? `停用账号「${record.display_name || record.username}」？` : `启用账号「${record.display_name || record.username}」？`,
      content: nextDisabled
        ? '停用后该用户将无法登录，现有会话也会立即失效。其录音与分享链接不会被删除。'
        : '启用后该用户可以重新登录并访问自己的录音。',
      okText: nextDisabled ? '停用账号' : '启用账号',
      danger: nextDisabled,
      onOk: async () => {
        try {
          await updateUser.mutateAsync({ id: record.id, payload: { disabled: nextDisabled } })
          toast.success(nextDisabled ? '账号已停用' : '账号已启用')
        } catch (error) {
          toastError(error, '操作失败')
          throw error
        }
      },
    })
  }

  const removeUser = (record: UserAdmin) => {
    confirmAction({
      title: `删除用户「${record.display_name || record.username}」？`,
      content:
        record.recording_count > 0
          ? `该用户拥有 ${record.recording_count} 条录音，需先转移或删除这些录音后才能删除账号。此操作无法撤销。`
          : '账号删除后无法恢复，该用户创建的分享链接会保留但不再显示创建者。',
      danger: true,
      okText: '删除用户',
      onOk: async () => {
        try {
          await deleteUser.mutateAsync(record.id)
          toast.success('用户已删除')
        } catch (error) {
          toastError(error, '删除失败')
          throw error
        }
      },
    })
  }

  const actionMenu = (record: UserAdmin): MenuProps['items'] => [
    { key: 'edit', icon: <EditOutlined />, label: '编辑资料与角色' },
    { key: 'reset', icon: <KeyOutlined />, label: '重置密码' },
    {
      key: 'toggle',
      icon: record.disabled ? <CheckCircleOutlined /> : <StopOutlined />,
      label: record.disabled ? '启用账号' : '停用账号',
    },
    { type: 'divider' },
    {
      key: 'delete',
      icon: <DeleteOutlined />,
      label: '删除用户',
      danger: true,
      disabled: record.id === currentUser?.id,
    },
  ]

  const onActionClick = (record: UserAdmin, key: string) => {
    if (key === 'edit') setEditing(record)
    else if (key === 'reset') setResetting(record)
    else if (key === 'toggle') toggleDisabled(record)
    else if (key === 'delete') removeUser(record)
  }

  const columns: Columns = [
    {
      title: '用户',
      dataIndex: 'username',
      key: 'user',
      width: 260,
      render: (_value, record) => {
        const name = record.display_name || record.username
        return (
          <div className="user-cell">
            <Avatar size={36} style={{ background: colorFromString(name), fontSize: 13, flex: '0 0 36px' }}>
              {initials(name, 1)}
            </Avatar>
            <div className="user-cell__body">
              <div className="user-cell__name">
                {name}
                {record.id === currentUser?.id && <span className="user-cell__self">你</span>}
              </div>
              <div className="user-cell__meta">
                @{record.username}
                {record.email ? ` · ${record.email}` : ''}
              </div>
            </div>
          </div>
        )
      },
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: 100,
      render: (_value, record) => (
        <Tag color={ROLE_META[record.role]?.color ?? 'default'} icon={record.role === 'admin' ? <UserOutlined /> : <TeamOutlined />}>
          {ROLE_META[record.role]?.label ?? record.role}
        </Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'disabled',
      key: 'status',
      width: 96,
      render: (_value, record) =>
        record.disabled ? (
          <span className="user-status user-status--disabled">
            <span className="user-status__dot" /> 已停用
          </span>
        ) : (
          <span className="user-status user-status--active">
            <span className="user-status__dot" /> 正常
          </span>
        ),
    },
    {
      title: '录音',
      dataIndex: 'recording_count',
      key: 'recording_count',
      width: 78,
      align: 'right',
      sorter: (a, b) => a.recording_count - b.recording_count,
      render: (value: number) => <span className="user-count">{value}</span>,
    },
    {
      title: '分享',
      dataIndex: 'share_count',
      key: 'share_count',
      width: 78,
      align: 'right',
      render: (value: number) => <span className="user-count">{value}</span>,
    },
    {
      title: '最近登录',
      dataIndex: 'last_login_at',
      key: 'last_login_at',
      width: 140,
      render: (value: string | null) =>
        value ? (
          <Tooltip title={formatDate(value)}>
            <span className="user-muted">{formatRelative(value)}</span>
          </Tooltip>
        ) : (
          <span className="user-muted user-muted--faint">从未登录</span>
        ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (value: string | null) => <span className="user-muted">{value ? formatDateShort(value) : '—'}</span>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 70,
      fixed: 'right',
      align: 'center',
      render: (_value, record) => (
        <Dropdown
          menu={{ items: actionMenu(record), onClick: ({ key }) => onActionClick(record, key) }}
          trigger={['click']}
          placement="bottomRight"
        >
          <Button type="text" size="small" icon={<MoreOutlined />} aria-label={`管理用户 ${record.username}`} />
        </Dropdown>
      ),
    },
  ]

  return (
    <div className="users-page">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">用户管理</h1>
          <p className="page-header__subtitle">
            管理账号、角色与启用状态。成员只能访问自己的录音；管理员可访问全部内容与系统设置。
          </p>
        </div>
        <div className="users-page__actions">
          <Tooltip title="刷新列表">
            <Button
              icon={<ReloadOutlined />}
              aria-label="刷新列表"
              loading={usersQuery.isFetching}
              onClick={() => void usersQuery.refetch()}
            >
              刷新
            </Button>
          </Tooltip>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            新建用户
          </Button>
        </div>
      </header>

      <section className="users-card">
        <div className="users-toolbar">
          <Input
            className="users-search"
            allowClear
            prefix={<SearchOutlined className="muted-icon" />}
            placeholder="搜索用户名、显示名称或邮箱"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
          />
          <Select
            className="users-role-filter"
            value={roleFilter}
            options={ROLE_FILTER_OPTIONS}
            onChange={(value) => setRoleFilter(value as 'all' | 'admin' | 'member')}
          />
          <label className="users-toolbar__switch">
            <Switch size="small" checked={includeDisabled} onChange={setIncludeDisabled} />
            <span>显示已停用</span>
          </label>
          <div className="users-toolbar__spacer" />
          <span className="users-toolbar__total">共 {total} 位用户</span>
        </div>

        {usersQuery.isError ? (
          <ErrorState
            title="用户列表加载失败"
            description={usersQuery.error?.message || '请确认管理员权限并稍后重试。'}
            onRetry={() => void usersQuery.refetch()}
          />
        ) : (
          <Table<UserAdmin>
            className="users-table"
            rowKey="id"
            columns={columns}
            dataSource={items}
            loading={usersQuery.isLoading}
            rowClassName={(record) => (record.disabled ? 'users-table__row--disabled' : '')}
            scroll={{ x: 920 }}
            locale={{
              emptyText: (
                <EmptyState
                  variant="plain"
                  compact
                  icon={<TeamOutlined />}
                  title={search || roleFilter !== 'all' ? '没有匹配的用户' : '还没有其他用户'}
                  description={
                    search || roleFilter !== 'all'
                      ? '试试调整搜索关键词或角色筛选。'
                      : '点击右上角「新建用户」为团队成员创建账号。'
                  }
                />
              ),
            }}
            pagination={{
              current: page,
              pageSize: 20,
              total,
              showSizeChanger: false,
              showTotal: (count) => `共 ${count} 位用户`,
              onChange: (nextPage) => setPage(nextPage),
            }}
          />
        )}
      </section>

      <CreateUserModal
        open={createOpen}
        loading={createUser.isPending}
        onCancel={() => setCreateOpen(false)}
        onSubmit={handleCreate}
      />
      <EditUserModal
        user={editing}
        loading={updateUser.isPending}
        onCancel={() => setEditing(null)}
        onSubmit={handleUpdate}
      />
      <ResetPasswordModal
        user={resetting}
        loading={resetPassword.isPending}
        onCancel={() => setResetting(null)}
        onSubmit={handleResetPassword}
      />
    </div>
  )
}
