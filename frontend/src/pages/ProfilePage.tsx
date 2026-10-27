import { useEffect, useState } from 'react'
import { Alert, Avatar, Button, Form, Input, Tag } from 'antd'
import {
  CalendarOutlined,
  ClockCircleOutlined,
  IdcardOutlined,
  LockOutlined,
  LogoutOutlined,
  MailOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../api/client'
import { useChangePassword, useLogout, useUpdateProfile } from '../api/queries'
import { useAuthStore } from '../store/auth'
import { ROLE_META, roleLabel } from '../utils/roles'
import { colorFromString, formatDate, formatRelative, initials } from '../utils/format'
import { toast, toastError } from '../utils/toast'

interface ProfileFormValues {
  display_name?: string
  email?: string
}

interface PasswordFormValues {
  old_password: string
  new_password: string
  confirm: string
}

/** Self-service profile page (`/profile`) for every authenticated user. */
export default function ProfilePage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const user = useAuthStore((state) => state.user)
  const setUser = useAuthStore((state) => state.setUser)
  const resetAuth = useAuthStore((state) => state.reset)

  const updateProfile = useUpdateProfile()
  const changePassword = useChangePassword()
  const logout = useLogout()

  const [profileForm] = Form.useForm<ProfileFormValues>()
  const [passwordForm] = Form.useForm<PasswordFormValues>()
  const [profileError, setProfileError] = useState<string | null>(null)

  useEffect(() => {
    document.title = '个人资料 · Echoic'
  }, [])

  useEffect(() => {
    if (!user) return
    profileForm.setFieldsValue({
      display_name: user.display_name,
      email: user.email ?? '',
    })
  }, [profileForm, user])

  if (!user) return null

  const displayName = user.display_name || user.username
  const roleMeta = ROLE_META[user.role] ?? ROLE_META.member

  const handleSaveProfile = (values: ProfileFormValues) => {
    setProfileError(null)
    updateProfile.mutate(
      {
        display_name: (values.display_name ?? '').trim(),
        email: (values.email ?? '').trim(),
      },
      {
        onSuccess: (data) => {
          setUser(data.user)
          toast.success('个人资料已保存')
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 422) {
            setProfileError('资料格式不正确：显示名称最多 120 字，邮箱需为有效地址。')
          } else {
            setProfileError(error.message || '保存失败，请稍后重试。')
          }
        },
      },
    )
  }

  const handleChangePassword = (values: PasswordFormValues) => {
    changePassword.mutate(
      { old_password: values.old_password, new_password: values.new_password },
      {
        onSuccess: () => {
          toast.success('密码已修改，请使用新密码登录其他设备')
          passwordForm.resetFields()
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 400) {
            toastError(error, '旧密码不正确，或新密码不符合要求')
          } else {
            toastError(error, '密码修改失败')
          }
        },
      },
    )
  }

  const handleLogout = () => {
    logout.mutate(undefined, {
      onSettled: () => {
        resetAuth()
        queryClient.clear()
        toast.success('已退出登录')
        navigate('/login', { replace: true })
      },
    })
  }

  return (
    <div className="settings-page">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">个人资料</h1>
          <p className="page-header__subtitle">管理你的账号信息与登录密码；角色与权限由管理员分配</p>
        </div>
        <Tag color={roleMeta.color} icon={user.role === 'admin' ? <UserOutlined /> : <IdcardOutlined />}>
          {roleLabel(user.role)}
        </Tag>
      </header>

      <div className="settings-tab">
        <section className="settings-card">
          <header className="settings-card__head">
            <div className="settings-card__title">
              <span className="settings-card__icon settings-card__icon--account">
                <UserOutlined />
              </span>
              <div>
                <h3>账号信息</h3>
                <p>显示名称与邮箱会展示在录音归属、分享链接等位置</p>
              </div>
            </div>
          </header>

          <div className="settings-account">
            <Avatar
              size={56}
              style={{ background: colorFromString(displayName), fontSize: 22, fontWeight: 600, flex: '0 0 56px' }}
            >
              {initials(displayName, 1)}
            </Avatar>
            <div>
              <div className="settings-account__name">{displayName}</div>
              <div className="settings-account__meta">
                @{user.username}
                {user.email ? ` · ${user.email}` : ''}
              </div>
            </div>
          </div>

          <div className="settings-info-grid" style={{ marginTop: 18 }}>
            <div>
              <span className="settings-info-grid__label">用户名</span>
              <span className="settings-info-grid__value">@{user.username}</span>
            </div>
            <div>
              <span className="settings-info-grid__label">角色</span>
              <span className="settings-info-grid__value">
                <Tag color={roleMeta.color} style={{ marginInlineEnd: 0 }}>
                  {roleLabel(user.role)}
                </Tag>
              </span>
            </div>
            <div>
              <span className="settings-info-grid__label">
                <CalendarOutlined /> 注册时间
              </span>
              <span className="settings-info-grid__value">{user.created_at ? formatDate(user.created_at) : '—'}</span>
            </div>
            <div>
              <span className="settings-info-grid__label">
                <ClockCircleOutlined /> 最近登录
              </span>
              <span className="settings-info-grid__value">
                {user.last_login_at ? formatRelative(user.last_login_at) : '从未登录'}
              </span>
            </div>
            <div>
              <span className="settings-info-grid__label">账号状态</span>
              <span className="settings-info-grid__value">{user.disabled ? '已停用' : '正常'}</span>
            </div>
          </div>

          <div
            style={{
              height: 1,
              background: 'var(--border)',
              margin: '20px 0 16px',
            }}
          />
          <div className="settings-field__label" style={{ fontSize: 13.5, marginBottom: 10 }}>
            编辑资料
          </div>

          {profileError && (
            <Alert
              type="error"
              showIcon
              message={profileError}
              closable
              onClose={() => setProfileError(null)}
              style={{ marginBottom: 14, borderRadius: 8 }}
            />
          )}

          <Form<ProfileFormValues>
            form={profileForm}
            layout="vertical"
            requiredMark={false}
            onFinish={handleSaveProfile}
            disabled={updateProfile.isPending}
          >
            <div className="settings-form-grid">
              <Form.Item
                name="display_name"
                label="显示名称"
                rules={[
                  { required: true, message: '显示名称不能为空' },
                  { max: 120, message: '最多 120 个字符' },
                ]}
              >
                <Input prefix={<IdcardOutlined className="login-input-icon" />} placeholder="如 张三" />
              </Form.Item>
              <Form.Item name="email" label="邮箱（可选）" rules={[{ type: 'email', message: '邮箱格式不正确' }]}>
                <Input prefix={<MailOutlined className="login-input-icon" />} placeholder="name@example.com" />
              </Form.Item>
            </div>
            <div className="settings-card__actions">
              <Button type="primary" htmlType="submit" loading={updateProfile.isPending}>
                保存资料
              </Button>
            </div>
          </Form>
        </section>

        <section className="settings-card">
          <header className="settings-card__head">
            <div className="settings-card__title">
              <span className="settings-card__icon settings-card__icon--password">
                <LockOutlined />
              </span>
              <div>
                <h3>修改密码</h3>
                <p>修改后当前会话保持登录，其他设备需使用新密码重新登录</p>
              </div>
            </div>
          </header>
          <Form<PasswordFormValues> form={passwordForm} layout="vertical" onFinish={handleChangePassword} className="settings-password-form">
            <Form.Item
              name="old_password"
              label="当前密码"
              rules={[{ required: true, message: '请输入当前密码' }]}
            >
              <Input.Password prefix={<LockOutlined className="login-input-icon" />} placeholder="输入当前密码" autoComplete="current-password" />
            </Form.Item>
            <Form.Item
              name="new_password"
              label="新密码"
              rules={[
                { required: true, message: '请输入新密码' },
                { min: 6, message: '密码至少 6 位' },
              ]}
            >
              <Input.Password prefix={<LockOutlined className="login-input-icon" />} placeholder="至少 6 位字符" autoComplete="new-password" />
            </Form.Item>
            <Form.Item
              name="confirm"
              label="确认新密码"
              dependencies={['new_password']}
              rules={[
                { required: true, message: '请再次输入新密码' },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    if (!value || getFieldValue('new_password') === value) return Promise.resolve()
                    return Promise.reject(new Error('两次输入的密码不一致'))
                  },
                }),
              ]}
            >
              <Input.Password prefix={<LockOutlined className="login-input-icon" />} placeholder="再次输入新密码" autoComplete="new-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={changePassword.isPending}>
              修改密码
            </Button>
          </Form>
        </section>

        <section className="settings-card">
          <footer className="settings-card__actions settings-card__actions--between">
            <span className="muted-text">退出后需要重新登录才能访问录音库；当前账号 @{user.username}</span>
            <Button danger icon={<LogoutOutlined />} onClick={handleLogout} loading={logout.isPending}>
              退出登录
            </Button>
          </footer>
        </section>
      </div>
    </div>
  )
}
