# 阿里云百炼「说话人分离」转录模型调研（2026-10-27）

> 调研对象：阿里云百炼（Model Studio / DashScope）语音识别模型，分别覆盖**实时（WebSocket）**与**非实时 / 录音文件（HTTP 异步）**两条链路。
> 结论用于 Echoic 的 ASR 选型与配置建议；官方页面可能随时更新，数字/模型名以文末来源为准。

## 0. 一句话结论

- **实时链路目前没有任何模型支持说话人分离**。官方选型表里所有实时 ASR 模型的「说话人分离」列均为「不支持」；实时稿只能按单说话人处理。
- **说话人分离只在“非实时 / 录音文件识别”链路提供**，推荐 `qwen-audio-3.1-asr-flash-filetrans`（会议场景）；性价比/旧代模型 `paraformer-v2` 也支持；`fun-asr` 同样支持。
- 需要「谁说了什么」的会议转写，**录音必须走文件转写**；实时字幕只能作为边录边看的参考，最终稿应由文件转写生成（Echoic 现有 `replace` 合并策略正是这个设计）。

## 1. 实时模型：全部不支持说话人分离

来源：百炼「语音识别概述」的模型总表（2026-10 页面）。

| 模型 ID | 模式 | API | 说话人分离 | 备注 |
|---|---|---|---|---|
| `qwen-audio-3.1-asr-flash-message` | 实时 | WebSocket | ❌ | 输入法/短语音消息，延迟低 |
| `qwen-audio-3.1-asr-flash-streaming` | 实时 | WebSocket | ❌ | 持续转写场景，官方推荐用于会议/课堂的实时字幕 |
| `qwen-audio-3.0-asr-flash-streaming` | 实时 | WebSocket | ❌ | 上一代 streaming |
| `fun-asr-realtime` / `fun-asr-realtime-2026-02-28` | 实时 | WebSocket | ❌ | 支持热词 |
| `fun-asr-flash-8k-realtime` / `-2026-01-28` | 实时 | WebSocket | ❌ | 8kHz 电话场景 |
| `qwen3-asr-flash-realtime` | 实时 | WebSocket | ❌ | 支持**情感识别**，但不支持说话人分离 |
| `paraformer-realtime-v2` | 实时 | WebSocket | ❌ | 百炼老一代实时模型，Echoic `dashscope_realtime` 默认 |
| `paraformer-realtime-8k-v2` | 实时 | WebSocket | ❌ | 8kHz 电话场景 |

**工程结论**：如果产品需要“实时字幕 + 实时区分说话人”，百炼当前没有可直接调用的原生能力；需在客户端/服务端另做声道隔离（每人一条音轨）或离线 diarization 方案。Echoic 的实时转写因此明确标注为 Beta、不区分说话人。

## 2. 非实时 / 文件转写：支持说话人分离的模型

来源：百炼「语音识别概述」模型总表 + 「Qwen-Audio-3.x-ASR-Flash-Filetrans/Fun-ASR HTTP API」参数说明。

| 模型 ID | API | 说话人分离 | 最大时长/大小 | 说明 |
|---|---|---|---|---|
| `qwen-audio-3.1-asr-flash-filetrans` | 异步 HTTP | ✅ | 12 小时 / 2GB | **官方会议场景首选**；支持热词、Prompt 上下文、方言保留；开分离建议 ≤2 小时 |
| `qwen-audio-3.0-asr-flash-filetrans` | 异步 HTTP | ✅ | 12 小时 / 2GB | 上一代，仍可用 |
| `fun-asr` | 异步 HTTP | ✅ | 12 小时 / 2GB | 热词；`language_hints` 只取第 1 个 |
| `paraformer-v2` | 异步 HTTP | ✅ | 12 小时 / 2GB | 老一代、便宜；Echoic 默认文件模型；开分离建议 ≤2 小时 |
| `qwen-audio-3.1-asr-flash` | 同步 HTTP | ✅ | 5 分钟 / 2GB | 短音频同步接口；Echoic 未接入该同步形态 |
| `qwen-audio-3.0-asr-flash` | 同步 HTTP | ❌ | 5 分钟 / 2GB | 同系列旧版，不支持 |
| `fun-asr-flash-2026-06-15` | 同步 HTTP | ❌ | 5 分钟 / 2GB | Fun-ASR 的 Flash 同步版，不支持 |
| `qwen3-asr-flash-filetrans` | 异步 HTTP | ❌ | 12 小时 / 2GB | 支持情感识别，但**不支持**分离 |
| `qwen3-asr-flash` | 同步 OpenAI 兼容 | ❌ | 5 分钟 / 10MB | 支持情感识别，但无时间戳/无分离 |
| `paraformer-8k-v2` | 异步 HTTP | 官方未单列（同系列） | 12 小时 / 2GB | 8kHz 电话音；是否支持建议实测 |

> 说明：`paraformer-v2` 的官方 API 参数包含 `diarization_enabled` 与 `speaker_count`；`paraformer-8k-v2` 与它同页但官方只对 `paraformer-v2` 明确写了“支持说话人分离”。

## 3. 文件转写如何开启说话人分离（参数/返回）

以 `qwen-audio-3.1-asr-flash-filetrans` / `fun-asr` / `paraformer-v2` 的异步接口为例（端点同为 `POST /api/v1/services/audio/asr/transcription`）：

```json
{
  "model": "qwen-audio-3.1-asr-flash-filetrans",
  "input": { "file_urls": ["oss://..."] },
  "parameters": {
    "channel_id": [0],
    "diarization_enabled": true,
    "speaker_count": 2,
    "language_hints": ["zh", "en"]
  }
}
```

关键点：

- `diarization_enabled: true`：**默认 false**，开启后才返回 `speaker_id`。
- `speaker_count`：可选，2–100 的参考人数；不填则自动检测，填了也只是引导，不保证精确人数。
- **仅单声道**支持说话人分离；多声道文件不支持（多声道可按 `channel_id` 逐轨识别，但那是“声道=说话人”的另一套方案，逐轨分别计费）。
- 开启说话人分离时官方建议**单文件 ≤2 小时**，否则可能失败或超时；超长会议建议拆分文件，或关闭分离只做普通转写。
- 结果 JSON：`transcripts[].sentences[]` 每条包含 `begin_time` / `end_time` / `text` / `speaker_id`（0 开始）；Echoic 会把 `speaker_id` 映射为“说话人 1/2/…”。
- Qwen-Audio-3.x Filetrans 必须传 `parameters`（即使为空对象 `{}`）；缺少可能导致任务提交成功但识别失败。
- 异步流程：`X-DashScope-Async: enable` 提交 → `GET /api/v1/tasks/{task_id}` 轮询 → 下载 `transcription_url`（默认 24h 有效）。
- 地域：华北2（北京）与新加坡均提供业务空间专属域名；旧 `dashscope.aliyuncs.com` / `dashscope-intl.aliyuncs.com` 仍可用。
- 语言：Qwen-Audio Filetrans 的 `language_hints` 最多 4 个；Fun-ASR 只取第 1 个。

## 4. Echoic 现状与建议配置

Echoic v0.3.x 的 `dashscope_file` Provider 已经：

- 通过百炼临时上传凭证把本地文件转为 `oss://` URL（**不需要公网可访问地址**）；
- 调用异步任务并轮询、下载结果；
- 在 `diarization=true` 时发送 `diarization_enabled`；
- 解析 `sentences[].speaker_id` 并落库为说话人。

因此要获得说话人分离，**只需把 ASR 设置为文件转写并选择支持分离的模型**，无需改代码：

```env
ECHOIC_ASR_PROVIDER=dashscope_file
ECHOIC_ASR_API_KEY=sk-...
ECHOIC_ASR_BASE_URL=https://dashscope.aliyuncs.com
# 推荐（会议、说话人分离）：qwen-audio-3.1-asr-flash-filetrans
# 性价比/旧代：paraformer-v2
ECHOIC_ASR_MODEL=qwen-audio-3.1-asr-flash-filetrans
ECHOIC_ASR_DIARIZATION=true
```

实时字幕单独开启（不产生说话人）：

```env
ECHOIC_LIVE_ENABLED=true
ECHOIC_LIVE_PROVIDER=dashscope_realtime   # 或 auto
```

注意：

- 若当前把 `dashscope_realtime` 当作上传录音的 ASR，**整场文件不会得到说话人分离**；这是模型能力限制，不是 Echoic bug。请改为 `dashscope_file`。
- 录音页的实时字幕仍可用（单说话人）；处理完成后最终转录以离线文件转写为准（Echoic 默认 `ECHOIC_LIVE_MERGE_MODE=replace`）。
- 长会议 >2 小时：文件转写仍可跑 12 小时，但说话人分离官方建议 ≤2 小时；Echoic 目前不会自动按 2 小时切片。如经常有超长会议，可规划“切片 + 合并”或关闭分离。
- 若已知参会人数，可后续在 Echoic 增加 `speaker_count` 设置（当前未暴露，自动检测；Provider 已支持该参数位置，增加配置即可）。
- 实时模型 `qwen-audio-3.1-asr-flash-streaming` 可作为实时字幕升级项，但它同样**不支持**说话人分离。

## 5. 风险与待验证

- 本次调研基于官方文档（2026-10-27），未用真实 Key 逐一跑全模型；建议上线前用 2–3 人真实会议分别验证 `qwen-audio-3.1-asr-flash-filetrans`、`paraformer-v2`、`fun-asr` 的分离质量与延迟。
- 价格：各模型按音频秒数计费，Qwen-Audio 与 Fun-ASR 单价高于老 Paraformer；正式使用前以百炼控制台价格为准。多声道 `channel_id` 逐轨分别计费。
- 说话人分离的准确率受重叠说话、远场噪声、人数估计影响；`speaker_count` 只是参考值。
- 结果 JSON 的下载 URL 24 小时有效；Echoic 已在任务完成后立即下载落盘。
- 实时链路未来是否会上线分离能力，需持续关注百炼「实时语音识别」更新。

## 6. 官方来源

- 语音识别概述（模型总表、说话人分离列）：https://help.aliyun.com/zh/model-studio/asr-model
- 非实时语音识别用户指南：https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide
- Qwen-Audio-3.x-ASR-Flash-Filetrans / Fun-ASR HTTP API（diarization 参数、结果 `speaker_id`）：https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api
- Paraformer 非实时 HTTP API（`diarization_enabled` / `speaker_count`）：https://help.aliyun.com/zh/model-studio/paraformer-recorded-speech-recognition-restful-api
- 实时语音识别接入指南 / 实时模型 API：https://help.aliyun.com/zh/model-studio/websocket-for-paraformer-real-time-service
- 实时语音识别用户指南：https://help.aliyun.com/zh/model-studio/real-time-speech-recognition-user-guide
