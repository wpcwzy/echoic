# DashScope / 百炼 API 速查（Echoic 会议转写与 LLM）

> 说话人分离（实时 vs 文件转写）的专项调研见 [bailian-diarization.md](./bailian-diarization.md)（2026-10-27）。


> 核查日期 2026-10-26。所有 URL 以官方文档为准；标 **待验证** 的条目未在官方文档中找到明确说明，勿直接写死。
> 关键结论：**DashScope 兼容模式没有 `/v1/audio/transcriptions`**（实测 404，见 §3），录音文件识别只吃公网 URL。

## 0. 结论先行（私有部署）

| 需求 | 推荐路径 | 是否需公网 URL | 时间戳 | 说话人分离 |
|---|---|---|---|---|
| 整场会议（上传后转写） | 录音文件识别 + 百炼临时上传换 `oss://` | 否（用 `oss://`） | 句/词级 | ✅（paraformer-v2 / fun-asr / qwen-audio-filetrans） |
| 边录边转 | `paraformer-realtime-v2` WebSocket | 否 | 句/词级 | ❌ |
| 短片段兜底 | `qwen3-asr-flash`（chat/completions + base64） | 否 | ❌ | ❌ |
| 完全私有 | 自建 faster-whisper/FunASR 的 OpenAI 兼容端点 | 否 | ✅ verbose_json | 取决于服务端 |

## 1. 认证、域名与地域

- 鉴权统一 `Authorization: Bearer $DASHSCOPE_API_KEY`；WebSocket 在握手头里带同一值。API Key **按地域绑定**，跨地域调用返回 401 `invalid_api_key`（[兼容模式文档](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)）。
- 新专属域名（推荐）：北京 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com`，新加坡 `...ap-southeast-1...`，弗吉尼亚 `...us-east-1...`。旧域名 `https://dashscope.aliyuncs.com` / `https://dashscope-intl.aliyuncs.com` **仍可用**（同上文档）。Echoic 把 `base_url` 做成配置项，默认旧域名。

## 2. 录音文件识别（异步：提交 → 轮询 → 下载结果）

接口（[Paraformer RESTful](https://help.aliyun.com/zh/model-studio/paraformer-recorded-speech-recognition-restful-api)、[Fun-ASR/Qwen-Audio HTTP](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api)）：

```http
POST {BASE}/api/v1/services/audio/asr/transcription
Authorization: Bearer $KEY
Content-Type: application/json
X-DashScope-Async: enable          # 漏了任务无法提交，必须带

{"model":"paraformer-v2",
 "input":{"file_urls":["https://.../a.wav"]},
 "parameters":{"channel_id":[0],"language_hints":["zh","en"],
               "diarization_enabled":true,"speaker_count":2,
               "disfluency_removal_enabled":false,"timestamp_alignment_enabled":false}}
→ {"output":{"task_status":"PENDING","task_id":"..."},"request_id":"..."}
```

查询 `GET {BASE}/api/v1/tasks/{task_id}`（新版文档用 GET；Paraformer 旧文档写 POST，**两者都可能可用，待验证**，建议实现 GET + 一次 POST 兜底）。返回：

```json
{"output":{"task_id":"...","task_status":"SUCCEEDED",
 "results":[{"file_url":"...","subtask_status":"SUCCEEDED",
             "transcription_url":"https://dashscope-result-bj.oss.../x.json?Expires=...",
             "code":"...","message":"..."}],
 "task_metrics":{"TOTAL":1,"SUCCEEDED":1,"FAILED":0}},
 "usage":{"duration":9}}
```

`transcription_url` 是**另一个 JSON 文件，24 小时有效**，必须立刻下载落盘。其结构：

```json
{"file_url":"...","properties":{"audio_format":"pcm_s16le","channels":[0],
  "original_sampling_rate":16000,"original_duration_in_milliseconds":3834},
 "transcripts":[{"channel_id":0,"content_duration_in_milliseconds":3720,"text":"...",
   "sentences":[{"begin_time":100,"end_time":3820,"text":"...","sentence_id":1,
     "speaker_id":0,                       // 仅 diarization_enabled=true 时出现
     "words":[{"begin_time":100,"end_time":596,"text":"Hello ","punctuation":""}]}]}]}
```

要点：
- `file_urls`：HTTP/HTTPS，**单次只允许 1 个 URL**；不支持 base64、不支持二进制流、不支持本地文件（Paraformer 文档 FAQ 明确说明）。OSS 对象可用 `oss://` 前缀（REST 支持，SDK 不支持自有 OSS 的 `oss://`）。URL 含空格/中文需先 URL 编码。
- 失败子任务不抛异常：整体可能 `SUCCEEDED`，必须逐条看 `subtask_status`（`InvalidFile.DownloadFailed` 很常见）。
- `channel_id` 多轨**分别计费**；`diarization_enabled` 只支持单声道，建议 ≤2 小时否则易超时；`speaker_count` 2–100，只是参考值。
- 时间戳单位 **ms**，句级 + 词级（paraformer-v2 可开 `timestamp_alignment_enabled` 校准）。
- 其他参数：`vocabulary_id`（热词）、`special_word_filter`（敏感词，JSON 字符串）、`context`（仅 Qwen-Audio 系列，提升专名召回）。

### 2.1 没有公网 URL 怎么喂文件（关键）

百炼提供**临时上传凭证**接口，只用 API Key 即可把本地文件传到百炼托管 OSS，拿回 `oss://...` 再当 `file_urls` 用。Python SDK 的 `OssUtils` 就是这样做的（`dashscope 2.0.1` 源码 `dashscope/utils/oss_utils.py`）：`GET {BASE}/api/v1/uploads?action=getPolicy&model=paraformer-v2` → 返回 `policy/signature/upload_host/upload_dir/oss_access_key_id/x_oss_object_acl/x_oss_forbid_overwrite`，再 multipart POST 到 `upload_host`，成功即 `oss://{upload_dir}/{filename}`。

```python
import time
import httpx

def upload_for_asr(path: str, model="paraformer-v2", base=BASE, key=KEY) -> str:
    h = {"Authorization": f"Bearer {key}"}
    pol = httpx.get(f"{base}/api/v1/uploads",
                    params={"action": "getPolicy", "model": model},
                    headers=h, timeout=30).json()["output"]
    name = path.rsplit("/", 1)[-1]
    key_in_oss = f'{pol["upload_dir"]}/{name}'
    form = {"OSSAccessKeyId": pol["oss_access_key_id"], "Signature": pol["signature"],
            "policy": pol["policy"], "key": key_in_oss,
            "x-oss-object-acl": pol["x_oss_object_acl"],
            "x-oss-forbid-overwrite": pol["x_oss_forbid_overwrite"],
            "success_action_status": "200", "x-oss-content-type": "audio/wav"}
    with open(path, "rb") as f:
        httpx.post(pol["upload_host"], data=form, files={"file": f}, timeout=600).raise_for_status()
    return f"oss://{key_in_oss}"      # 官方：临时 URL 有效期 48h，勿用于生产高并发

def transcribe(url: str, base=BASE, key=KEY) -> dict:
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "X-DashScope-Async": "enable"}
    body = {"model": "paraformer-v2", "input": {"file_urls": [url]},
            "parameters": {"channel_id": [0], "diarization_enabled": True}}
    tid = httpx.post(f"{base}/api/v1/services/audio/asr/transcription",
                     headers=h, json=body, timeout=60).json()["output"]["task_id"]
    while True:
        out = httpx.get(f"{base}/api/v1/tasks/{tid}", headers=h, timeout=60).json()["output"]
        if out["task_status"] in ("SUCCEEDED", "FAILED", "UNKNOWN"):
            break
        time.sleep(2)                                    # 查询限流 20 QPS
    for r in out["results"]:
        if r["subtask_status"] == "SUCCEEDED":
            return httpx.get(r["transcription_url"], timeout=120).json()
    raise RuntimeError(out)
```

### 2.2 模型与限制（[语音识别概述](https://help.aliyun.com/zh/model-studio/asr-model)）

| 模型 | 输入 | 上限 | 说话人分离 | 备注 |
|---|---|---|---|---|
| `paraformer-v2` / `paraformer-8k-v2` | 公网 URL | 12h / 2GB | ✅（单声道） | 旧代，便宜（0.00008 元/秒） |
| `fun-asr` | 公网 URL | 12h / 2GB | ✅ | 0.00022 元/秒，支持热词/上下文 |
| `qwen-audio-3.1-asr-flash-filetrans` | 公网 URL | 12h / 2GB | ✅ | 官方会议场景首选 |
| `qwen3-asr-flash-filetrans` | 公网 URL（`input.file_url` 单数！） | 12h / 2GB | ❌ | 参数 `enable_itn`/`enable_words` |
| `qwen3-asr-flash` | OpenAI 兼容 chat，URL/Base64 | **5 min / 10MB** | ❌ | 无时间戳 |
| `qwen-audio-3.1-asr-flash` | URL / Base64 | 5 min / 2GB | ✅ | 同步接口，**API 细节待验证** |

格式：`aac/amr/avi/flac/flv/m4a/mkv/mov/mp3/mp4/mpeg/ogg/opus/wav/webm/wma/wmv`（paraformer 同）。`language_hints`：`zh/en/ja/yue/ko/de/fr/ru`（Qwen-Audio 最多 4 个，Fun-ASR 只取第 1 个）。

## 3. Qwen ASR（2025/2026）：没有 `/v1/audio/transcriptions`

官方 [Qwen-ASR API 文档](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference) 只给两种调用：**OpenAI 兼容的 `chat/completions`** 和 **DashScope 同步 `multimodal-generation`**。我实测 `POST https://dashscope.aliyuncs.com/compatible-mode/v1/audio/transcriptions` 返回 **404**，而 `/embeddings`、`/chat/completions` 返回 401 —— 证明该端点不存在。

```http
POST {BASE}/compatible-mode/v1/chat/completions
{"model":"qwen3-asr-flash",
 "messages":[{"role":"user","content":[
    {"type":"input_audio","input_audio":{"data":"data:audio/mpeg;base64,<...>"}}]}],
 "asr_options":{"language":"zh","enable_itn":true}}     # 非标准字段，SDK 走 extra_body
```

- 响应就是 chat completion，`choices[0].message.content` 为文本，`annotations` 里带 `language`/`emotion`；**无时间戳**。
- 输入：公网 URL 或 Data URL（`data:audio/wav;base64,...`），≤5 分钟、≤10MB，单声道。
- `qwen3-asr-flash-filetrans` 走异步任务（见 §2），结果 JSON 有 `sentences[].words[]`（需 `enable_words:true`），但**无 `speaker_id`**。
- 结论：会议场景不要用它做主力（切段 + 无说话人 + 无时间戳）。仅适合"短语音消息/兜底"。

## 4. 实时语音识别 `paraformer-realtime-v2`（WebSocket）

- URL：`wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference`（旧 `wss://dashscope.aliyuncs.com/api-ws/v1/inference`），仅华北2（北京）可用。头部：`Authorization: Bearer $KEY`，可选 `X-DashScope-WorkSpace`。握手失败即 401/403（[WS 接入指南](https://help.aliyun.com/zh/model-studio/websocket-for-paraformer-real-time-service)）。
- 协议：建连 → 发 `run-task` → 收 `task-started` → **持续发二进制音频帧**（单声道 PCM，`format=pcm`、`sample_rate=16000`）→ 收 `result-generated` → 发 `finish-task` → 收 `task-finished`。（[客户端事件](https://help.aliyun.com/zh/model-studio/paraformer-client-events)）
- 本文档未出现 `continue-task`（那是其他 Realtime 模型的指令）；Paraformer 音频走二进制帧，不要包 JSON。
- 结果事件（[服务端事件](https://help.aliyun.com/zh/model-studio/paraformer-server-events)）：

```json
{"header":{"task_id":"...","event":"result-generated"},
 "payload":{"output":{"sentence":{"begin_time":170,"end_time":920,"text":"好，我知道了",
   "sentence_end":true,"heartbeat":false,
   "words":[{"begin_time":170,"end_time":295,"text":"好","punctuation":"，"}]}},
  "usage":{"duration":3}}}
```

`sentence_end=false` 是中间结果（要覆盖 UI 最后一行），`true` 才落库；`heartbeat=true` 丢弃。**没有 `speaker_id`** → 实时链路做不了说话人分离（官方选型表也标注实时模型"不支持"）。
- 生命周期：任务结束后 60s 无新任务连接自动断开；同一连接可复用，但必须换新 `task_id`；`task-failed` 会关连接且不可复用；长静音建议 `heartbeat:true`（[实时识别指南](https://help.aliyun.com/zh/model-studio/real-time-speech-recognition-user-guide)）。
- 参数：`semantic_punctuation_enabled`（会议建议 true）、`max_sentence_silence`（默认 1300ms）、`punctuation_prediction_enabled`、`disfluency_removal_enabled`、`inverse_text_normalization_enabled`。

```python
import asyncio, json, uuid, wave, websockets

WS = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

async def stream_wav(path: str, key: str):
    wf = wave.open(path, "rb")
    assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 16000)
    tid = str(uuid.uuid4())

    async with websockets.connect(WS, additional_headers={"Authorization": f"Bearer {key}"},
                                  max_size=None, ping_interval=20) as ws:
        await ws.send(json.dumps({"header": {"action": "run-task", "task_id": tid, "streaming": "duplex"},
            "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                        "model": "paraformer-realtime-v2", "input": {},
                        "parameters": {"format": "pcm", "sample_rate": 16000,
                                       "language_hints": ["zh"], "semantic_punctuation_enabled": True}}}))
        msg = json.loads(await ws.recv())
        assert msg["header"]["event"] == "task-started", msg

        async def reader():                     # 后台收结果
            async for raw in ws:
                ev = json.loads(raw)
                kind = ev["header"]["event"]
                if kind == "result-generated":
                    s = ev["payload"]["output"]["sentence"]
                    if not s.get("heartbeat"):
                        handle(s["text"], s["begin_time"], s["sentence_end"])
                elif kind == "task-finished":
                    return
                elif kind == "task-failed":
                    raise RuntimeError(ev["header"].get("error_message"))
        task = asyncio.create_task(reader())

        while chunk := wf.readframes(1600):     # 100ms @16k
            await ws.send(chunk)
            await asyncio.sleep(0.1)            # 实时节奏；离线文件可去掉
        await ws.send(json.dumps({"header": {"action": "finish-task", "task_id": tid,
                                             "streaming": "duplex"}, "payload": {"input": {}}}))
        await asyncio.wait_for(task, timeout=60)
```

## 5. LLM：DashScope OpenAI 兼容模式

- Base URL：`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`；旧 `https://dashscope.aliyuncs.com/compatible-mode/v1` 仍可用（实测返回 401 而非 404）。`POST /chat/completions`，SSE 为 `data: {...}` + `data: [DONE]`（[兼容模式](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)）。
- 当前模型（[文本生成模型](https://help.aliyun.com/zh/model-studio/text-generation-model)）：`qwen3.8-max`、`qwen3.7-plus`、`qwen3.8-flash`、`qwen3.7-flash`；旧版 `qwen-plus`（= qwen-plus-2025-12-01）、`qwen-max`、`qwen-flash` 仍列在"旧版 Qwen"；`qwen-turbo` 只在结构化输出支持列表里出现，**是否可调待验证**。Echoic 默认 `qwen3.7-plus`，暴露 `model` 配置项，不要硬编码。
- 结构化输出（[结构化输出文档](https://help.aliyun.com/zh/model-studio/qwen-structured-output)）：
  - `response_format={"type":"json_object"}`：**messages 必须含 "JSON" 字样**，否则报错；键名/类型不保证。
  - `response_format={"type":"json_schema","json_schema":{"name":"meeting","strict":true,"schema":{...}}}`：严格按 schema，**仅 Qwen3.7-Plus / 3.8-Flash / 3.7-Flash / 3.7-Max / 3.8-Max 系列**。
  - 思考模式下 `json_object` 可能失效 → 会议纪要建议 `json_schema` 或关闭 thinking。
- Function calling：`tools` 支持；旧文档称 "tools 暂时无法与 stream=True 同时使用"，**待验证**（新模型应已支持）。

```python
import httpx, json
r = httpx.post(f"{BASE}/compatible-mode/v1/chat/completions",
    headers={"Authorization": f"Bearer {KEY}"}, timeout=120,
    json={"model": "qwen3.7-plus",
          "messages": [{"role": "system", "content": "你是会议纪要助手，只输出 JSON。"},
                       {"role": "user", "content": transcript}],
          "response_format": {"type": "json_object"},
          "stream": False})
data = json.loads(r.json()["choices"][0]["message"]["content"])
```

### 5.1 Embeddings

| 通道 | 端点 | 关键参数 |
|---|---|---|
| OpenAI 兼容 | `POST {BASE}/compatible-mode/v1/embeddings` | `{"model","input","dimensions","encoding_format":"float"}` |
| DashScope 原生 | `POST {BASE}/api/v1/services/embeddings/text-embedding/text-embedding` | `{"model","input","parameters":{"dimension","text_type","output_type"}}` |

- 模型（[同步接口文档](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)）：`text-embedding-v4`（2048/1536/1024/768/512/256/128/64，单次 ≤10 条，8192 token/条）、`text-embedding-v3`（1024…64）、`qwen3.7-text-embedding`（2560…256，≤20 条，128k token/条）。
- 注意参数名不一致：兼容模式 `dimensions`，原生 `parameters.dimension`。
- 响应：`{"data":[{"embedding":[...],"index":0,"object":"embedding"}],"model":"...","usage":{"total_tokens":184}}`。
- 检索建议 `text_type="query"`/`"document"`（原生参数，仅 DashScope 通道）。

## 6. 通用 OpenAI Whisper 兼容端点（自建 faster-whisper / FunASR）

`POST {base}/v1/audio/transcriptions`，`multipart/form-data`，`Authorization: Bearer <optional>`（[OpenAI 参考](https://platform.openai.com/docs/api-reference/audio/createTranscription)）。

| 字段 | 必填 | 说明 |
|---|---|---|
| `file` | ✅ | 文件对象（flac/mp3/mp4/mpeg/mpga/m4a/ogg/wav/webm），**不是路径字符串** |
| `model` | ✅ | `whisper-1` / `gpt-4o-transcribe` / 自建服务自定义名 |
| `language` | | ISO-639-1，提升准确率与延迟 |
| `prompt` | | 术语提示（会议专名很有用） |
| `response_format` | | `json`(默认)/`text`/`srt`/`vtt`/`verbose_json`/`diarized_json` |
| `temperature` | | 0–1 |
| `timestamp_granularities[]` | | `word`/`segment`，**必须配 `verbose_json`** |

`verbose_json` 形状（Echoic 只需 `text`/`duration`/`segments`；`words` 要显式请求 `timestamp_granularities[]=word`）：

```json
{"task":"transcribe","language":"zh","duration":3834.0,"text":"...",
 "segments":[{"id":0,"seek":0,"start":0.1,"end":3.82,"text":"...","tokens":[..],
              "temperature":0.0,"avg_logprob":-0.2,"compression_ratio":1.1,"no_speech_prob":0.01}],
 "words":[{"word":"你好","start":0.1,"end":0.44}]}
```

`diarized_json`：`{"task","duration","text","segments":[{"id","start","end","speaker","text"}]}`，speaker 为 `A/B/...`。

```python
files = {"file": (name, open(path, "rb"), "audio/wav")}
data = {"model": "whisper-1", "response_format": "verbose_json",
        "timestamp_granularities[]": "segment", "language": "zh"}
r = httpx.post(f"{base}/v1/audio/transcriptions",
               headers={"Authorization": f"Bearer {key}"} if key else {},
               files=files, data=data, timeout=1800)
```

**兼容性坑**：自建服务对 `timestamp_granularities[]`、`diarized_json` 支持参差。探测策略：先 `verbose_json`，无 `segments` 则退回 `json` + 客户端按 `duration` 均分；不要假设 `words` 一定存在。

## 7. 会拖垮长期私有部署的点

1. **结果 URL 24h 过期**：`transcription_url` 必须立刻下载入库，任务数据也只保留 24 小时（[异步任务管理](https://help.aliyun.com/zh/model-studio/manage-asynchronous-tasks)）。
2. **临时上传 URL 48h**、上传凭证接口 100 QPS 且不可扩容 —— 官方明说"勿用于生产"。对 Echoic 意味着：上传完立刻提交任务，失败重试要重新 `getPolicy`。
3. **查询 20 QPS**（可扩到 100），轮询间隔 ≥2s；官方建议高并发改用回调，但回调配置未在本次核查确认（**待验证**），先按指数退避轮询。
4. **12h/2GB 上限**、`diarization_enabled` 建议 ≤2h；超长会议应在客户端切片（按 30–60 min）再合并，注意 speaker_id 跨片不保证一致。
5. **API Key 地域绑定**，北京 Key 调新加坡端点直接 401；多地域部署要分开存 Key。
6. **实时连接**：任务结束 60s 空闲即断，`task-failed` 后连接不可复用；长会议要写重连 + 新 `task_id`。
7. **`X-DashScope-Async: enable` 漏传**直接失败；`qwen3-asr-flash-filetrans` 用 `input.file_url`（单数），其余用 `file_urls`（数组）——极易踩。
8. **价格/额度**：paraformer-v2 0.00008 元/秒、fun-asr 与 qwen3-asr-flash-filetrans 0.00022 元/秒；ASR 多数模型每月 36,000 秒免费额度（[价格页](https://help.aliyun.com/zh/model-studio/model-pricing)，"token 套餐"细则**待验证**）。
9. **别依赖模型别名**：`qwen-plus`/`qwen-max` 已归入"旧版"，`qwen-turbo` 可能下线；模型名做成可覆盖配置。

## 8. Echoic 落地建议（provider 抽象）

```
ASRProvider: transcribe(local_path) -> {segments:[{start_ms,end_ms,text,speaker}], language}
  ├─ DashScopeFileProvider      # getPolicy 上传 → oss:// → 异步任务 → 下载 JSON（主力）
  ├─ DashScopeRealtimeProvider  # WS 流式，仅实时场景，speaker 恒为 None
  ├─ OpenAIWhisperProvider      # 自建 faster-whisper/FunASR，verbose_json → segments
  └─ MockProvider               # 离线 demo
LLMProvider: chat_json(messages, schema) / embed(texts)
  ├─ DashScopeOpenAICompatProvider（embeddings 同 base_url）
  ├─ GenericOpenAIProvider（任意 base_url）
  └─ MockProvider
```

降级顺序：`DashScopeFile` → 失败（上传被限流/`oss://` 不被接受/额度耗尽）则 `OpenAIWhisper`（若配置了本地服务）→ 再退 `DashScopeRealtime`（仅新录音）→ 最后 `Mock`。**永远不要把 5 分钟的 `qwen3-asr-flash` 放进会议主链路**。

## 9. 待验证清单（写代码前用真 Key 打一遍）

- [ ] `GET /api/v1/uploads?action=getPolicy` 返回字段名与 48h 有效期（本次仅验证端点存在，401）。
- [ ] `oss://` 在 `paraformer-v2` 与 `fun-asr` 上是否都接受（文档只在新模型页明确）。
- [ ] 查询任务是 GET 还是 POST（新旧文档冲突）。
- [ ] `qwen-audio-3.1-asr-flash` 的请求/响应细节与是否真支持 base64 + diarization。
- [ ] `qwen-turbo` 是否仍可调用；`tools` + `stream=true` 是否已兼容。