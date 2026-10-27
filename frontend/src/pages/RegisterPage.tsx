import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, Spin } from 'antd'
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  IdcardOutlined,
  LockOutlined,
  MailOutlined,
  RobotOutlined,
  SearchOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useLogin, useRegister, useSystemHealth } from '../api/queries'
import { useAuthStore } from '../store/auth'
import { BrandMark } from '../components/common/BrandMark'
import { toast } from '../utils/toast'

interface RegisterFormValues {
  username: string
  display_name?: string
  email?: string
  password: string
  confirm: string
}

function registerErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return '当前未开放自助注册，请联系管理员开通账号。'
    if (error.status === 409) return '该用户名已被占用，请换一个用户名再试。'
    if (error.status === 422) return '注册信息有误：用户名需 2–64 位字母/数字/下划线/点/横线，密码至少 8 位。'
    if (error.status === 429) return '操作过于频繁，请稍后再试。'
    return error.message || '注册失败，请稍后重试。'
  }
  return error instanceof Error && error.message ? error.message : '注册失败，请稍后重试。'
}

/** Public registration page (`/register`) — shown only when the server allows it. */
export default function RegisterPage() {
  const navigate = useNavigate()
  const register = useRegister()
  const login = useLogin()
  const setUser = useAuthStore((state) => state.setUser)
  const status = useAuthStore((state) => state.status)
  const [form] = Form.useForm<RegisterFormValues>()
  const [error, setError] = useState<string | null>(null)
  const { data: health, isLoading: healthLoading } = useSystemHealth()

  const registrationOpen = health?.allow_registration !== false

  useEffect(() => {
    document.title = '注册 · Echoic'
  }, [])

  if (status === 'authed') return <Navigate to="/" replace />

  const handleSubmit = (values: RegisterFormValues) => {
    setError(null)
    const payload = {
      username: values.username.trim(),
      password: values.password,
      display_name: values.display_name?.trim() || undefined,
      email: values.email?.trim() || undefined,
    }
    register.mutate(payload, {
      onSuccess: () => {
        // Registration does not set a cookie: sign in immediately afterwards.
        login.mutate(
          { username: payload.username, password: payload.password },
          {
            onSuccess: (data) => {
              setUser(data.user)
              toast.success(`账号创建成功，欢迎加入，${data.user.display_name || data.user.username}`)
              navigate('/', { replace: true })
            },
            onError: () => {
              toast.warning('账号已创建，请使用新账号登录')
              navigate('/login', { replace: true })
            },
          },
        )
      },
      onError: (mutationError) => setError(registerErrorMessage(mutationError)),
    })
  }

  return (
    <div className="login-page">
      <section className="login-brand">
        <div className="login-brand__glow login-brand__glow--a" />
        <div className="login-brand__glow login-brand__glow--b" />
        <div className="login-brand__inner">
          <div className="login-brand__logo-row">
            <BrandMark size={46} />
            <div>
              <div className="login-brand__name">Echoic</div>
              <div className="login-brand__slogan">私人会议录音知识库</div>
            </div>
          </div>

          <h1 className="login-brand__title">
            创建你的账号
            <br />
            开始积累会议知识
          </h1>
          <p className="login-brand__subtitle">
            注册后即可上传或录制会议，自动生成逐字稿、AI 纪要与观点树；每个成员只能访问属于自己的录音。
          </p>

          <div className="login-brand__features">
            <div className="login-feature">
              <span className="login-feature__icon">
                <RobotOutlined />
              </span>
              <div>
                <div className="login-feature__title">AI 纪要、观点树与关键洞察</div>
                <div className="login-feature__desc">一键提炼重点、决策与待办</div>
              </div>
            </div>
            <div className="login-feature">
              <span className="login-feature__icon">
                <SearchOutlined />
              </span>
              <div>
                <div className="login-feature__title">语义检索与可点击引用</div>
                <div className="login-feature__desc">用自然语言回顾任意讨论片段</div>
              </div>
            </div>
            <div className="login-feature">
              <span className="login-feature__icon">
                <IdcardOutlined />
              </span>
              <div>
                <div className="login-feature__title">成员空间与分享链接</div>
                <div className="login-feature__desc">录音归属清晰，可安全分享给外部伙伴</div>
              </div>
            </div>
          </div>

          <div className="login-brand__footer">
            <LockOutlined /> 数据仅保存在你自己的服务器上
          </div>
        </div>
      </section>

      <section className="login-panel">
        <div className="login-card">
          <div className="login-card__head">
            <h2>创建账号</h2>
            <p>注册成功后将自动登录并进入资料库</p>
          </div>

          {healthLoading && (
            <div style={{ textAlign: 'center', padding: '18px 0' }}>
              <Spin size="small" />
              <div className="muted-text" style={{ marginTop: 8 }}>正在检查注册开关…</div>
            </div>
          )}

          {!healthLoading && !registrationOpen ? (
            <>
              <Alert
                type="warning"
                showIcon
                message="注册暂未开放"
                description="管理员尚未开启自助注册。你可以联系管理员创建账号，或先使用已有账号登录。"
                className="login-card__alert"
              />
              <Link to="/login">
                <Button icon={<ArrowLeftOutlined />} block size="large">
                  返回登录
                </Button>
              </Link>
            </>
          ) : (
            <>
              {error && (
                <Alert
                  type="error"
                  showIcon
                  message={error}
                  className="login-card__alert"
                  closable
                  onClose={() => setError(null)}
                />
              )}

              <Form<RegisterFormValues>
                form={form}
                layout="vertical"
                requiredMark={false}
                size="large"
                disabled={register.isPending || login.isPending}
                onFinish={handleSubmit}
              >
                <Form.Item
                  name="username"
                  label="用户名"
                  rules={[
                    { required: true, message: '请输入用户名' },
                    { min: 2, max: 64, message: '用户名需 2–64 个字符' },
                    { pattern: /^[A-Za-z0-9_.-]+$/, message: '仅支持字母、数字、下划线、点和横线' },
                  ]}
                >
                  <Input
                    prefix={<UserOutlined className="login-input-icon" />}
                    placeholder="登录用户名，如 zhangsan"
                    autoComplete="username"
                    autoFocus
                  />
                </Form.Item>
                <Form.Item name="display_name" label="显示名称（可选）" rules={[{ max: 120, message: '最多 120 个字符' }]}>
                  <Input prefix={<IdcardOutlined className="login-input-icon" />} placeholder="展示给团队的名字，如 张三" />
                </Form.Item>
                <Form.Item name="email" label="邮箱（可选）" rules={[{ type: 'email', message: '邮箱格式不正确' }]}>
                  <Input prefix={<MailOutlined className="login-input-icon" />} placeholder="name@example.com" autoComplete="email" />
                </Form.Item>
                <Form.Item
                  name="password"
                  label="密码"
                  rules={[
                    { required: true, message: '请输入密码' },
                    { min: 8, message: '密码至少 8 位' },
                  ]}
                >
                  <Input.Password
                    prefix={<LockOutlined className="login-input-icon" />}
                    placeholder="至少 8 位字符"
                    autoComplete="new-password"
                  />
                </Form.Item>
                <Form.Item
                  name="confirm"
                  label="确认密码"
                  dependencies={['password']}
                  rules={[
                    { required: true, message: '请再次输入密码' },
                    ({ getFieldValue }) => ({
                      validator(_, value) {
                        if (!value || getFieldValue('password') === value) return Promise.resolve()
                        return Promise.reject(new Error('两次输入的密码不一致'))
                      },
                    }),
                  ]}
                >
                  <Input.Password
                    prefix={<LockOutlined className="login-input-icon" />}
                    placeholder="再次输入密码"
                    autoComplete="new-password"
                  />
                </Form.Item>
                <Button
                  type="primary"
                  htmlType="submit"
                  block
                  size="large"
                  className="login-submit"
                  loading={register.isPending || login.isPending}
                >
                  注册并登录 <ArrowRightOutlined />
                </Button>
              </Form>

              <div className="login-card__foot" style={{ justifyContent: 'center', gap: 6 }}>
                已有账号？
                <Link to="/login" style={{ color: 'var(--brand)', fontWeight: 500 }}>
                  返回登录
                </Link>
              </div>
            </>
          )}
        </div>

        <div className="login-copyright">Echoic v{health?.version ?? '0.3.0'} · 私人会议录音知识库</div>
      </section>
    </div>
  )
}
