import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input } from 'antd'
import {
  ArrowRightOutlined,
  AudioOutlined,
  LockOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useLogin, useSystemHealth } from '../api/queries'
import { useAuthStore } from '../store/auth'
import { BrandMark } from '../components/common/BrandMark'
import { toast } from '../utils/toast'

interface LoginFormValues {
  username: string
  password: string
}

interface LocationState {
  from?: string
}

const FEATURES = [
  {
    icon: <AudioOutlined />,
    title: '自动转写与说话人分离',
    desc: '上传录音或视频，自动生成带时间戳的逐字稿',
  },
  {
    icon: <RobotOutlined />,
    title: 'AI 纪要与观点树',
    desc: '一键提炼会议纪要、关键决策、待办与章节',
  },
  {
    icon: <SearchOutlined />,
    title: '自然语言问答',
    desc: '向录音提问，回答附带可点击的原文引用',
  },
]

export default function LoginPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const login = useLogin()
  const setUser = useAuthStore((state) => state.setUser)
  const status = useAuthStore((state) => state.status)
  const [form] = Form.useForm<LoginFormValues>()
  const [error, setError] = useState<string | null>(null)
  const { data: health } = useSystemHealth()

  const state = location.state as LocationState | null
  const from = state?.from && !state.from.startsWith('/login') ? state.from : '/'

  useEffect(() => {
    document.title = '登录 · Echoic'
  }, [])

  if (status === 'authed') return <Navigate to={from} replace />

  const handleSubmit = (values: LoginFormValues) => {
    setError(null)
    login.mutate(values, {
      onSuccess: (data) => {
        setUser(data.user)
        toast.success(`欢迎回来，${data.user.display_name || data.user.username}`)
        navigate(from, { replace: true })
      },
      onError: (mutationError) => {
        if (mutationError instanceof ApiError) {
          if (mutationError.status === 429) {
            setError('登录尝试过于频繁，请稍等一分钟后再试。')
            return
          }
          if (mutationError.status === 401) {
            setError('用户名或密码错误，请检查后重试。')
            return
          }
        }
        setError(mutationError.message || '登录失败，请稍后重试。')
      },
    })
  }

  const fillDemoAccount = () => {
    form.setFieldsValue({ username: 'admin', password: 'admin123' })

  }

  const healthText = health
    ? health.demo_mode
      ? '演示模式运行中 · 内置示例数据'
      : '服务运行正常'
    : '正在连接服务…'

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
            把每一次会议
            <br />
            变成可检索的知识
          </h1>
          <p className="login-brand__subtitle">
            本地部署 · 隐私优先。录音自动转写、AI 提炼纪要，随时用自然语言回顾任何一次讨论。
          </p>

          <div className="login-brand__features">
            {FEATURES.map((feature) => (
              <div className="login-feature" key={feature.title}>
                <span className="login-feature__icon">{feature.icon}</span>
                <div>
                  <div className="login-feature__title">{feature.title}</div>
                  <div className="login-feature__desc">{feature.desc}</div>
                </div>
              </div>
            ))}
          </div>

          <div className="login-brand__visual" aria-hidden="true">
            <div className="login-visual-card login-visual-card--main">
              <div className="login-visual-card__avatar" />
              <div className="login-visual-card__lines">
                <span style={{ width: '72%' }} />
                <span style={{ width: '90%' }} />
                <span style={{ width: '58%' }} />
              </div>
              <div className="login-visual-card__wave">
                {[8, 14, 20, 12, 17, 9, 15, 22, 11, 16, 7, 13].map((height, index) => (
                  <i key={index} style={{ height }} />
                ))}
              </div>
            </div>
            <div className="login-visual-badge">
              <ThunderboltOutlined /> AI 纪要已生成
            </div>
          </div>

          <div className="login-brand__footer">
            <SafetyCertificateOutlined /> 数据仅保存在你自己的服务器上
          </div>
        </div>
      </section>

      <section className="login-panel">
        <div className="login-card">
          <div className="login-card__head">
            <h2>欢迎回来</h2>
            <p>登录以继续管理你的录音与会议知识</p>
          </div>

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

          <Form<LoginFormValues>
            form={form}
            layout="vertical"
            requiredMark={false}
            initialValues={{ username: '', password: '' }}
            onFinish={handleSubmit}
            size="large"
          >
            <Form.Item
              name="username"
              label="用户名"
              rules={[{ required: true, message: '请输入用户名' }]}
            >
              <Input
                prefix={<UserOutlined className="login-input-icon" />}
                placeholder="请输入用户名"
                autoComplete="username"
                autoFocus
              />
            </Form.Item>
            <Form.Item
              name="password"
              label="密码"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password
                prefix={<LockOutlined className="login-input-icon" />}
                placeholder="请输入密码"
                autoComplete="current-password"
                onPressEnter={() => form.submit()}
              />
            </Form.Item>
            <Button
              type="primary"
              htmlType="submit"
              block
              size="large"
              className="login-submit"
              loading={login.isPending}
            >
              登录 <ArrowRightOutlined />
            </Button>
          </Form>

          <div className="login-demo-hint">
            <div className="login-demo-hint__text">
              <span className="login-demo-hint__label">演示账号</span>
              <code>admin</code> / <code>admin123</code>
            </div>
            <Button type="link" size="small" onClick={fillDemoAccount}>
              一键填入
            </Button>
          </div>

          {health?.allow_registration && (
            <div style={{ marginTop: 14, textAlign: 'center', fontSize: 13, color: 'var(--text-2)' }}>
              还没有账号？
              <Link to="/register" style={{ color: 'var(--brand)', marginLeft: 6, fontWeight: 500 }}>
                立即注册
              </Link>
            </div>
          )}

          <div className="login-card__foot">
            <span className={`login-health-dot ${health ? 'is-ok' : ''}`} />
            {healthText}
          </div>
        </div>

        <div className="login-copyright">Echoic v{health?.version ?? '0.1.0'} · 私人会议录音知识库</div>
      </section>
    </div>
  )
}
