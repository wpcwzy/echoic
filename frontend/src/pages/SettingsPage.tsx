import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Progress,
  Segmented,
  Select,
  Skeleton,
  Switch,
  Tabs,
  Tag,
  Tooltip,
} from 'antd'
import {
  AudioOutlined,
  CheckCircleFilled,
  CloudServerOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  ExperimentOutlined,
  FolderOpenOutlined,
  InfoCircleOutlined,
  LogoutOutlined,
  ReloadOutlined,
  BgColorsOutlined,
  DesktopOutlined,
  MoonOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  ScanOutlined,
  SunOutlined,
  PartitionOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  UserOutlined,
  WarningFilled,
} from '@ant-design/icons'
import { useQueryClient } from '@tanstack/react-query'
import {
  useChangePassword,
  useLogout,
  useResetPrompts,
  useSaveSettings,
  useSeedDemo,
  useSettings,
  useSystemHealth,
  useSystemInfo,
  useTestProvider,
  useConsumeScan,
} from '../api/queries'
import { useVectorReindex, useVectorStatus } from '../api/vector'
import { useAuthStore } from '../store/auth'
import { useThemeStore, type ThemeMode } from '../store/theme'
import { isAdminUser } from '../utils/roles'
import type {
  AiKind,
  ConsumeScanResponse,
  ProviderSettings,
  ProviderTestResult,
  Settings,
  VectorReindexResult,
} from '../types'
import { formatBytes, formatDate, humanizeError } from '../utils/format'
import { confirmAction, toast, toastError } from '../utils/toast'
import { ErrorState } from '../components/common/ErrorState'

type SettingsTab = 'ai' | 'prompts' | 'system' | 'account' | 'about'

const TABS: { key: SettingsTab; label: string }[] = [
  { key: 'ai', label: 'AI 服务' },
  { key: 'prompts', label: '提示词' },
  { key: 'system', label: '系统' },
  { key: 'account', label: '账户' },
  { key: 'about', label: '关于' },
]

const PROVIDER_OPTIONS: Record<AiKind, { label: string; value: string }[]> = {
  asr: [
    { label: '内置演示（Mock）', value: 'mock' },
    { label: '阿里云百炼 · 文件转写', value: 'dashscope_file' },
    { label: '阿里云百炼 · 实时转写（旧配置兼容，不分离）', value: 'dashscope_realtime' },
    { label: 'OpenAI 兼容接口', value: 'openai' },
  ],
  realtime_asr: [
    { label: '自动（复用文件转写的 Key）', value: 'auto' },
    { label: '内置演示（Mock）', value: 'mock' },
    { label: '阿里云百炼 · 实时转写', value: 'dashscope_realtime' },
  ],
  llm: [
    { label: '内置演示（Mock）', value: 'mock' },
    { label: 'OpenAI 兼容接口（百炼 / DeepSeek / Ollama / vLLM）', value: 'openai' },
  ],
  embeddings: [
    { label: '不启用', value: 'none' },
    { label: '内置演示（Mock 向量，仅演示/测试）', value: 'mock' },
    { label: 'OpenAI 兼容接口', value: 'openai' },
    { label: '阿里云百炼', value: 'dashscope' },
  ],
}

const PROVIDER_META: Record<AiKind, { title: string; desc: string; icon: React.ReactNode; modelPlaceholder: string; basePlaceholder: string }> = {
  asr: {
    title: '语音识别 · 文件转写',
    desc: '处理上传的录音/视频，生成带说话人与时间戳的最终转录（整场会议）',
    icon: <AudioOutlined />,
    modelPlaceholder: '如 qwen-audio-3.1-asr-flash-filetrans / paraformer-v2',
    basePlaceholder: 'https://dashscope.aliyuncs.com',
  },
  realtime_asr: {
    title: '实时转写 · 录音页字幕',
    desc: '录音页边录边出字（Beta），单独配置；不支持说话人分离，最终稿以文件转写为准',
    icon: <ThunderboltOutlined />,
    modelPlaceholder: '如 paraformer-realtime-v2',
    basePlaceholder: 'wss://dashscope.aliyuncs.com/api-ws/v1/inference',
  },
  llm: {
    title: '大语言模型（LLM）',
    desc: '生成 AI 纪要、观点树、章节、关键洞察与问答',
    icon: <RobotOutlined />,
    modelPlaceholder: '如 qwen-plus / deepseek-chat / gpt-4o-mini',
    basePlaceholder: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  },
  embeddings: {
    title: '向量嵌入（Embedding）',
    desc: '构建语义索引，用于录音内容的检索与问答召回',
    icon: <DatabaseOutlined />,
    modelPlaceholder: '如 text-embedding-v3',
    basePlaceholder: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  },
}

const REALTIME_ASR_PROVIDER_DEFAULTS: Record<string, { base: string; model: string }> = {
  auto: { base: 'wss://dashscope.aliyuncs.com/api-ws/v1/inference', model: 'paraformer-realtime-v2' },
  mock: { base: '', model: 'mock-realtime' },
  dashscope_realtime: {
    base: 'wss://dashscope.aliyuncs.com/api-ws/v1/inference',
    model: 'paraformer-realtime-v2',
  },
}

const ASR_PROVIDER_DEFAULTS: Record<string, { base: string; model: string }> = {
  dashscope_file: { base: 'https://dashscope.aliyuncs.com', model: 'paraformer-v2' },
  dashscope_realtime: {
    base: 'wss://dashscope.aliyuncs.com/api-ws/v1/inference',
    model: 'paraformer-realtime-v2',
  },
  openai: { base: 'https://api.openai.com/v1', model: 'whisper-1' },
}

const PROMPT_FIELDS = [
  { key: 'summary', label: '纪要总结', desc: '生成 AI 纪要（Markdown）时使用' },
  { key: 'chapters', label: '章节划分', desc: '生成章节标题、摘要与要点时使用' },
  { key: 'viewpoint_tree', label: '观点树', desc: '生成主题 / 观点 / 待办 / 风险结构时使用' },
  { key: 'suggested_questions', label: '猜你想问', desc: '生成 AI 面板的推荐问题列表' },
  { key: 'chat', label: 'AI 问答', desc: '问答时的系统提示词，可要求引用原文时间戳' },
]

const LANGUAGE_OPTIONS = [
  { label: '中文', value: 'zh' },
  { label: '英文', value: 'en' },
  { label: '中英混合 / 自动', value: 'auto' },
  { label: '日语', value: 'ja' },
]

export default function SettingsPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const user = useAuthStore((state) => state.user)
  const resetAuth = useAuthStore((state) => state.reset)
  const isAdmin = isAdminUser(user)
  const themeMode = useThemeStore((state) => state.mode)
  const setThemeMode = useThemeStore((state) => state.setMode)

  const tabParam = searchParams.get('tab')
  const activeTab: SettingsTab = TABS.some((tab) => tab.key === tabParam) ? (tabParam as SettingsTab) : 'ai'

  const settingsQuery = useSettings()
  const saveSettings = useSaveSettings()
  const testProvider = useTestProvider()
  const resetPrompts = useResetPrompts()
  const changePassword = useChangePassword()
  const logout = useLogout()
  const seedDemo = useSeedDemo()
  const healthQuery = useSystemHealth()
  const systemInfoQuery = useSystemInfo(activeTab === 'about' || activeTab === 'system')
  const consumeScan = useConsumeScan()
  const vectorStatusQuery = useVectorStatus(activeTab === 'system')
  const vectorReindex = useVectorReindex()

  const [aiDrafts, setAiDrafts] = useState<Settings['ai'] | null>(null)
  const [keys, setKeys] = useState<Record<AiKind, string>>({ asr: '', realtime_asr: '', llm: '', embeddings: '' })
  const [promptsDraft, setPromptsDraft] = useState<Record<string, string> | null>(null)
  const [systemDraft, setSystemDraft] = useState<Settings['system'] | null>(null)
  const [testResults, setTestResults] = useState<Partial<Record<AiKind, ProviderTestResult>>>({})
  const [testingKind, setTestingKind] = useState<AiKind | null>(null)
  const [savingKind, setSavingKind] = useState<AiKind | null>(null)
  const [scanResult, setScanResult] = useState<ConsumeScanResponse | null>(null)
  const [reindexResult, setReindexResult] = useState<{ result: VectorReindexResult; dryRun: boolean } | null>(null)
  const [passwordForm] = Form.useForm<{ old_password: string; new_password: string; confirm: string }>()

  useEffect(() => {
    document.title = '设置 · Echoic'
  }, [])

  useEffect(() => {
    const data = settingsQuery.data
    if (!data) return
    setAiDrafts((prev) => prev ?? data.ai)
    setPromptsDraft((prev) => prev ?? data.prompts)
    setSystemDraft((prev) => prev ?? data.system)
  }, [settingsQuery.data])

  const setTab = (key: SettingsTab) => {
    const params = new URLSearchParams(searchParams)
    params.set('tab', key)
    setSearchParams(params, { replace: true })
  }

  const updateProvider = (kind: AiKind, patch: Partial<ProviderSettings>) => {
    setAiDrafts((prev) => (prev ? { ...prev, [kind]: { ...prev[kind], ...patch } } : prev))
  }

  const handleTest = (kind: AiKind) => {
    const draft = aiDrafts?.[kind]
    if (!draft) return
    setTestingKind(kind)
    setTestResults((prev) => {
      const next = { ...prev }
      delete next[kind]
      return next
    })
    const payload: Record<string, unknown> = {
      provider: draft.provider,
      base_url: draft.base_url,
      model: draft.model,
    }
    if (kind === 'asr' || kind === 'realtime_asr') {
      payload.language = draft.language ?? 'zh'
    }
    if (kind === 'asr') payload.diarization = draft.diarization ?? true
    if (kind === 'llm') payload.temperature = draft.temperature ?? 0.3
    if (kind === 'embeddings') payload.dimensions = draft.dimensions ?? 1024
    if (keys[kind].trim()) payload.api_key = keys[kind].trim()

    testProvider.mutate(
      { kind, draft: payload },
      {
        onSuccess: (result) => setTestResults((prev) => ({ ...prev, [kind]: result })),
        onError: (error) =>
          setTestResults((prev) => ({
            ...prev,
            [kind]: { ok: false, latency_ms: null, message: humanizeError(error, '连接测试失败') },
          })),
        onSettled: () => setTestingKind(null),
      },
    )
  }

  const handleSaveProvider = (kind: AiKind) => {
    const draft = aiDrafts?.[kind]
    if (!draft) return
    const payload: Record<string, unknown> = {
      provider: draft.provider,
      base_url: draft.base_url,
      model: draft.model,
    }
    if (kind === 'asr' || kind === 'realtime_asr') {
      payload.language = draft.language ?? 'zh'
    }
    if (kind === 'asr') payload.diarization = draft.diarization ?? true
    if (kind === 'llm') payload.temperature = draft.temperature ?? 0.3
    if (kind === 'embeddings') payload.dimensions = draft.dimensions ?? 1024
    if (keys[kind].trim()) payload.api_key = keys[kind].trim()

    setSavingKind(kind)
    saveSettings.mutate(
      { ai: { [kind]: payload } },
      {
        onSuccess: (data) => {
          setAiDrafts(data.ai)
          setKeys((prev) => ({ ...prev, [kind]: '' }))
          toast.success(`${PROVIDER_META[kind].title}配置已保存并立即生效`)
        },
        onError: (error) => toastError(error, '保存配置失败'),
        onSettled: () => setSavingKind(null),
      },
    )
  }

  const handleClearKey = (kind: AiKind) => {
    confirmAction({
      title: '清除已保存的 API Key？',
      content: '清除后需要重新填写 API Key，AI 服务才能继续使用。',
      danger: true,
      okText: '清除',
      onOk: async () => {
        try {
          const data = await saveSettings.mutateAsync({ ai: { [kind]: { api_key: '__clear__' } } })
          setAiDrafts(data.ai)
          toast.success('API Key 已清除')
        } catch (error) {
          toastError(error, '清除失败')
        }
      },
    })
  }

  const handleSavePrompts = () => {
    if (!promptsDraft) return
    saveSettings.mutate(
      { prompts: promptsDraft },
      {
        onSuccess: (data) => {
          setPromptsDraft(data.prompts)
          toast.success('提示词已保存')
        },
        onError: (error) => toastError(error, '保存提示词失败'),
      },
    )
  }

  const handleResetPrompts = () => {
    confirmAction({
      title: '恢复默认提示词？',
      content: '所有自定义提示词都会被恢复为系统默认值。',
      okText: '恢复默认',
      onOk: async () => {
        try {
          const data = await resetPrompts.mutateAsync()
          setPromptsDraft(data.prompts)
          toast.success('已恢复默认提示词')
        } catch (error) {
          toastError(error, '恢复失败')
        }
      },
    })
  }

  const handleSaveSystem = () => {
    if (!systemDraft) return
    saveSettings.mutate(
      {
        system: {
          auto_process: systemDraft.auto_process,
          default_language: systemDraft.default_language,
          max_upload_mb: systemDraft.max_upload_mb,
          allow_registration: Boolean(systemDraft.allow_registration),
        },
      },
      {
        onSuccess: (data) => {
          setSystemDraft(data.system)
          toast.success('系统设置已保存')
        },
        onError: (error) => toastError(error, '保存系统设置失败'),
      },
    )
  }

  const handleChangePassword = (values: { old_password: string; new_password: string; confirm: string }) => {
    changePassword.mutate(
      { old_password: values.old_password, new_password: values.new_password },
      {
        onSuccess: () => {
          toast.success('密码修改成功，请牢记新密码')
          passwordForm.resetFields()
        },
        onError: (error) => toastError(error, '密码修改失败'),
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

  const demoMode = settingsQuery.data?.system.demo_mode ?? false
  const diskPercent = useMemo(() => {
    const disk = systemInfoQuery.data?.disk
    if (!disk || !disk.total_bytes) return 0
    return Math.round((disk.used_bytes / disk.total_bytes) * 100)
  }, [systemInfoQuery.data?.disk])

  if (settingsQuery.isLoading) {
    return (
      <div className="settings-page">
        <div className="page-header">
          <Skeleton active title={{ width: 120 }} paragraph={{ rows: 1, width: ['40%'] }} />
        </div>
        <Skeleton active paragraph={{ rows: 8 }} />
      </div>
    )
  }

  if (settingsQuery.isError) {
    return (
      <div className="settings-page">
        <ErrorState
          title="设置加载失败"
          description={settingsQuery.error?.message || '需要管理员权限，或服务暂时不可用。'}
          onRetry={() => void settingsQuery.refetch()}
        />
      </div>
    )
  }

  const renderAiTab = () => {
    if (!aiDrafts) return <Skeleton active />
    return (
      <div className="settings-tab">
        {demoMode && (
          <Alert
            type="info"
            showIcon
            icon={<InfoCircleOutlined />}
            message="当前处于演示模式"
            description="AI 输出为内置示例数据。配置阿里云百炼（DashScope）等真实模型后，上传的录音会进行真实转写与分析。"
          />
        )}
        {(Object.keys(PROVIDER_META) as AiKind[]).map((kind) => {
          const meta = PROVIDER_META[kind]
          const draft = aiDrafts[kind]
          const result = testResults[kind]
          const providerDefaults =
            kind === 'asr'
              ? ASR_PROVIDER_DEFAULTS[draft.provider]
              : kind === 'realtime_asr'
                ? REALTIME_ASR_PROVIDER_DEFAULTS[draft.provider]
                : undefined
          const basePlaceholder = providerDefaults?.base ?? meta.basePlaceholder
          const modelPlaceholder = providerDefaults?.model ?? meta.modelPlaceholder
          return (
            <section className="settings-card" key={kind}>
              <header className="settings-card__head">
                <div className="settings-card__title">
                  <span className={`settings-card__icon settings-card__icon--${kind}`}>{meta.icon}</span>
                  <div>
                    <h3>{meta.title}</h3>
                    <p>{meta.desc}</p>
                  </div>
                </div>
                <div className="settings-card__tags">
                  {draft.configured ? (
                    <Tag color="success" icon={<CheckCircleFilled />}>
                      已配置
                    </Tag>
                  ) : (
                    <Tag color="default">未配置</Tag>
                  )}
                </div>
              </header>

              {kind === 'realtime_asr' && (
                <Alert
                  type={systemDraft?.live_enabled ? 'info' : 'warning'}
                  showIcon
                  style={{ marginBottom: 14 }}
                  message={systemDraft?.live_enabled ? '实时转写已启用' : '实时转写当前未启用'}
                  description={
                    systemDraft?.live_enabled
                      ? '录音页会显示实时字幕；先点「测试连接」验证实时 WebSocket 可用，再保存。选“自动”时若 API Key 留空，会复用上方文件转写配置的 Key。'
                      : '需要在 .env 设置 ECHOIC_LIVE_ENABLED=true 并重启服务后，录音页才会出现实时字幕。此处的配置会提前保存，启用后立即生效。'
                  }
                />
              )}
              {kind === 'asr' && (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 14 }}
                  message="文件转写用于整场录音的最终转录，支持说话人分离"
                  description="可选择 qwen-audio-3.1-asr-flash-filetrans / paraformer-v2 / fun-asr；录音页的实时字幕在下方单独配置。"
                />
              )}

              <div className="settings-form-grid">
                <label className="settings-field">
                  <span className="settings-field__label">服务提供商</span>
                  <Select
                    value={draft.provider}
                    options={PROVIDER_OPTIONS[kind]}
                    onChange={(value) => {
                      if (kind !== 'asr' && kind !== 'realtime_asr') {
                        updateProvider(kind, { provider: value })
                        return
                      }
                      const defaults =
                        kind === 'asr' ? ASR_PROVIDER_DEFAULTS[value] : REALTIME_ASR_PROVIDER_DEFAULTS[value]
                      const patch: Partial<ProviderSettings> = { provider: value }
                      if (defaults) {
                        const knownDefaults =
                          kind === 'asr'
                            ? Object.values(ASR_PROVIDER_DEFAULTS)
                            : Object.values(REALTIME_ASR_PROVIDER_DEFAULTS)
                        if (!draft.base_url || knownDefaults.some((item) => item.base === draft.base_url)) {
                          patch.base_url = defaults.base
                        }
                        if (!draft.model || knownDefaults.some((item) => item.model === draft.model)) {
                          patch.model = defaults.model
                        }
                      }
                      updateProvider(kind, patch)
                    }}
                  />
                </label>
                <label className="settings-field">
                  <span className="settings-field__label">模型</span>
                  <Input
                    value={draft.model}
                    placeholder={modelPlaceholder}
                    onChange={(event) => updateProvider(kind, { model: event.target.value })}
                  />
                </label>
                <label className="settings-field settings-field--wide">
                  <span className="settings-field__label">Base URL</span>
                  <Input
                    value={draft.base_url}
                    placeholder={basePlaceholder}
                    onChange={(event) => updateProvider(kind, { base_url: event.target.value })}
                  />
                  {kind === 'asr' && (
                    <span className="settings-field__hint">
                      文件转写填 <code>https://dashscope.aliyuncs.com</code>（不要带 /api/v1 或
                      /compatible-mode/v1）；使用工作空间域名时填到域名根即可，服务端会自动补全。
                    </span>
                  )}
                  {kind === 'realtime_asr' && (
                    <span className="settings-field__hint">
                      填 <code>wss://dashscope.aliyuncs.com/api-ws/v1/inference</code>，或只填域名
                      <code> dashscope.aliyuncs.com</code>（服务端自动补全路径）。选“自动”且 API Key
                      留空时，会复用文件转写配置的 Key。
                    </span>
                  )}
                </label>

                {(kind === 'asr' || kind === 'realtime_asr') && (
                  <>
                    <label className="settings-field">
                      <span className="settings-field__label">识别语言</span>
                      <Select
                        value={draft.language ?? 'zh'}
                        options={LANGUAGE_OPTIONS}
                        onChange={(value) => updateProvider(kind, { language: value })}
                      />
                    </label>
                    {kind === 'asr' ? (
                      <div className="settings-field settings-field--switch">
                        <span className="settings-field__label">说话人分离（Diarization）</span>
                        <Switch
                          checked={draft.diarization ?? true}
                          onChange={(checked) => updateProvider(kind, { diarization: checked })}
                        />
                      </div>
                    ) : (
                      <div className="settings-field settings-field--wide">
                        <span className="settings-field__hint">
                          实时转写不支持说话人分离；若要区分说话人，请用上面的「文件转写」处理整场录音。
                          最终稿会以文件转写为准（replace 模式）。
                        </span>
                      </div>
                    )}
                  </>
                )}

                {kind === 'llm' && (
                  <label className="settings-field">
                    <span className="settings-field__label">Temperature</span>
                    <InputNumber
                      min={0}
                      max={2}
                      step={0.1}
                      style={{ width: '100%' }}
                      value={draft.temperature ?? 0.3}
                      onChange={(value) => updateProvider(kind, { temperature: value ?? 0.3 })}
                    />
                  </label>
                )}

                {kind === 'embeddings' && (
                  <label className="settings-field">
                    <span className="settings-field__label">向量维度</span>
                    <InputNumber
                      min={64}
                      max={8192}
                      step={64}
                      style={{ width: '100%' }}
                      value={draft.dimensions ?? 1024}
                      onChange={(value) => updateProvider(kind, { dimensions: value ?? 1024 })}
                    />
                  </label>
                )}

                <label className="settings-field settings-field--wide">
                  <span className="settings-field__label">API Key</span>
                  <Input.Password
                    value={keys[kind]}
                    placeholder={draft.has_api_key ? `已配置：${draft.api_key_masked}（留空保持不变）` : '请输入 API Key'}
                    onChange={(event) => setKeys((prev) => ({ ...prev, [kind]: event.target.value }))}
                  />
                  <span className="settings-field__hint">
                    API Key 仅保存在你的服务器上，接口只返回掩码。
                    {draft.has_api_key && (
                      <Button type="link" size="small" danger onClick={() => handleClearKey(kind)}>
                        清除已保存的 Key
                      </Button>
                    )}
                  </span>
                </label>
              </div>

              {kind === 'embeddings' && draft.provider === 'mock' && (
                <div className="settings-inline-hint">
                  <PartitionOutlined /> Mock 向量使用确定性哈希（dim 256），仅用于演示和功能测试；
                  生产环境建议配置 OpenAI / 阿里云百炼 Embedding 并重建索引。
                </div>
              )}

              {result && (
                <Alert
                  className="settings-test-result"
                  type={result.ok ? 'success' : 'error'}
                  showIcon
                  icon={result.ok ? <CheckCircleFilled /> : <WarningFilled />}
                  message={result.message || (result.ok ? '连接成功' : '连接失败')}
                  description={result.ok && result.latency_ms ? `耗时 ${result.latency_ms} ms` : undefined}
                />
              )}

              <footer className="settings-card__actions">
                <Button
                  icon={<ExperimentOutlined />}
                  loading={testingKind === kind}
                  onClick={() => handleTest(kind)}
                >
                  测试连接
                </Button>
                <Button type="primary" loading={savingKind === kind} onClick={() => handleSaveProvider(kind)}>
                  保存配置
                </Button>
              </footer>
            </section>
          )
        })}
      </div>
    )
  }

  const renderPromptsTab = () => {
    if (!promptsDraft) return <Skeleton active />
    return (
      <div className="settings-tab">
        <Alert
          type="info"
          showIcon
          message="提示词会影响 AI 输出风格"
          description="修改后立即生效，作用于下一次处理与问答。建议保留「时间戳」「JSON 结构」等关键约束。"
        />
        <section className="settings-card">
          {PROMPT_FIELDS.map((field) => (
            <label className="prompt-field" key={field.key}>
              <div className="prompt-field__head">
                <span className="prompt-field__label">{field.label}</span>
                <span className="prompt-field__desc">{field.desc}</span>
              </div>
              <Input.TextArea
                value={promptsDraft[field.key] ?? ''}
                autoSize={{ minRows: 3, maxRows: 10 }}
                onChange={(event) =>
                  setPromptsDraft((prev) => (prev ? { ...prev, [field.key]: event.target.value } : prev))
                }
              />
            </label>
          ))}
          <footer className="settings-card__actions">
            <Button icon={<ReloadOutlined />} onClick={handleResetPrompts} loading={resetPrompts.isPending}>
              恢复默认
            </Button>
            <Button type="primary" onClick={handleSavePrompts} loading={saveSettings.isPending}>
              保存提示词
            </Button>
          </footer>
        </section>
      </div>
    )
  }

  const handleReindex = (dryRun: boolean) => {
    vectorReindex.mutate(
      { dry_run: dryRun },
      {
        onSuccess: (result) => {
          setReindexResult({ result, dryRun })
          if (dryRun) toast.success(`试运行完成：将处理 ${result.processed_recordings} 条录音、${result.chunks} 个切片`)
          else toast.success(`索引完成：${result.chunks} 个切片${result.errors.length ? `，${result.errors.length} 个错误` : ''}`)
          void vectorStatusQuery.refetch()
        },
        onError: (error) => toastError(error, '重建索引失败'),
      },
    )
  }

  const handleConsumeScan = () => {
    consumeScan.mutate(undefined, {
      onSuccess: (data) => {
        setScanResult(data)
        const result = data.result
        toast.success(`扫描完成：导入 ${result.imported} 个，重复 ${result.duplicates} 个，失败 ${result.failed} 个`)
        void systemInfoQuery.refetch()
      },
      onError: (error) => toastError(error, '触发扫描失败'),
    })
  }

  const renderSystemTab = () => {
    if (!systemDraft) return <Skeleton active />
    const worker = settingsQuery.data?.system.worker
    const consumeInfo = systemInfoQuery.data?.consume
    const consumeBasic = settingsQuery.data?.system.consume
    const displayConsume = {
      enabled: consumeInfo?.enabled ?? consumeBasic?.enabled ?? false,
      dir: consumeInfo?.dir || consumeBasic?.dir || '—',
      intervalSec: consumeInfo?.interval_sec ?? consumeBasic?.interval_sec ?? 15,
      pending: consumeInfo?.pending_files,
      failed: consumeInfo?.failed_files,
    }
    const lastScan = scanResult
      ? { ...scanResult.result, at: scanResult.at }
      : (consumeInfo?.last_scan ?? null)
    return (
      <div className="settings-tab">
        {demoMode && (
          <Alert
            type="info"
            showIcon
            message="演示模式已开启"
            description="在「AI 服务」中把 ASR / LLM 的 Provider 从内置演示（Mock）切换为真实服务后，即会关闭演示模式。"
          />
        )}
        <section className="settings-card">
          <header className="settings-card__head">
            <div className="settings-card__title">
              <span className="settings-card__icon settings-card__icon--system">
                <SettingOutlined />
              </span>
              <div>
                <h3>系统默认值</h3>
                <p>上传与处理任务的全局默认配置</p>
              </div>
            </div>
          </header>
          <div className="settings-form-grid">
            <label className="settings-field">
              <span className="settings-field__label">单文件上传上限（MB）</span>
              <InputNumber
                min={1}
                max={8192}
                style={{ width: '100%' }}
                value={systemDraft.max_upload_mb}
                onChange={(value) =>
                  setSystemDraft((prev) => (prev ? { ...prev, max_upload_mb: value ?? 2048 } : prev))
                }
              />
            </label>
            <label className="settings-field">
              <span className="settings-field__label">默认识别语言</span>
              <Select
                value={systemDraft.default_language}
                options={LANGUAGE_OPTIONS}
                onChange={(value) => setSystemDraft((prev) => (prev ? { ...prev, default_language: value } : prev))}
              />
            </label>
            <div className="settings-field settings-field--switch">
              <span className="settings-field__label">上传后自动处理</span>
              <Switch
                checked={systemDraft.auto_process}
                onChange={(checked) =>
                  setSystemDraft((prev) => (prev ? { ...prev, auto_process: checked } : prev))
                }
              />
            </div>
            <div className="settings-field settings-field--switch">
              <span className="settings-field__label">开放注册（成员自助注册）</span>
              <Switch
                checked={Boolean(systemDraft.allow_registration)}
                onChange={(checked) =>
                  setSystemDraft((prev) => (prev ? { ...prev, allow_registration: checked } : prev))
                }
              />
            </div>
          </div>
          <div className="settings-field__hint settings-card__hint">
            开放注册后，访客可以通过 <code>/api/auth/register</code> 自助注册为「成员」，仅能访问自己的录音；默认关闭。
          </div>
          <footer className="settings-card__actions">
            <Button type="primary" onClick={handleSaveSystem} loading={saveSettings.isPending}>
              保存系统设置
            </Button>
          </footer>
        </section>

        <section className="settings-card">
          <header className="settings-card__head">
            <div className="settings-card__title">
              <span className="settings-card__icon settings-card__icon--consume">
                <FolderOpenOutlined />
              </span>
              <div>
                <h3>目录自动导入（Consume）</h3>
                <p>把音视频文件放进监控目录，worker 会自动导入并跳过重复文件</p>
              </div>
            </div>
            <Tag color={displayConsume.enabled ? 'success' : 'default'}>
              {displayConsume.enabled ? '已启用' : '未启用'}
            </Tag>
          </header>
          <div className="settings-info-grid">
            <div className="settings-info-grid__wide">
              <span className="settings-info-grid__label">监控目录</span>
              <span className="settings-info-grid__value settings-info-grid__value--mono">{displayConsume.dir}</span>
            </div>
            <div>
              <span className="settings-info-grid__label">扫描间隔</span>
              <span className="settings-info-grid__value">{displayConsume.intervalSec} 秒</span>
            </div>
            <div>
              <span className="settings-info-grid__label">待处理文件</span>
              <span className="settings-info-grid__value">{displayConsume.pending ?? '—'}</span>
            </div>
            <div>
              <span className="settings-info-grid__label">失败文件</span>
              <span className="settings-info-grid__value">{displayConsume.failed ?? '—'}</span>
            </div>
          </div>
          {lastScan && (
            <Alert
              className="settings-test-result"
              type={lastScan.failed > 0 ? 'warning' : 'info'}
              showIcon
              message={`最近一次扫描${lastScan.at ? `：${formatDate(lastScan.at)}` : ''}`}
              description={`扫描 ${lastScan.scanned} 个文件 · 导入 ${lastScan.imported} 个 · 重复 ${lastScan.duplicates} 个 · 失败 ${lastScan.failed} 个`}
            />
          )}
          <footer className="settings-card__actions">
            <Button icon={<ScanOutlined />} loading={consumeScan.isPending} onClick={handleConsumeScan}>
              立即扫描
            </Button>
            <Button icon={<ReloadOutlined />} loading={systemInfoQuery.isFetching} onClick={() => void systemInfoQuery.refetch()}>
              刷新状态
            </Button>
          </footer>
        </section>

        <section className="settings-card">
          <header className="settings-card__head">
            <div className="settings-card__title">
              <span className="settings-card__icon settings-card__icon--vector">
                <PartitionOutlined />
              </span>
              <div>
                <h3>检索索引（向量搜索）</h3>
                <p>语义/混合检索依赖的切片向量索引；配置 Embedding 后可重建</p>
              </div>
            </div>
            {vectorStatusQuery.data ? (
              <Tag color={vectorStatusQuery.data.available ? 'success' : 'warning'}>
                {vectorStatusQuery.data.backend} {vectorStatusQuery.data.available ? '可用' : '降级'}
              </Tag>
            ) : null}
          </header>

          {vectorStatusQuery.isLoading ? (
            <Skeleton active paragraph={{ rows: 2 }} />
          ) : vectorStatusQuery.isError ? (
            <ErrorState
              compact
              title="索引状态加载失败"
              description={vectorStatusQuery.error?.message || '请稍后重试。'}
              onRetry={() => void vectorStatusQuery.refetch()}
            />
          ) : vectorStatusQuery.data ? (
            <>
              <div className="settings-info-grid">
                <div>
                  <span className="settings-info-grid__label">向量后端</span>
                  <span className="settings-info-grid__value">{vectorStatusQuery.data.backend}</span>
                </div>
                <div>
                  <span className="settings-info-grid__label">Embedding 模型</span>
                  <span className="settings-info-grid__value">
                    {vectorStatusQuery.data.embedding_model || (vectorStatusQuery.data.embeddings_configured ? '已配置' : '未配置')}
                  </span>
                </div>
                <div>
                  <span className="settings-info-grid__label">维度</span>
                  <span className="settings-info-grid__value">{vectorStatusQuery.data.dim ?? '—'}</span>
                </div>
                <div>
                  <span className="settings-info-grid__label">已索引 / 总切片</span>
                  <span className="settings-info-grid__value">
                    {vectorStatusQuery.data.chunks_indexed} / {vectorStatusQuery.data.chunks_total}
                  </span>
                </div>
                <div>
                  <span className="settings-info-grid__label">过期切片</span>
                  <span className="settings-info-grid__value">{vectorStatusQuery.data.stale_chunks}</span>
                </div>
              </div>
              {vectorStatusQuery.data.fallback_reason && (
                <Alert
                  className="settings-test-result"
                  type="warning"
                  showIcon
                  message="向量后端已降级"
                  description={vectorStatusQuery.data.fallback_reason}
                />
              )}
              {!vectorStatusQuery.data.embeddings_configured && (
                <Alert
                  className="settings-test-result"
                  type="info"
                  showIcon
                  message="尚未配置 Embedding 模型"
                  description="可在上方「向量嵌入（Embedding）」中配置 OpenAI / 阿里云百炼，或使用内置 Mock 进行演示，然后重建索引。"
                />
              )}
              {reindexResult && (
                <Alert
                  className="settings-test-result"
                  type={reindexResult.result.errors.length ? 'warning' : 'success'}
                  showIcon
                  message={`${reindexResult.dryRun ? '试运行' : '重建'}完成：处理 ${reindexResult.result.processed_recordings} 条录音，${reindexResult.result.chunks} 个切片，跳过 ${reindexResult.result.skipped} 个`}
                  description={
                    reindexResult.result.errors.length
                      ? `错误（前 3 条）：${reindexResult.result.errors.slice(0, 3).join('；')}`
                      : undefined
                  }
                />
              )}
              <footer className="settings-card__actions">
                <Tooltip title={vectorStatusQuery.data.embeddings_configured ? '' : '请先配置 Embedding 模型'}>
                  <Button
                    icon={<ExperimentOutlined />}
                    disabled={!vectorStatusQuery.data.embeddings_configured || vectorReindex.isPending}
                    loading={vectorReindex.isPending && vectorReindex.variables?.dry_run === true}
                    onClick={() => handleReindex(true)}
                  >
                    试运行
                  </Button>
                </Tooltip>
                <Tooltip title={vectorStatusQuery.data.embeddings_configured ? '' : '请先配置 Embedding 模型'}>
                  <Button
                    type="primary"
                    icon={<ThunderboltOutlined />}
                    disabled={!vectorStatusQuery.data.embeddings_configured || vectorReindex.isPending}
                    loading={vectorReindex.isPending && vectorReindex.variables?.dry_run !== true}
                    onClick={() => handleReindex(false)}
                  >
                    重建索引
                  </Button>
                </Tooltip>
                <Button icon={<ReloadOutlined />} loading={vectorStatusQuery.isFetching} onClick={() => void vectorStatusQuery.refetch()}>
                  刷新状态
                </Button>
              </footer>
            </>
          ) : null}
        </section>

        <section className="settings-card">
          <header className="settings-card__head">
            <div className="settings-card__title">
              <span className="settings-card__icon settings-card__icon--worker">
                <CloudServerOutlined />
              </span>
              <div>
                <h3>后台处理服务</h3>
                <p>内嵌 worker 负责转写与 AI 分析任务</p>
              </div>
            </div>
            <Tag color={worker?.running ? 'success' : 'warning'}>{worker?.running ? '运行中' : '未运行'}</Tag>
          </header>
          <div className="settings-info-grid">
            <div>
              <span className="settings-info-grid__label">运行模式</span>
              <span className="settings-info-grid__value">{worker?.embedded ? '内嵌于 API 进程' : '独立进程'}</span>
            </div>
            <div>
              <span className="settings-info-grid__label">队列深度</span>
              <span className="settings-info-grid__value">{worker?.queue_depth ?? 0}</span>
            </div>
            <div>
              <span className="settings-info-grid__label">当前版本</span>
              <span className="settings-info-grid__value">v{settingsQuery.data?.system.version ?? '0.1.0'}</span>
            </div>
          </div>
          <footer className="settings-card__actions">
            <Tooltip title="仅管理员可用，会创建一条示例录音">
              <Button
                icon={<ThunderboltOutlined />}
                loading={seedDemo.isPending}
                disabled={!isAdmin}
                onClick={() =>
                  seedDemo.mutate(undefined, {
                    onSuccess: (data) => toast.success(data.created > 0 ? '演示数据已创建' : '演示数据已存在'),
                    onError: (error) => toastError(error, '创建演示数据失败'),
                  })
                }
              >
                载入演示数据
              </Button>
            </Tooltip>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => {
                void queryClient.invalidateQueries()
                toast.success('已刷新配置')
              }}
            >
              刷新
            </Button>
          </footer>
        </section>
      </div>
    )
  }

  const renderAccountTab = () => (
    <div className="settings-tab">
      <section className="settings-card">
        <header className="settings-card__head">
          <div className="settings-card__title">
            <span className="settings-card__icon settings-card__icon--account">
              <UserOutlined />
            </span>
            <div>
              <h3>账户信息</h3>
              <p>当前登录的账号</p>
            </div>
          </div>
          <Tag color={isAdmin ? 'blue' : 'default'}>{isAdmin ? '管理员' : '成员'}</Tag>
        </header>
        <div className="settings-account">
          <div className="settings-account__avatar">{(user?.display_name || user?.username || 'U').slice(0, 1)}</div>
          <div>
            <div className="settings-account__name">{user?.display_name || '未登录'}</div>
            <div className="settings-account__meta">@{user?.username} · 注册于 {formatDate(user?.created_at)}</div>
          </div>
        </div>
      </section>

      <section className="settings-card">
        <header className="settings-card__head">
          <div className="settings-card__title">
            <span className="settings-card__icon settings-card__icon--account">
              <BgColorsOutlined />
            </span>
            <div>
              <h3>外观主题</h3>
              <p>选择浅色或深色界面，也可以跟随系统自动切换</p>
            </div>
          </div>
        </header>
        <div className="settings-appearance">
          <Segmented
            value={themeMode}
            options={[
              { label: '浅色', value: 'light', icon: <SunOutlined /> },
              { label: '深色', value: 'dark', icon: <MoonOutlined /> },
              { label: '跟随系统', value: 'system', icon: <DesktopOutlined /> },
            ]}
            onChange={(value) => setThemeMode(value as ThemeMode)}
          />
          <span className="muted-text">主题设置会保存在当前浏览器，不会同步到其他设备。</span>
        </div>
      </section>

      <section className="settings-card">
        <header className="settings-card__head">
          <div className="settings-card__title">
            <span className="settings-card__icon settings-card__icon--password">
              <SafetyCertificateOutlined />
            </span>
            <div>
              <h3>修改密码</h3>
              <p>修改后当前会话保持登录，下次使用新密码</p>
            </div>
          </div>
        </header>
        <Form form={passwordForm} layout="vertical" onFinish={handleChangePassword} className="settings-password-form">
          <Form.Item name="old_password" label="当前密码" rules={[{ required: true, message: '请输入当前密码' }]}>
            <Input.Password placeholder="输入当前密码" autoComplete="current-password" />
          </Form.Item>
          <Form.Item
            name="new_password"
            label="新密码"
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 6, message: '密码至少 6 位' },
            ]}
          >
            <Input.Password placeholder="至少 6 位字符" autoComplete="new-password" />
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
            <Input.Password placeholder="再次输入新密码" autoComplete="new-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={changePassword.isPending}>
            修改密码
          </Button>
        </Form>
      </section>

      <section className="settings-card">
        <footer className="settings-card__actions settings-card__actions--between">
          <span className="muted-text">退出后需要重新登录才能访问录音库</span>
          <Button danger icon={<LogoutOutlined />} onClick={handleLogout} loading={logout.isPending}>
            退出登录
          </Button>
        </footer>
      </section>
    </div>
  )

  const renderAboutTab = () => (
    <div className="settings-tab">
      <section className="settings-card">
        <header className="settings-card__head">
          <div className="settings-card__title">
            <span className="settings-card__icon settings-card__icon--about">
              <InfoCircleOutlined />
            </span>
            <div>
              <h3>Echoic 私人会议录音知识库</h3>
              <p>本地部署 · 隐私优先 · 版本 v{healthQuery.data?.version ?? settingsQuery.data?.system.version ?? '0.1.0'}</p>
            </div>
          </div>
        </header>
        <p className="settings-about__text">
          Echoic 帮你在本地把会议录音变成可检索的知识：自动转写与说话人分离、AI 纪要、观点树、章节跳转，
          以及带原文引用的自然语言问答。所有数据保存在你自己的服务器上。
        </p>
        <div className="settings-tech-tags">
          {['React 18', 'TypeScript', 'Vite', 'Ant Design 5', 'TanStack Query', 'React Flow', 'FastAPI', '阿里云百炼'].map(
            (tech) => (
              <Tag key={tech}>{tech}</Tag>
            ),
          )}
        </div>
      </section>

      <section className="settings-card">
        <header className="settings-card__head">
          <div className="settings-card__title">
            <span className="settings-card__icon settings-card__icon--health">
              <CheckCircleFilled />
            </span>
            <div>
              <h3>服务状态</h3>
              <p>来自 /api/system/health 的实时检测结果</p>
            </div>
          </div>
        </header>
        {healthQuery.isError ? (
          <Alert type="warning" showIcon message="无法获取服务状态" description="请确认后端服务正在运行。" />
        ) : (
          <div className="settings-health">
            {[
              { label: '数据库', ok: healthQuery.data?.db === 'ok' },
              { label: '媒体目录', ok: healthQuery.data?.media === 'ok' },
              { label: 'ffmpeg', ok: healthQuery.data?.ffmpeg === 'ok' },
              { label: 'Worker', ok: healthQuery.data?.worker === 'ok' },
            ].map((item) => (
              <span className={`settings-health__item ${item.ok ? 'is-ok' : 'is-warn'}`} key={item.label}>
                {item.ok ? <CheckCircleFilled /> : <WarningFilled />} {item.label}
              </span>
            ))}
          </div>
        )}
      </section>

      <section className="settings-card">
        <header className="settings-card__head">
          <div className="settings-card__title">
            <span className="settings-card__icon settings-card__icon--storage">
              <DatabaseOutlined />
            </span>
            <div>
              <h3>存储与数据</h3>
              <p>本地磁盘使用情况</p>
            </div>
          </div>
        </header>
        {systemInfoQuery.isLoading ? (
          <Skeleton active paragraph={{ rows: 2 }} />
        ) : systemInfoQuery.isError ? (
          <div className="muted-text">系统信息接口暂不可用</div>
        ) : systemInfoQuery.data ? (
          <>
            <div className="settings-storage">
              <div className="settings-storage__row">
                <span>磁盘使用</span>
                <span>
                  {formatBytes(systemInfoQuery.data.disk.used_bytes)} / {formatBytes(systemInfoQuery.data.disk.total_bytes)}
                </span>
              </div>
              <Progress percent={diskPercent} showInfo={false} strokeColor={{ from: '#69b1ff', to: '#1677ff' }} />
            </div>
            <div className="settings-info-grid">
              <div>
                <span className="settings-info-grid__label">录音</span>
                <span className="settings-info-grid__value">{systemInfoQuery.data.recordings}</span>
              </div>
              <div>
                <span className="settings-info-grid__label">转录片段</span>
                <span className="settings-info-grid__value">{systemInfoQuery.data.segments}</span>
              </div>
              <div>
                <span className="settings-info-grid__label">语义切片</span>
                <span className="settings-info-grid__value">{systemInfoQuery.data.chunks}</span>
              </div>
              <div>
                <span className="settings-info-grid__label">数据目录</span>
                <span className="settings-info-grid__value settings-info-grid__value--mono">
                  {systemInfoQuery.data.data_dir}
                </span>
              </div>
            </div>
          </>
        ) : null}
      </section>

      <section className="settings-card">
        <footer className="settings-card__actions settings-card__actions--between">
          <span className="muted-text">
            <SafetyCertificateOutlined /> 数据仅存储在你自己的服务器上，不会上传到第三方。
          </span>
          <Button
            icon={<DeleteOutlined />}
            onClick={() =>
              toast.info('如需清空数据，请直接删除数据目录中的文件，或联系管理员。')
            }
          >
            数据说明
          </Button>
        </footer>
      </section>
    </div>
  )

  return (
    <div className="settings-page">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">设置</h1>
          <p className="page-header__subtitle">配置 AI 服务、提示词与系统默认值</p>
        </div>
        <Tag icon={<UserOutlined />}>{user?.display_name || user?.username || '未登录'}</Tag>
      </header>

      <Tabs
        activeKey={activeTab}
        onChange={(key) => setTab(key as SettingsTab)}
        items={TABS.map((tab) => ({ key: tab.key, label: tab.label }))}
        className="settings-tabs"
      />

      {activeTab === 'ai' && renderAiTab()}
      {activeTab === 'prompts' && renderPromptsTab()}
      {activeTab === 'system' && renderSystemTab()}
      {activeTab === 'account' && renderAccountTab()}
      {activeTab === 'about' && renderAboutTab()}
    </div>
  )
}
