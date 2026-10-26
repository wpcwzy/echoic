export const theme = {
  colorPrimary: '#1677ff',
  colorBgLayout: '#f5f6f7',
  colorBgContainer: '#ffffff',
  colorBorderSecondary: '#eceef1',
  colorText: '#1f2329',
  colorTextSecondary: '#646a73',
  borderRadius: 10,
  borderRadiusLG: 14,
  fontFamily:
    '"Inter", -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
}

export const speakerPalette = ['#5B8FF9', '#5AD8A6', '#F6BD16', '#E8684A', '#6DC8EC', '#9270CA', '#FF9D4D', '#269A99']

export const statusMeta: Record<string, { label: string; color: string }> = {
  uploaded: { label: '已上传', color: 'default' },
  queued: { label: '排队中', color: 'processing' },
  processing: { label: '处理中', color: 'processing' },
  ready: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
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
