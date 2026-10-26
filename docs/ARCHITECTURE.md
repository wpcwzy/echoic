# Echoic 架构设计

> Echoic 是一个自托管（self-hosted）的**私人会议 / 通话 / 录音 / 视频知识库**。
> 定位：paperless-ngx 之于纸质文档 = Echoic 之于录音。上传或网页录音后，自动获得
> **转录稿、AI 纪要、观点树 & 思维导图、章节、关键洞察、待办、AI 问答**，全部数据留在自己的服务器上。

- 版本：v0.1.0（MVP，可私有化部署运行）
- 许可：MIT（建议）
- 参考产品：paperless-ngx（资料库/标签/消费管线/私有化）、飞书妙记 & 钉钉闪记（转写/纪要/章节/思维导图/AI 问答）

---

## 1. 目标与非目标

### 1.1 目标（v0.1 必达）

| # | 能力 | 验收口径 |
|---|------|---------|
| G1 | 网页录音 + 文件上传 | Chrome/Edge 可直接麦克风录音（含暂停/计时/重新录制）；支持 mp3/wav/m4a/aac/ogg/webm/mp4/mov/mkv 上传，单文件 ≤ 2GB |
| G2 | 私有化部署 | `docker compose up -d` 一条命令启动；数据/媒体/密钥全部落在挂载卷；无外部数据库依赖（SQLite 默认，PostgreSQL 可选） |
| G3 | 语音转写 | 可配置阿里云百炼 DashScope / OpenAI(Whisper) 兼容 / Mock；输出带说话人、时间戳的分段转录稿；可编辑说话人名称与文本 |
| G4 | AI 纪要 | Markdown 纪要（概述/关键结论/决议/待办），流式无关，可重新生成 |
| G5 | 观点树 & 思维导图 | 结构化 JSON 观点树；页面“观点树”页签 + “思维导图”弹窗可视化，可点击定位音视频 |
| G6 | 章节 | 自动切章（标题/时间区间/摘要），点击跳转播放 |
| G7 | AI 问答 | 基于转录稿+纪要的 RAG 问答，SSE 流式输出，带时间戳引用；内置“猜你想问” |
| G8 | 资料库 | 列表/搜索/筛选/标签/排序/统计；全局搜索定位到录音+时间点；播放器支持拖动、倍速、±15s |
| G9 | 设置 | 服务商/模型/API Key 网页配置（加密存储）、连接测试、提示词可改、演示模式 |
| G10 | 安全 | 登录鉴权（HttpOnly Cookie JWT）、密码 bcrypt、密钥 Fernet 加密、Range 媒体流、上传类型/大小校验 |
| G11 | 数据导出 | 转录稿导出 TXT/SRT/Markdown/JSON；纪要、观点树等可在页面上复制 |
| G12 | 演示模式 | 无任何 API Key 也可完整跑通 UI（内置 TraceOpt 论文讨论示例录音与全套分析结果） |

### 1.2 非目标（v0.1 不做，列入 Roadmap）

- 团队协作/细粒度权限/分享链接（先单管理员 + 多用户基础表）
- 实时转写（边录边出字）、实时字幕
- 视频画面场景识别、PPT 抽取
- 邮件/扫描目录自动导入（paperless 的 consume 目录可后续加）
- 移动 App / 浏览器扩展
- 向量数据库服务（pgvector/Qdrant）；MVP 用文件内 numpy 向量
- 回收站、审计日志、OAuth/LDAP

---

## 2. 总体架构

```
┌─────────────────────────── 浏览器 (BS) ───────────────────────────┐
│ React 18 + TypeScript + Vite + Ant Design 5                        │
│  资料库 / 录音上传 / 详情页(转写|AI纪要|观点树|章节) / AI问答 / 设置   │
│  MediaRecorder 录音 · <audio> 播放(Range) · SSE 问答流               │
└───────────────┬────────────────────────────────────────────────────┘
                │ HTTPS  (同源，HttpOnly Cookie JWT)
┌───────────────▼────────────────────────────────────────────────────┐
│ FastAPI (single process, uvicorn)                                   │
│  /api/auth  /api/recordings  /api/recordings/{id}/transcript ...    │
│  /api/recordings/{id}/insights  /chat(SSE)  /api/search             │
│  /api/tags  /api/settings  /api/system  /api/dashboard              │
│  ├─ core: config / security / crypto / storage / media(ffmpeg)      │
│  ├─ providers: ASR / LLM / Embedding（可插拔，注册表）                │
│  └─ services: pipeline / queue / chat / search / demo / analysis    │
│  ├─ 内嵌 Worker（后台线程轮询 DB 任务队列；可切独立 worker 进程）       │
│  └─ 静态资源：生产模式直接托管 frontend/dist（无需 nginx）             │
└──────┬───────────────────────────────┬──────────────────────────────┘
       │ SQLAlchemy 2.0                │ 本地文件系统（挂载卷）
┌──────▼────────────┐          ┌───────▼──────────────────────────────┐
│ SQLite (默认, WAL) │          │ /data                                │
│ PostgreSQL(可选)   │          │   echoic.db · secret.key             │
└───────────────────┘          │   media/{uuid}/original.*            │
                               │   media/{uuid}/playback.m4a          │
                               └──────────────────────────────────────┘
       │
       ▼  外部 AI 服务（仅出站 HTTPS，可用自建/内网兼容端点替代）
  DashScope 百炼(paraformer ASR / qwen LLM / embedding)
  OpenAI 兼容 LLM（DeepSeek/Ollama/vLLM/LM Studio…）
  Whisper 兼容 ASR（OpenAI / faster-whisper-server / FunASR…）
```

### 2.1 关键决策

1. **单容器交付**：FastAPI 同时提供 API 与前端静态资源，`docker compose up -d` 即用；不强制 Redis/Celery/Postgres。
2. **数据库任务队列**：`jobs` 表 + 内嵌 worker 线程，处理状态持久化、重启自动恢复（processing→queued）、可手动重试；需要隔离时以 `ECHOIC_EMBEDDED_WORKER=false` 启动独立 worker。
3. **Provider 抽象**：ASR/LLM/Embedding 三类 provider 通过注册表按设置创建；Mock provider 让全流程离线可演示、可测试；真实 provider 仅依赖出站 HTTPS。
4. **密钥安全**：API Key 只存加密值（Fernet），接口只返回 `sk-****abcd` 掩码；密钥来源：环境变量初始值或设置页写入。
5. **同源部署**：前后端同源，Cookie 鉴权 + Range 请求简单可靠；媒体经 API 鉴权后流式返回，不做静态目录裸奔。
6. **AI 结果结构化**：所有 AI 输出落为 `analysis` 行（kind + JSON payload），可单类重算；观点树/思维导图/章节共用一次分析的中间结构，保证一致性。

### 2.2 处理管线（详见 `docs/PIPELINE.md`）

```
upload/record ──▶ 落盘 + ffprobe ──▶ [prepare] 标准化(16k mono wav + playback.m4a)
      ──▶ [transcribe] ASR(可带说话人分离) ──▶ transcript_segments + speakers
      ──▶ [analyze] LLM: 摘要/章节/观点树/洞察/待办/关键词/猜你想问（JSON 结构化）
      ──▶ [embed] 切片 + 向量(可选) ──▶ chunks
      ──▶ [finalize] status=ready
失败：status=failed + error_message，可重试；进度写 progress/stage 供前端轮询。
```

---

## 3. 数据模型

SQLAlchemy 2.0 ORM，表前缀无；UUID 用于对外 ID，自增 int 主键内部使用。所有时间为 UTC（存储）/ 本地化展示。

### users
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| username | str unique index | 登录名 |
| password_hash | str | bcrypt |
| display_name | str | |
| is_admin | bool default true | MVP 首个用户为管理员 |
| created_at | datetime | |

### app_settings
| 字段 | 类型 | 说明 |
|---|---|---|
| key | str PK | 如 `ai.asr`、`ai.llm`、`ai.embeddings`、`prompts`、`system` |
| value_json | Text | JSON 字符串；敏感字段在写入前加密 |
| updated_at | datetime | |

### tags
| id int PK | name str unique | color str (#hex) | created_at |

### recordings
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| uuid | str(36) unique | 对外/存储目录 ID |
| title | str | 默认取文件名 |
| description | text nullable | |
| status | str | `uploaded` `queued` `processing` `ready` `failed` `cancelled` |
| stage | str | `preparing` `transcribing` `analyzing` `embedding` `finalizing` `done` `failed` |
| progress | int 0-100 | |
| error_message | text nullable | |
| media_type | str | `audio` / `video` |
| mime_type / original_filename / original_ext | str | |
| storage_dir | str | 相对 data 目录 |
| size_bytes | bigint | |
| duration_ms | int | ffprobe 得出 |
| recorded_at | datetime | 用户可改，默认上传时间 |
| language | str | `zh` `en` ... |
| source | str | `upload` / `browser_recording` |
| asr_provider / llm_provider | str nullable | 处理时快照 |
| owner_id | FK users.id | |
| created_at / updated_at / processed_at | datetime | |

### recording_tags（多对多）
recording_id + tag_id 联合主键。

### speakers
| id | recording_id FK index | label（ASR 原始标签如 "说话人1"） | display_name | color（#hex） | is_auto bool |

### transcript_segments
| id | recording_id FK index | idx int | speaker_id FK nullable | start_ms | end_ms | text | confidence float nullable |
唯一约束 (recording_id, idx)。

### analyses（唯一约束 recording_id + kind）
| id | recording_id FK | kind | payload_json | provider | model | created_at |

kind 取值：
- `summary` → `{"markdown": "..."}`
- `chapters` → `[{"id","title","start_ms","end_ms","summary","bullets":[]}]`
- `viewpoint_tree` → `{"root": ViewpointNode}`；`ViewpointNode = {id,label,detail?,kind:"topic"|"point"|"task"|"risk",time_ms?,speaker?,children:[]}`
- `insights` → `[{"id","title","content","category":"decision"|"risk"|"insight"|"opportunity"}]`
- `action_items` → `[{"id","title","owner","due","status":"open","detail"}]`
- `suggested_questions` → `["帮我提炼一下重点内容", ...]`
- `keywords` → `[{"name","weight"}]`

### chunks（RAG 切片）
| id | recording_id FK index | idx | start_ms | end_ms | text | embedding BLOB nullable | dim int nullable |
embedding = float32 numpy tobytes；无 embedding 时问答走关键词/字面混合检索。

### chat_messages
| id | recording_id FK index | role `user`/`assistant` | content text | citations_json nullable | created_at |

### jobs
| id | recording_id FK index | type `process` | status `queued`/`running`/`done`/`failed`/`cancelled` | stage | progress | attempts | error | created_at/started_at/finished_at |

---

## 4. 后端目录（FastAPI）

```
backend/
  app/
    main.py            # 应用装配：中间件、路由、静态资源、启动钩子（内嵌 worker/建管理员/演示数据）
    config.py          # pydantic-settings，环境变量
    db.py              # engine/session/init_db；SQLite WAL + busy_timeout；支持 DATABASE_URL 指向 PG
    models.py          # 全部 ORM 模型（唯一事实来源）
    schemas.py         # 对外 DTO（Pydantic v2，前后端契约）
    deps.py            # get_db / current_user / require_admin
    core/
      security.py      # 密码哈希、JWT 签发校验、Cookie
      crypto.py        # Fernet 加解密（API Key）
      storage.py       # data 目录、媒体路径、上传落盘、删除
      media.py         # ffprobe/ffmpeg 封装、时长、标准化、生成 playback
      errors.py        # 统一错误响应
      utils.py
    api/
      __init__.py, auth.py, recordings.py, transcript.py, insights.py,
      chat.py, tags.py, settings.py, search.py, system.py, dashboard.py
    providers/
      base.py          # 协议 + dataclasses（Lead 定义，勿改签名）
      registry.py      # provider 工厂 + 配置解析
      asr_dashscope.py asr_openai.py asr_mock.py
      llm_openai.py llm_mock.py
      embeddings_openai.py embeddings_dashscope.py embeddings_mock.py
    services/
      queue.py         # enqueue/cancel/reset_stale；worker 消费入口
      pipeline.py      # 全流程编排，阶段与进度
      analysis.py      # 提示词构建、JSON 解析、结果落库、重算
      chat.py          # RAG 检索 + 流式回答（SSE 事件字典）
      search.py        # 全局搜索（标题/描述/转录分段 LIKE + 时间点）
      settings_store.py# AI 配置读写/脱敏/连接测试
      demo.py          # 演示数据 seed（示例录音 + 全套分析 + 音频合成）
      prompts.py       # 默认中文提示词模板
    worker.py          # 内嵌线程 / 独立 `python -m app.worker`
    static/            # 前端构建产物（Docker 内拷贝）
  tests/
  requirements.txt  requirements-dev.txt  pytest.ini
```

## 5. 前端目录（React + TS）

```
frontend/src/
  main.tsx App.tsx router.tsx
  api/client.ts（axios/fetch 封装，credentials include，401 跳登录） api/*.ts
  types.ts（与 docs/API.md 对齐）
  store/auth.ts ui.ts
  layouts/AppShell.tsx（左侧图标栏 + 顶栏 + 内容区 + 全局搜索/上传入口）
  pages/Login.tsx Library.tsx Record.tsx RecordingDetail.tsx Settings.tsx Search.tsx
  components/
    player/AudioPlayer.tsx（进度条/±15s/倍速/说话人头像）
    detail/TranscriptPanel.tsx MinutesPanel.tsx ViewpointTreePanel.tsx ChaptersPanel.tsx
    detail/AiPanel.tsx（猜你想问/AI问答流式/思维导图&关键洞察入口）
    detail/MindMapModal.tsx（React Flow + dagre）
    detail/InsightsModal.tsx
    library/RecordingCard.tsx UploadDropzone.tsx StatusBadge.tsx TagPicker.tsx
    common/...
  styles/global.css theme.ts
```

## 6. 部署架构（详见 `docs/DEPLOYMENT.md`）

- 多阶段 `Dockerfile`：node:22-alpine 构建前端 → python:3.12-slim 安装 ffmpeg + pip 依赖 + 拷贝前后端 → 非 root 用户运行 → `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers`。
- `docker-compose.yml`：单服务 `echoic`，端口 `8000:8000`，命名卷 `echoic-data:/data`，env_file `.env`，healthcheck `/api/system/health`。
- 可选 `--profile postgres`：Postgres 16 服务 + `DATABASE_URL`；可选 `--profile standalone-worker`。
- 首次启动：自动建库、建管理员（`ECHOIC_ADMIN_USERNAME/PASSWORD` 默认 admin/admin123）、可选演示数据（`ECHOIC_DEMO_SEED=1`，缺省开启）。
- 备份 = 备份 data 卷（SQLite + media + secret.key）。
- 开发：`scripts/dev.sh` 同时起后端 `uvicorn --reload`（:8000）与前端 `vite`（:5173，代理 /api）。

## 7. 安全与隐私

- 所有解析/转码在本地完成；仅 ASR/LLM 需要的音频或文本发送给用户配置的服务商。
- API Key 加密存储，日志绝不打印密钥；设置接口只回掩码。
- 上传校验扩展名 + ffprobe 解析；文件名不落用户路径；存储用 uuid 目录。
- 媒体接口必须登录；支持 HTTP Range，防止大文件全量加载。
- 登录限速（简单内存计数，5 次/分钟/IP）；JWT 7 天；密码 bcrypt cost=12。
- SSRF 风险控制：base_url 仅允许 http/https；内网地址允许（私有化场景需要），但设置只管理员可改。

## 8. 可观测性与可维护性

- 结构化日志（标准 logging，含 request_id/recording_id/job_id）。
- `GET /api/system/health` 返回 db/media/worker/ffmpeg 状态。
- 任务历史落 `jobs` 表；详情页展示阶段进度与失败原因与“重试”按钮。
- 统一错误 JSON：`{"detail": "...", "code": "..."}`。
- 测试：pytest（API 冒烟/管线 mock/SSE），真实 provider 用 respx mock HTTP；端到端脚本 `e2e/smoke.py`。
