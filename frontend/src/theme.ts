import type { ThemeConfig } from 'antd'
import type { Insight, RecordingStatus } from './types'

/**
 * Ant Design theme — light, airy, Feishu-esque.
 * The visual contract lives in docs/FRONTEND.md and styles/app.css.
 */
export const theme: ThemeConfig = {
  token: {
    colorPrimary: '#1677ff',
    colorBgLayout: '#f5f6f7',
    colorBgContainer: '#ffffff',
    colorBorderSecondary: '#eceef1',
    colorBorder: '#e3e5e8',
    colorText: '#1f2329',
    colorTextSecondary: '#646a73',
    colorTextTertiary: '#8f959e',
    borderRadius: 10,
    borderRadiusLG: 14,
    borderRadiusSM: 8,
    fontSize: 14,
    controlHeight: 34,
    controlHeightLG: 40,
    fontFamily:
      '"Inter", -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
    boxShadowSecondary: '0 6px 24px rgba(31, 35, 41, 0.08)',
  },
  components: {
    Button: {
      fontWeight: 500,
      primaryShadow: 'none',
      defaultShadow: 'none',
      dangerShadow: 'none',
    },
    Tabs: {
      inkBarColor: '#1677ff',
      itemColor: '#646a73',
      itemSelectedColor: '#1f2329',
      itemHoverColor: '#1f2329',
      horizontalItemGutter: 28,
      titleFontSize: 14,
    },
    Segmented: {
      trackBg: '#f0f1f3',
      itemSelectedBg: '#ffffff',
      itemColor: '#646a73',
      itemSelectedColor: '#1f2329',
    },
    Input: {
      activeShadow: '0 0 0 3px rgba(22, 119, 255, 0.10)',
    },
    Modal: {
      titleFontSize: 16,
    },
  },
}

export const speakerPalette = [
  '#5B8FF9',
  '#5AD8A6',
  '#F6BD16',
  '#E8684A',
  '#6DC8EC',
  '#9270CA',
  '#FF9D4D',
  '#269A99',
]

export interface StatusMeta {
  label: string
  color: string
  bg: string
  dot: string
}

export const statusMeta: Record<RecordingStatus, StatusMeta> = {
  uploaded: { label: '已上传', color: '#646a73', bg: '#f2f3f5', dot: '#8f959e' },
  queued: { label: '排队中', color: '#ad6800', bg: '#fff7e6', dot: '#faad14' },
  processing: { label: '处理中', color: '#1677ff', bg: '#eaf2fe', dot: '#1677ff' },
  ready: { label: '已完成', color: '#0f7b4d', bg: '#e9f7ef', dot: '#22c55e' },
  failed: { label: '处理失败', color: '#cf1322', bg: '#fff1f0', dot: '#ff4d4f' },
  cancelled: { label: '已取消', color: '#646a73', bg: '#f2f3f5', dot: '#8f959e' },
}

export const stageLabel: Record<string, string> = {
  preparing: '解析媒体',
  transcribing: '语音转写',
  analyzing: 'AI 分析',
  embedding: '构建索引',
  finalizing: '收尾',
  done: '完成',
  failed: '失败',
}

export const stageHint: Record<string, string> = {
  preparing: '正在解析媒体文件、读取时长与音量信息…',
  transcribing: '正在识别说话人并生成逐字稿，长录音需要一些时间…',
  analyzing: '正在生成 AI 纪要、观点树、章节与关键洞察…',
  embedding: '正在构建语义索引，之后即可进行 AI 问答…',
  finalizing: '正在保存处理结果…',
  done: '处理完成',
  failed: '处理遇到问题',
}

export interface CategoryMeta {
  label: string
  color: string
  bg: string
  icon: string
}

export const categoryMeta: Record<Insight['category'], CategoryMeta> = {
  decision: { label: '决策', color: '#0958d9', bg: '#e8f1fe', icon: '✓' },
  risk: { label: '风险', color: '#cf1322', bg: '#fff1f0', icon: '!' },
  insight: { label: '洞察', color: '#722ed1', bg: '#f5edff', icon: '✦' },
  opportunity: { label: '机会', color: '#0f7b4d', bg: '#e9f7ef', icon: '↗' },
}

export const analysisKindLabel: Record<string, string> = {
  summary: 'AI 纪要',
  chapters: '章节',
  viewpoint_tree: '观点树',
  insights: '关键洞察',
  action_items: '待办事项',
  suggested_questions: '猜你想问',
  keywords: '关键词',
}
