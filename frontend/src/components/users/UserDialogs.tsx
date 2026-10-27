import { useEffect } from 'react'
import { Form, Input, Modal, Select, Switch } from 'antd'
import type { UserAdmin, UserCreatePayload, UserRole, UserUpdatePayload } from '../../types'

interface CreateUserFormValues {
  username: string
  display_name?: string
  email?: string
  password: string
  confirm: string
  role: UserRole
}

const ROLE_OPTIONS: { label: string; value: UserRole }[] = [
  { label: '成员（仅自己的录音）', value: 'member' },
  { label: '管理员（全部权限）', value: 'admin' },
]

interface CreateUserModalProps {
  open: boolean
  loading: boolean
  onCancel: () => void
  onSubmit: (payload: UserCreatePayload) => void
}

/** Admin dialog: create a user account. */
export function CreateUserModal({ open, loading, onCancel, onSubmit }: CreateUserModalProps) {
  const [form] = Form.useForm<CreateUserFormValues>()

  useEffect(() => {
    if (open) form.resetFields()
  }, [form, open])

  const handleOk = async () => {
    try {
      const values = await form.validateFields()
      if (values.password !== values.confirm) {
        form.setFields([{ name: 'confirm', errors: ['两次输入的密码不一致'] }])
        return
      }
      onSubmit({
        username: values.username.trim(),
        password: values.password,
        display_name: values.display_name?.trim() || undefined,
        email: values.email?.trim() || undefined,
        role: values.role,
      })
    } catch {
      /* validation errors are rendered inline */
    }
  }

  return (
    <Modal
      open={open}
      title="新建用户"
      okText="创建用户"
      cancelText="取消"
      confirmLoading={loading}
      onOk={() => void handleOk()}
      onCancel={onCancel}
      width={560}
      destroyOnClose
    >
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        initialValues={{ role: 'member' }}
        className="user-form"
      >
        <div className="form-grid-2">
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              { min: 2, max: 64, message: '用户名需 2–64 个字符' },
              { pattern: /^[A-Za-z0-9_.-]+$/, message: '仅支持字母、数字、下划线、点和横线' },
            ]}
          >
            <Input placeholder="登录账号，如 zhangsan" autoComplete="off" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名称" rules={[{ max: 120, message: '最多 120 个字符' }]}>
            <Input placeholder="如 张三" />
          </Form.Item>
          <Form.Item name="email" label="邮箱（可选）" rules={[{ type: 'email', message: '邮箱格式不正确' }]}>
            <Input placeholder="name@example.com" autoComplete="off" />
          </Form.Item>
          <Form.Item name="role" label="角色">
            <Select options={ROLE_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            rules={[
              { required: true, message: '请输入初始密码' },
              { min: 8, message: '密码至少 8 位' },
            ]}
          >
            <Input.Password placeholder="至少 8 位字符" autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="confirm"
            label="确认密码"
            rules={[{ required: true, message: '请再次输入密码' }]}
            dependencies={['password']}
          >
            <Input.Password placeholder="再次输入密码" autoComplete="new-password" />
          </Form.Item>
        </div>
      </Form>
    </Modal>
  )
}

interface EditUserFormValues {
  display_name?: string
  email?: string
  role: UserRole
  enabled: boolean
}

interface EditUserModalProps {
  user: UserAdmin | null
  loading: boolean
  onCancel: () => void
  onSubmit: (payload: UserUpdatePayload) => void
}

/** Admin dialog: edit display name / email / role / enabled state. */
export function EditUserModal({ user, loading, onCancel, onSubmit }: EditUserModalProps) {
  const [form] = Form.useForm<EditUserFormValues>()

  useEffect(() => {
    if (!user) return
    form.setFieldsValue({
      display_name: user.display_name,
      email: user.email ?? '',
      role: user.role,
      enabled: !user.disabled,
    })
  }, [form, user])

  const handleOk = async () => {
    try {
      const values = await form.validateFields()
      onSubmit({
        display_name: values.display_name?.trim() ?? '',
        email: values.email?.trim() ?? '',
        role: values.role,
        disabled: !values.enabled,
      })
    } catch {
      /* validation errors are rendered inline */
    }
  }

  return (
    <Modal
      open={Boolean(user)}
      title={user ? `编辑用户 · ${user.display_name || user.username}` : '编辑用户'}
      okText="保存"
      cancelText="取消"
      confirmLoading={loading}
      onOk={() => void handleOk()}
      onCancel={onCancel}
      width={520}
      destroyOnClose
    >
      {user && (
        <Form form={form} layout="vertical" requiredMark={false} className="user-form">
          <div className="form-grid-2">
            <Form.Item name="display_name" label="显示名称" rules={[{ max: 120, message: '最多 120 个字符' }]}>
              <Input placeholder="显示名称" />
            </Form.Item>
            <Form.Item name="email" label="邮箱" rules={[{ type: 'email', message: '邮箱格式不正确' }]}>
              <Input placeholder="name@example.com" />
            </Form.Item>
            <Form.Item name="role" label="角色" extra="管理员可管理所有录音、用户与系统设置">
              <Select options={ROLE_OPTIONS} />
            </Form.Item>
            <div className="settings-field settings-field--switch user-form__switch">
              <span className="settings-field__label">启用账号（关闭后无法登录，现有会话立即失效）</span>
              <Form.Item name="enabled" valuePropName="checked" noStyle>
                <Switch checkedChildren="启用" unCheckedChildren="停用" />
              </Form.Item>
            </div>
          </div>
          <p className="user-form__hint">用户名 @{user.username} 不可修改。停用或降级最后一位管理员会被系统拒绝。</p>
        </Form>
      )}
    </Modal>
  )
}

interface ResetPasswordModalProps {
  user: UserAdmin | null
  loading: boolean
  onCancel: () => void
  onSubmit: (newPassword: string) => void
}

/** Admin dialog: reset another user's password. */
export function ResetPasswordModal({ user, loading, onCancel, onSubmit }: ResetPasswordModalProps) {
  const [form] = Form.useForm<{ new_password: string; confirm: string }>()

  useEffect(() => {
    if (user) form.resetFields()
  }, [form, user])

  const handleOk = async () => {
    try {
      const values = await form.validateFields()
      if (values.new_password !== values.confirm) {
        form.setFields([{ name: 'confirm', errors: ['两次输入的密码不一致'] }])
        return
      }
      onSubmit(values.new_password)
    } catch {
      /* validation errors are rendered inline */
    }
  }

  return (
    <Modal
      open={Boolean(user)}
      title={user ? `重置密码 · ${user.display_name || user.username}` : '重置密码'}
      okText="重置密码"
      cancelText="取消"
      confirmLoading={loading}
      onOk={() => void handleOk()}
      onCancel={onCancel}
      width={460}
      destroyOnClose
    >
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item
          name="new_password"
          label="新密码"
          rules={[
            { required: true, message: '请输入新密码' },
            { min: 8, message: '密码至少 8 位' },
          ]}
        >
          <Input.Password placeholder="至少 8 位字符" autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="确认新密码"
          dependencies={['new_password']}
          rules={[{ required: true, message: '请再次输入新密码' }]}
        >
          <Input.Password placeholder="再次输入新密码" autoComplete="new-password" />
        </Form.Item>
        <p className="user-form__hint">重置后请及时通知对方；对方已登录的会话不会自动失效（改密仅影响下次登录）。</p>
      </Form>
    </Modal>
  )
}
