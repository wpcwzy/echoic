import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, Dropdown, Input, Modal, Popover, Select, Tooltip } from 'antd'
import type { MenuProps } from 'antd'
import {
  CheckOutlined,
  CloseOutlined,
  DownOutlined,
  DownloadOutlined,
  EditOutlined,
  ExportOutlined,
  MergeCellsOutlined,
  SearchOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  UpOutlined,
} from '@ant-design/icons'
import type { RecordingLive, Segment, Speaker, Transcript } from '../../types'
import { useMergeSpeakers, useUpdateSegment, useUpdateSpeaker, exportUrl } from '../../api/queries'
import { formatMs } from '../../utils/format'
import { toast, toastError } from '../../utils/toast'
import { EmptyState } from '../common/EmptyState'
import { ErrorState } from '../common/ErrorState'
import { TabContentSkeleton } from '../common/Skeletons'

interface TranscriptTabProps {
  recordingId: number
  recordingTitle: string
  transcript: Transcript | undefined
  loading: boolean
  error: unknown
  currentMs: number
  activeSegmentId: number | null
  highlightSegmentId: number | null
  playing: boolean
  onSeek: (ms: number) => void
  onRetry: () => void
  /** Public share page mode: hide editing / renaming / exports. */
  readOnly?: boolean
  /** Live transcript produced during browser recording (R-19). */
  live?: RecordingLive | null
}

function triggerDownload(url: string): void {
  const link = document.createElement('a')
  link.href = url
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  link.remove()
}

function SpeakerRenamePopover({
  speaker,
  disabled,
  onSave,
  saving,
}: {
  speaker: Speaker
  disabled: boolean
  onSave: (name: string) => void
  saving: boolean
}) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState(speaker.display_name || speaker.label)
  const currentName = speaker.display_name || speaker.label

  useEffect(() => {
    setValue(currentName)
  }, [currentName])

  const submit = () => {
    const next = value.trim()
    if (!next) {
      toast.warning('名称不能为空')
      return
    }
    if (next === currentName) {
      setOpen(false)
      return
    }
    onSave(next)
  }

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      placement="bottomLeft"
      title="修改说话人名称"
      content={
        <div className="speaker-popover">
          <Input
            value={value}
            maxLength={20}
            placeholder="输入说话人名称"
            onChange={(event) => setValue(event.target.value)}
            onPressEnter={submit}
            autoFocus
          />
          <div className="speaker-popover__actions">
            <Button size="small" onClick={() => setOpen(false)}>
              取消
            </Button>
            <Button size="small" type="primary" loading={saving} onClick={submit}>
              保存
            </Button>
          </div>
        </div>
      }
    >
      <button
        type="button"
        className="transcript-speaker__name"
        style={{ color: speaker.color || '#1677ff' }}
        disabled={disabled}
      >
        {currentName}
        {!disabled && <EditOutlined className="transcript-speaker__edit" />}
      </button>
    </Popover>
  )
}

/**
 * 转写 tab: speaker-grouped dialogue stream with search, click-to-seek,
 * inline segment editing and speaker renaming.
 */
export function TranscriptTab({
  recordingId,
  recordingTitle,
  transcript,
  loading,
  error,
  currentMs,
  activeSegmentId,
  highlightSegmentId,
  playing,
  onSeek,
  onRetry,
  readOnly = false,
  live = null,
}: TranscriptTabProps) {
  const [query, setQuery] = useState('')
  const [liveOpen, setLiveOpen] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editText, setEditText] = useState('')
  const [speakerModalOpen, setSpeakerModalOpen] = useState(false)
  const [speakerDraft, setSpeakerDraft] = useState<Record<number, string>>({})
  const [mergeOpen, setMergeOpen] = useState(false)
  const [mergeTargetId, setMergeTargetId] = useState<number | null>(null)
  const [mergeSourceIds, setMergeSourceIds] = useState<number[]>([])
  const rowRefs = useRef<Record<number, HTMLDivElement | null>>({})
  const updateSegment = useUpdateSegment(recordingId)
  const updateSpeaker = useUpdateSpeaker(recordingId)
  const updateSpeakerBulk = useUpdateSpeaker(recordingId)
  const mergeSpeakers = useMergeSpeakers(recordingId)

  const speakers = useMemo(() => transcript?.speakers ?? [], [transcript])
  const speakerMap = useMemo(() => new Map(speakers.map((speaker) => [speaker.id, speaker])), [speakers])
  const segments = transcript?.segments ?? []

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const speakerLookup = (id: number | null): string => {
      if (id === null) return ''
      const speaker = speakerMap.get(id)
      return (speaker?.display_name || speaker?.label || '').toLowerCase()
    }
    if (!needle) return segments
    return segments.filter(
      (segment) =>
        segment.text.toLowerCase().includes(needle) || speakerLookup(segment.speaker_id).includes(needle),
    )
  }, [query, segments, speakerMap])

  // Auto-follow the playback cursor while the tab is open.
  useEffect(() => {
    if (!playing || editingId !== null || query.trim()) return
    if (activeSegmentId === null) return
    const node = rowRefs.current[activeSegmentId]
    node?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [activeSegmentId, editingId, playing, query])

  const speakerCount = new Set(segments.map((segment) => segment.speaker_id).filter((id) => id !== null)).size

  const exportItems: MenuProps['items'] = [
    { key: 'txt', label: '导出 TXT 文本' },
    { key: 'srt', label: '导出 SRT 字幕' },
    { key: 'md', label: '导出 Markdown' },
    { key: 'json', label: '导出 JSON 数据' },
  ]

  const startEdit = (segment: Segment) => {
    setEditingId(segment.id)
    setEditText(segment.text)
  }

  const saveEdit = (segmentId: number) => {
    const text = editText.trim()
    if (!text) {
      toast.warning('转录内容不能为空')
      return
    }
    updateSegment.mutate(
      { segmentId, text },
      {
        onSuccess: () => {
          toast.success('转录内容已更新')
          setEditingId(null)
        },
        onError: (mutationError) => toastError(mutationError, '保存失败，请重试'),
      },
    )
  }

  const openSpeakerModal = () => {
    const draft: Record<number, string> = {}
    speakers.forEach((speaker) => {
      draft[speaker.id] = speaker.display_name || speaker.label
    })
    setSpeakerDraft(draft)
    setSpeakerModalOpen(true)
  }

  const saveAllSpeakers = async () => {
    try {
      for (const speaker of speakers) {
        const next = (speakerDraft[speaker.id] ?? '').trim()
        const current = speaker.display_name || speaker.label
        if (next && next !== current) {
          await updateSpeakerBulk.mutateAsync({ speakerId: speaker.id, display_name: next })
        }
      }
      toast.success('说话人信息已更新')
      setSpeakerModalOpen(false)
    } catch (mutationError) {
      toastError(mutationError, '说话人更新失败')
    }
  }

  const openMergeModal = () => {
    setMergeTargetId(speakers[0]?.id ?? null)
    setMergeSourceIds([])
    setMergeOpen(true)
  }

  const submitMerge = () => {
    if (!mergeTargetId) {
      toast.warning('请选择目标说话人')
      return
    }
    const sourceIds = mergeSourceIds.filter((id) => id !== mergeTargetId)
    if (!sourceIds.length) {
      toast.warning('请选择至少一个要合并的说话人')
      return
    }
    mergeSpeakers.mutate(
      { source_ids: sourceIds, target_id: mergeTargetId },
      {
        onSuccess: (result) => {
          toast.success(`已合并 ${sourceIds.length} 个说话人，更新 ${result.merged_segments} 段转录`)
          setMergeOpen(false)
        },
        onError: (mutationError) => toastError(mutationError, '合并说话人失败'),
      },
    )
  }

  if (loading) return <TabContentSkeleton rows={6} />
  if (error) {
    return <ErrorState title="转录加载失败" description="转录稿获取失败，请稍后重试。" onRetry={onRetry} />
  }
  if (!segments.length) {
    return (
      <EmptyState
        icon={<TeamOutlined />}
        title="转录尚未生成"
        description={
          readOnly
            ? '这份分享暂未包含转录内容，或录音仍在处理中。'
            : '处理完成后，这里会显示按说话人分组的逐字稿，支持搜索、编辑、导出与点击跳转播放。'
        }
      />
    )
  }

  return (
    <div className="transcript-tab">
      <div className="transcript-toolbar">
        <div className="transcript-toolbar__left">
          <Input
            allowClear
            className="transcript-search"
            prefix={<SearchOutlined className="muted-icon" />}
            placeholder="搜索转录内容或说话人"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <span className="transcript-toolbar__meta">
            {query.trim() ? `匹配 ${filtered.length} / ${segments.length} 段` : `共 ${segments.length} 段 · ${speakerCount} 位发言人`}
          </span>
        </div>
        {!readOnly && (
          <div className="transcript-toolbar__right">
            <Button icon={<TeamOutlined />} onClick={openSpeakerModal}>
              说话人
            </Button>
            <Tooltip title={speakers.length < 2 ? '至少需要 2 位说话人才能合并' : '把被误识别的多个说话人合并为一位'}>
              <Button icon={<MergeCellsOutlined />} disabled={speakers.length < 2} onClick={openMergeModal}>
                合并说话人
              </Button>
            </Tooltip>
            <Dropdown
              menu={{
                items: exportItems,
                onClick: ({ key }) => triggerDownload(exportUrl(recordingId, key as 'txt' | 'srt' | 'md' | 'json')),
              }}
              trigger={['click']}
            >
              <Button icon={<DownloadOutlined />}>导出</Button>
            </Dropdown>
          </div>
        )}
      </div>

      {live && live.segments?.length > 0 && (
        <div className={`live-transcript ${liveOpen ? 'is-open' : ''}`}>
          <button
            type="button"
            className="live-transcript__toggle"
            aria-expanded={liveOpen}
            onClick={() => setLiveOpen((value) => !value)}
          >
            <span className="live-transcript__title">
              <ThunderboltOutlined /> 实时稿
              <span className="live-transcript__sub">实时字幕 · 未区分说话人</span>
            </span>
            <span className="live-transcript__meta">
              {live.segments.length} 段 · {live.session.provider || 'live'}
              {live.session.total_audio_ms ? ` · ${formatMs(live.session.total_audio_ms)}` : ''}
            </span>
            {liveOpen ? <UpOutlined /> : <DownOutlined />}
          </button>
          {liveOpen && (
            <div className="live-transcript__body">
              <div className="live-transcript__note">
                实时稿由录音过程中的实时字幕生成，可能存在错漏，且不区分说话人；最终请以离线转写为准。
              </div>
              {live.segments.map((segment) => (
                <div className="live-transcript__segment" key={segment.id}>
                  <button type="button" className="live-transcript__time" onClick={() => onSeek(segment.start_ms)}>
                    {formatMs(segment.start_ms)}
                  </button>
                  <span className="live-transcript__text">{segment.text}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="transcript-stream">
        {filtered.map((segment, index) => {
          const speaker = segment.speaker_id !== null ? speakerMap.get(segment.speaker_id) : undefined
          const displayName = speaker?.display_name || speaker?.label || '未识别说话人'
          const color = speaker?.color || '#8f959e'
          const previous = index > 0 ? filtered[index - 1] : null
          const showSpeaker = !previous || previous.speaker_id !== segment.speaker_id
          const isActive = segment.id === activeSegmentId && !query.trim()
          const isEditing = segment.id === editingId
          const isHighlighted = segment.id === highlightSegmentId

          return (
            <div
              key={segment.id}
              ref={(element) => {
                rowRefs.current[segment.id] = element
              }}
              className={`transcript-segment ${isActive ? 'is-active' : ''} ${isHighlighted ? 'is-highlighted' : ''} ${
                isEditing ? 'is-editing' : ''
              }`}
            >
              <div className="transcript-segment__gutter">
                <span className="transcript-segment__avatar" style={{ background: color }}>
                  {showSpeaker ? displayName.slice(0, 1) : ''}
                </span>
              </div>
              <div className="transcript-segment__body">
                {showSpeaker && (
                  <div className="transcript-segment__head">
                    {readOnly ? (
                      <span className="transcript-speaker__name transcript-speaker__name--static" style={{ color }}>
                        {displayName}
                      </span>
                    ) : (
                      <SpeakerRenamePopover
                        speaker={
                          speaker ?? {
                            id: -1,
                            label: displayName,
                            display_name: displayName,
                            color,
                            is_auto: true,
                          }
                        }
                        disabled={!speaker || recordingId <= 0}
                        saving={updateSpeaker.isPending}
                        onSave={(name) => {
                          if (!speaker) return
                          updateSpeaker.mutate(
                            { speakerId: speaker.id, display_name: name },
                            {
                              onSuccess: () => {
                                toast.success('说话人名称已更新')
                              },
                              onError: (mutationError) => toastError(mutationError, '更新说话人失败'),
                            },
                          )
                        }}
                      />
                    )}
                    <Tooltip title="点击时间戳跳转播放">
                      <button type="button" className="transcript-segment__time" onClick={() => onSeek(segment.start_ms)}>
                        {formatMs(segment.start_ms)}
                      </button>
                    </Tooltip>
                  </div>
                )}
                {isEditing ? (
                  <div className="transcript-editor">
                    <Input.TextArea
                      value={editText}
                      autoSize={{ minRows: 2, maxRows: 8 }}
                      onChange={(event) => setEditText(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Escape') setEditingId(null)
                        if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                          event.preventDefault()
                          saveEdit(segment.id)
                        }
                      }}
                      autoFocus
                    />
                    <div className="transcript-editor__actions">
                      <Button size="small" icon={<CloseOutlined />} onClick={() => setEditingId(null)}>
                        取消
                      </Button>
                      <Button
                        size="small"
                        type="primary"
                        icon={<CheckOutlined />}
                        loading={updateSegment.isPending}
                        onClick={() => saveEdit(segment.id)}
                      >
                        保存
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="transcript-segment__text-wrap">
                    <p className="transcript-segment__text" onClick={() => onSeek(segment.start_ms)}>
                      {segment.text}
                    </p>
                    {!readOnly && (
                      <button
                        type="button"
                        className="transcript-segment__edit"
                        aria-label="编辑这段转录"
                        onClick={() => startEdit(segment)}
                      >
                        <EditOutlined />
                      </button>
                    )}
                  </div>
                )}
                {!showSpeaker && (
                  <button type="button" className="transcript-segment__inline-time" onClick={() => onSeek(segment.start_ms)}>
                    {formatMs(segment.start_ms)}
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {filtered.length === 0 && (
        <EmptyState
          variant="plain"
          compact
          title={`没有找到包含「${query.trim()}」的转录内容`}
          actions={
            <Button size="small" onClick={() => setQuery('')}>
              清除搜索
            </Button>
          }
        />
      )}

      {!readOnly && (
        <div className="transcript-footer">
          <ExportOutlined /> 导出文件名：{recordingTitle || '转录稿'}.txt
        </div>
      )}

      <Modal
        open={speakerModalOpen}
        title="管理说话人"
        onCancel={() => setSpeakerModalOpen(false)}
        onOk={() => void saveAllSpeakers()}
        okText="保存"
        cancelText="取消"
        confirmLoading={updateSpeakerBulk.isPending}
      >
        <p className="muted-text">修改后会同步更新所有转录片段中的说话人名称。</p>
        <div className="speaker-manager">
          {speakers.map((speaker) => (
            <div className="speaker-manager__row" key={speaker.id}>
              <span className="speaker-manager__dot" style={{ background: speaker.color }} />
              <span className="speaker-manager__label">{speaker.label}</span>
              <Input
                value={speakerDraft[speaker.id] ?? ''}
                maxLength={20}
                onChange={(event) =>
                  setSpeakerDraft((prev) => ({ ...prev, [speaker.id]: event.target.value }))
                }
              />
            </div>
          ))}
        </div>
      </Modal>

      <Modal
        open={mergeOpen}
        title="合并说话人"
        onCancel={() => setMergeOpen(false)}
        onOk={submitMerge}
        okText="确认合并"
        cancelText="取消"
        confirmLoading={mergeSpeakers.isPending}
        destroyOnClose
      >
        <p className="muted-text">
          当同一个人被识别成多个说话人时，选择一个目标说话人，把其他说话人合并进去。所有转录片段会归属目标说话人，
          来源说话人将被删除；转录文字与时间戳不变。AI 纪要/观点树若需要按新说话人重算，可在详情页「重新生成 AI 分析」。
        </p>
        <div className="speaker-merge">
          <label className="speaker-merge__field">
            <span>目标说话人（合并后保留）</span>
            <Select
              value={mergeTargetId ?? undefined}
              placeholder="选择目标说话人"
              options={speakers.map((speaker) => ({
                value: speaker.id,
                label: `${speaker.display_name || speaker.label}（${speaker.label}）`,
              }))}
              onChange={(value) => {
                setMergeTargetId(value)
                setMergeSourceIds((prev) => prev.filter((id) => id !== value))
              }}
            />
          </label>
          <label className="speaker-merge__field">
            <span>要合并到目标的说话人（可多选）</span>
            <Select
              mode="multiple"
              allowClear
              value={mergeSourceIds}
              placeholder="选择被误识别的说话人"
              options={speakers
                .filter((speaker) => speaker.id !== mergeTargetId)
                .map((speaker) => ({
                  value: speaker.id,
                  label: `${speaker.display_name || speaker.label}（${speaker.label}）`,
                }))}
              onChange={(values) => setMergeSourceIds(values)}
            />
          </label>
        </div>
      </Modal>
    </div>
  )
}
