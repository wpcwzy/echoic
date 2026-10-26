# Echoic AI 处理管线与 Provider 契约

本文档是 `backend-ai` 的实现契约。Lead 已落地 `app/providers/base.py`、`app/models.py`、`app/schemas.py`。
**不要修改** `models.py` / `schemas.py` / `providers/base.py` 的既有字段与签名；需要扩展先与 Lead 沟通。

## 1. 处理流程

```
[prepare]      ffprobe → duration/media_type；生成 playback.m4a（浏览器可播）；
               生成 asr.wav（16kHz mono pcm_s16le，ASR 用）；更新 recordings
[transcribe]   build_asr_provider → transcribe(asr.wav) → 写 speakers + transcript_segments
[analyze]      读转录稿 → LLM 任务：summary/insights/action_items/keywords
               → chapters → viewpoint_tree → suggested_questions → 逐类写 analyses
[embed]        切片 chunks（约 45s 或 280 字一组，保证 <=1200 字）→ embedding（可选）
[finalize]     清理 asr.wav，status=ready, progress=100, processed_at=now
```

- 每个阶段开始/结束更新 `recordings.stage/progress` 与 `jobs.stage/progress`。
- 阶段间检查 `recordings.status == "cancelled"` 则中止且不置 failed。
- `reprocess from=asr` 清理旧 speakers/segments/analyses/chunks 后全跑；`from=insights` 跳过 prepare/transcribe；`from=embeddings` 只重建 chunks。
- 失败：`status=failed`，`error_message` 用中文友好信息（含 provider 与 HTTP 状态，不含密钥），job 同样 failed；`attempts` 已由 queue 递增。
- FFmpeg/ffprobe 路径：环境变量 `ECHOIC_FFMPEG` / `ECHOIC_FFPROBE`，默认 PATH 中 `ffmpeg`/`ffprobe`。不存在时 prepare 阶段给明确错误，mock 管线单测可跳过 prepare。

### 进度分配

| stage | 区间 |
|---|---|
| preparing | 0–8 |
| transcribing | 8–55（provider 回调按 0–100 映射） |
| analyzing | 55–88（4 个任务均分） |
| embedding | 88–96（无 embedding 直接跳过） |
| finalizing | 96–100 |

## 2. 队列 / Worker 语义（`app/services/queue.py`, `app/worker.py`）

```python
def enqueue(db: Session, recording_id: int, from_: str = "all") -> None
def request_cancel(db: Session, recording_id: int) -> bool
def queue_depth(db: Session) -> int
def reset_stale_tasks() -> None            # 启动时：processing/running → queued
async def process_recording(recording_id: int, from_: str = "all") -> None   # pipeline.py
def start_embedded_worker() -> None        # main.py lifespan 调用（线程，daemon）
def stop_embedded_worker() -> None
def worker_status() -> dict                # {"running": bool, "queue_depth": int, "last_error": ...}
```

- 内嵌 worker：守护线程，循环 `SELECT jobs WHERE status='queued' ORDER BY id LIMIT 1` 认领 → `asyncio.run(process_recording(...))`。
  每轮 sleep 1.0s；无任务时 1.5s。单 worker 串行处理。
- `ECHOIC_EMBEDDED_WORKER=false` 时不启动内嵌线程；`python -m app.worker` 提供独立进程入口（同样逻辑）。
- `reset_stale_tasks` 在 lifespan 启动时调用：running 任务回 queued（attempts 已计），recording processing 回 queued。
- 并发：每次 DB 操作用独立短 Session；SQLite 已开 WAL/busy_timeout。
- 异常捕获后记录日志（含 job id/recording id，不含密钥），不使 worker 线程退出。
- 处理超时：整体 watchdog 30 分钟（asyncio.wait_for），超时置 failed。

## 3. Provider 契约（`app/providers/base.py` 已定义，实现勿改签名）

```python
@dataclass
class ASRSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker_label: str | None = None
    confidence: float | None = None

@dataclass
class ASRResult:
    text: str
    segments: list[ASRSegment]
    speakers: list[str]                 # 出现过的 speaker_label（按首次出现顺序）
    language: str
    provider: str
    model: str

class ASRProvider(Protocol):
    name: str
    async def transcribe(self, audio_path: Path, *, language: str = "zh",
                         diarization: bool = True,
                         progress: Callable[[int], None] | None = None) -> ASRResult: ...

class LLMProvider(Protocol):
    name: str
    async def complete(self, messages: list[dict[str, str]], *, json_mode: bool = False,
                       temperature: float = 0.3, max_tokens: int = 4096) -> str: ...
    def stream(self, messages: list[dict[str, str]], *, temperature: float = 0.3,
               max_tokens: int = 4096) -> AsyncIterator[str]: ...

class EmbeddingProvider(Protocol):
    name: str
    dim: int
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```

注册表 `app/providers/registry.py`：

```python
def build_asr_provider(cfg: ASRConfig) -> ASRProvider
def build_llm_provider(cfg: LLMConfig) -> LLMProvider
def build_embedding_provider(cfg: EmbeddingConfig) -> EmbeddingProvider | None
```

配置 dataclass（在 `app/services/settings_store.py`）：
`ASRConfig(provider, base_url, model, api_key, language, diarization)`
`LLMConfig(provider, base_url, model, api_key, temperature)`
`EmbeddingConfig(provider, base_url, model, api_key, dimensions)`
`AIConfig(asr, llm, embeddings, prompts: dict[str,str], system: dict)`
`load_ai_config(db) -> AIConfig`（解密 key、合并默认提示词）
`masked_settings(db) -> dict`（见 API.md §6）
`update_settings(db, payload: dict) -> None`（局部合并；`api_key` 空/省略=保留，`__clear__`=清除）
`async def test_provider(db, kind: str, draft: dict | None) -> dict`
`provider_summary(cfg, kind) -> dict`（脱敏输出）
`demo_mode(cfg) -> bool`（asr.mock 或 llm.mock）

## 4. 各 Provider 实现要求

### 4.1 ASR

| provider | 说明 |
|---|---|
| `mock` | 不依赖网络。若音频时长 ≥ 15min 且标题/文件名含 `traceopt`（演示数据）→ 用 `demo_data.json` 的转录；否则按真实音频时长生成 8–14 段通用中文会议转录（按实际 duration 均分时间戳，2–4 个 speaker）。进度回调按文件时长档位递增。 |
| `dashscope_realtime` | 阿里云 Paraformer 实时 WebSocket，本地文件切 PCM 帧推流；支持 `language_hints`、`diarization_enabled`（以官方文档为准；不支持时降级为单说话人并在日志说明）。base_url 默认 `wss://dashscope.aliyuncs.com/api-ws/v1/inference`，model 默认 `paraformer-realtime-v2`。优先实现该 provider 作为阿里云主路径（无需公网文件 URL）。 |
| `dashscope_file` | 阿里云录音文件识别（async task）。需要公网可访问 URL 或可选 OSS 配置；无 OSS 配置时抛 `ProviderError("录音文件识别需要公网可访问的音频 URL；当前私有部署建议改用 dashscope_realtime，或配置对象存储")`。实现 OSS 上传/签名如时间允许（可选扩展，失败时明确报错）。 |
| `openai` | OpenAI Whisper 兼容 `/v1/audio/transcriptions`，multipart：`file`、`model`、`language`、`response_format=verbose_json`、`timestamp_granularities[]=segment`。解析 segment start/end/text；无说话人 → 单 speaker label "说话人1"；`base_url` 默认 `https://api.openai.com/v1`，兼容 faster-whisper-server/FunASR 等。 |

统一异常：所有 provider 抛 `ProviderError`（定义于 `app/providers/base.py` 或 errors.py），message 中文可展示。重试仅对 429/5xx/网络超时（最多 2 次，指数退避 2s/5s）。日志脱敏。

### 4.2 LLM

| provider | 说明 |
|---|---|
| `mock` | 不联网。按 system prompt 里 `TASK=<name>` 标记返回数据：`minutes/chapters/viewpoint_tree/suggested_questions/chat`。演示数据命中 `demo_data.json` 返回人工示例；否则基于转录文本启发式生成（中文，结构合法）。chat 流式按 20–40 字符分片 + 20ms 延迟。 |
| `openai` | OpenAI 兼容 `/chat/completions`。base_url 可指向 DashScope 兼容模式（`https://dashscope.aliyuncs.com/compatible-mode/v1`，model `qwen-plus`）、DeepSeek、Ollama、vLLM 等。`json_mode=True` 时先带 `response_format={"type":"json_object"}`，若 400 则去掉重试一次。stream 用 SSE 行解析 `data:`。 |

连接测试 `test_provider`：LLM 发一条 ≤16 token 的 "回复：OK"；ASR 用 1 秒静音 wav 走真实接口（若成本高则只校验 base_url 可达；mock 直接 ok）；embedding embed("ping")。返回 `{ok, latency_ms, message}`。

### 4.3 Embeddings

| provider | 说明 |
|---|---|
| `none` | 不用向量，RAG 走字面检索 |
| `openai` | `/embeddings`，OpenAI 兼容；DashScope 兼容模式是否支持以实测为准，若不支持请用 `dashscope` |
| `dashscope` | DashScope 原生 text-embedding-v3（`POST https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding`），batch ≤ 10 条/次、每批间隔限流；`dimensions` 默认 1024。 |

失败不阻断主流程：embedding 阶段异常 → 记录 warning，chunks 保留但 embedding 为空，任务仍 ready。

## 5. AI 分析任务与 JSON 结构

### 5.1 输入构造

- 转录稿格式（每位发言一行）：`[mm:ss] 说话人：文本`，总长超过 **48000 字符** 时保留前 70% + 后 30%，中间加 `……（中间内容因长度省略）……`。
- 每次任务 system prompt 首行固定包含 `TASK=<task_name>`（供 mock 识别）。默认提示词放 `app/services/prompts.py`，设置页可覆盖（存 `app_settings.key='prompts'`）。

### 5.2 四类任务

**A. minutes** → kind `summary` + `insights` + `action_items` + `keywords`
输出：
```json
{
  "summary_md": "## 会议概述\n…\n## 关键结论\n- …\n## 待办事项\n- …",
  "insights": [{"title":"…","content":"…","category":"decision|risk|insight|opportunity"}],
  "action_items": [{"title":"…","owner":"…","due":"…","detail":"…"}],
  "keywords": [{"name":"Graph500","weight":0.9}]
}
```
- `summary_md` 要求中文 Markdown，含：会议概述、关键结论（3–6 条）、决议、待办；不得编造转录稿中没有的事实，人名/数字必须来自原文。
- `owner`/`due` 未提及则空串；`keywords` 5–10 个，weight 0–1。

**B. chapters** → kind `chapters`
```json
{"chapters":[{"title":"…","start_ms":0,"end_ms":300000,"summary":"…","bullets":["…"]}]}
```
3–12 章，按内容转折切分，时间戳必须来自转录稿时间范围（升序不重叠），首章 start_ms=0，末章 end_ms≈duration。

**C. viewpoint_tree（v0.3.4 起为“观点森林”）** → kind `viewpoint_tree`
```json
{"roots":[
  {"id":"t1","label":"专题标题（≤18字）","detail":"一句话说明专题","kind":"topic","time_ms":385000,"speaker":"吴克强","children":[
    {"id":"t1-1","label":"一级论点/任务/风险","detail":"具体内容（≤60字）","kind":"point","time_ms":386000,"speaker":"吴克强","children":[
      {"id":"t1-1-1","label":"补充观点","kind":"point","children":[]}
    ]}
  ]}
]}
```
- 从长会议中提炼 2–6 个相互独立、可命名的专题（短会议 1–2 个），每个专题一棵树；不要强行造总根。
- 每个专题 3–10 个节点，总节点 12–40，最多 3 层；label ≤ 24 字；`detail` ≤ 80 字；`kind`: `topic`（仅专题根）/`point`/`task`/`risk`。
- 观点必须来自转录稿，`time_ms` 取支撑该观点的首个发言时间；`speaker` 用转录稿中的说话人显示名。
- 兼容旧格式 `{"root": {...}}`：旧数据的 root 子主题会由前端提升为森林专题；新分析一律输出 `roots`。

**D. suggested_questions** → kind `suggested_questions`
```json
{"questions":["帮我提炼一下重点内容","ICPE论文怎么分工？"]}
```
4–6 条，口语化、具体、针对本录音，能由转录稿回答。

### 5.3 解析与容错

- 从 LLM 返回中提取 JSON：先直接 `json.loads`，失败则去掉 ```json 围栏、截取首个 `{` 到最后一个 `}`，再失败则单次“修复”请求（附加错误信息与原始输出）。
- 校验结构（Pydantic 或手写）；时间戳夹紧到 `[0, duration_ms]`；缺失可选字段补默认；`chapters` 升序重排；`viewpoint_tree` 节点 id 缺失自动补。
- 单任务失败重试 1 次；仍失败则该 kind 不写，其他任务继续；若 summary 也失败则整体任务 failed（记录错误）。`analyses` 行 upsert（`recording_id+kind` 唯一）。
- LLM 返回内容不落日志全文（只记长度/摘要）。

## 6. RAG 问答（`app/services/chat.py`）

```python
async def stream_chat(db_factory, recording_id: int, user_message: str) -> AsyncIterator[dict]
```
事件 dict 与 API.md §7 一致：`{"type":"start","message_id":N}` / `{"type":"delta","text":"..."}` / `{"type":"citations","citations":[...]}` / `{"type":"done","message_id":N}` / `{"type":"error","message":"..."}`。

流程：
1. 校验录音存在且 ready（否则 error 事件：录音尚未处理完成）。
2. 写入 user chat_message；生成 AI message_id 占位（先插入空 assistant 行，done 后更新内容；失败也保留部分）。
3. 检索：
   - 有 chunks 且存在 embedding 且配置了 embedding provider → query 向量 + numpy 余弦 top 8；
   - 否则字面检索：CJK 二元组 + 英文单词集合重叠打分（idf 简化：按词频），top 8 + 若含章节则附章节目录；
   - 上下文最多 14000 字符，超长按分数截断。
4. system prompt（`TASK=chat`）：私人会议助理；只能依据提供的转录片段/纪要回答；回答用中文 Markdown，引用处标注 `[mm:ss]`；无依据时明确说“转录稿中没有提到”；不得编造人名与数字；简洁（≤400 字）。
5. 历史最近 6 轮拼入 messages。
6. 流式输出，完整答案写入 assistant 行；citations = 检索到且出现在答案上下文里的前 3–5 条片段（segment_id/start_ms/end_ms/speaker/text 截断 120 字）。
7. 客户端断开（asyncio.CancelledError）也要保存已生成内容。

## 7. 演示数据（`app/services/demo.py` + `app/demo_data.json`）

**Lead 提供 `backend/app/demo_data.json`**（高质量示例：TraceOpt 论文讨论 40:37 + 3 条其他录音），结构约定：

```jsonc
{
  "recordings": [
    {
      "key": "traceopt",
      "title": "TraceOpt paper discussion",
      "description": "……",
      "duration_ms": 2437000,
      "recorded_at_days_ago": 2,
      "tags": [{"name":"论文","color":"#7C9CF5"},{"name":"组会","color":"#8BC6A9"}],
      "media_type": "audio",
      "status": "ready",
      "generate_audio": true,          // seed 时用 ffmpeg 合成等长低码率音频（无 ffmpeg 则跳过）
      "speakers": [{"label":"说话人1","display_name":"吴克强","color":"#5B8FF9"}, ...],
      "segments": [{"start_ms":0,"end_ms":12000,"speaker":0,"text":"……"}, ...],
      "insights": {
        "summary": {"markdown":"…"},
        "chapters": [...], "viewpoint_tree": {"root": {...}},
        "insights": [...], "action_items": [...],
        "suggested_questions": [...], "keywords": [...]
      },
      "chat_examples": {"这次会议定了哪些交付节点？":"……"}
    }
  ]
}
```

`services/demo.py`：
```python
def seed_demo(db: Session) -> int    # 幂等（按 title 判断），返回新建数量
def ensure_mock_demo(db: Session) -> None   # 当 provider 为 mock 且库为空时自动 seed（main lifespan 调用）
```
- 创建录音（status=ready、processed_at、asr/llm provider=mock）、tags、speakers、segments、analyses、
  chunks（字面切片，无 embedding）、chat 无需预置。
- `generate_audio=true` 时若 `data/media/{uuid}/playback.m4a` 不存在：用 ffmpeg lavfi 合成等长 220Hz 低音量音频（`-c:a aac -b:a 16k`，单声道；40min 约 5MB，可接受；失败则打 warning 不阻断）。`original.mp3` 可复用同一文件。
- 自动 seed 开关：`ECHOIC_DEMO_SEED`（默认 true）；已有录音则不重复 seed。
- mock LLM 读取本 JSON 作为示例答案来源（demo 问答命中 `chat_examples` 时优先返回）。

## 8. 测试要求（`backend/tests/`，pytest）

- `test_pipeline_mock.py`：建库 → 造录音（ffmpeg 生成 30s 音频，无 ffmpeg 则跳过 prepare）→ `process_recording` → 断言 segments/speakers/analyses/chunks/status=ready；再测 `reprocess insights` 与 cancel。
- `test_chat_mock.py`：seed demo → `stream_chat("这次会议定了哪些交付节点？")` 收集事件：start/delta/done，回答含 “两周/三周” 等关键信息，citations 时间戳合法；历史落库。
- `test_demo_seed.py`：幂等、条数、insights 完整、audio 生成（无 ffmpeg 跳过）。
- `test_openai_provider.py`：respx mock `/chat/completions`（含 json_mode 400 回退）与 `/audio/transcriptions` 请求体字段；错误转 ProviderError。
- `test_settings_store.py`：密钥加密落库（DB 中不含明文）、脱敏、更新/清除、默认提示词合并。
- 所有测试不访问外网；使用临时目录与独立 SQLite 文件。

## 9. 文件归属（写权限）

**backend-ai 负责**：`app/providers/`（除 base.py）`app/services/`（除 search.py 外全部）`app/worker.py` `app/demo_data.json`?（Lead 提供内容，backend-ai 可补充）`backend/tests/test_pipeline_mock.py` `test_chat_mock.py` `test_demo_seed.py` `test_openai_provider.py` `test_settings_store.py`。
**backend-api 负责**：`app/api/` `app/deps.py` `app/core/` `app/services/search.py` `app/schemas.py` 扩展 + `backend/tests/test_api_*.py`。
**Lead 负责**：`app/main.py` `config.py` `db.py` `models.py` `schemas.py` 基础 DTO `providers/base.py` `services/settings_store.py` 的调用契约、`frontend/` 脚手架、Docker/文档。
遇到契约不满足：发消息给 Lead，不要私自改别人的文件。
