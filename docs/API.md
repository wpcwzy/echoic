# Echoic API 契约 v1

所有接口前缀 `/api`。JSON 使用 UTF-8；时间戳字段为 UTC ISO-8601（`2026-10-24T07:30:00Z`）；时长/时间为毫秒整数。
前端、后端、测试均以本文档为唯一契约。示例省略重复字段。

## 0. 约定

- **鉴权**：登录成功后写入 HttpOnly Cookie `echoic_token`（SameSite=Lax，7 天）。同时支持请求头 `Authorization: Bearer <token>`（脚本/CLI）。除 `POST /api/auth/login` 与 `GET /api/system/health` 外全部需要登录。
- **错误**：HTTP 4xx/5xx + `{"detail":"人类可读信息","code":"ERROR_CODE"}`。常用 code：`UNAUTHORIZED` `FORBIDDEN` `NOT_FOUND` `VALIDATION_ERROR` `UPLOAD_TOO_LARGE` `UNSUPPORTED_MEDIA` `CONFLICT` `PROVIDER_ERROR` `RATE_LIMITED`。
- **分页**：`page`（1 起）、`page_size`（默认 24，最大 100）。
- **列表筛选**：空字符串等于不过滤。
- **ID**：录音对外暴露 `id`（int）与 `uuid`（str），API 以 `id` 为主，媒体存储用 `uuid`。
- **录音状态机**：`uploaded → queued → processing → ready`；任意阶段可 `failed`；用户可 `cancelled`。处理阶段 `stage`：`preparing|transcribing|analyzing|embedding|finalizing|done|failed`。

## 1. 数据类型（TypeScript 形式）

```ts
type RecordingStatus = 'uploaded'|'queued'|'processing'|'ready'|'failed'|'cancelled';
type Stage = 'preparing'|'transcribing'|'analyzing'|'embedding'|'finalizing'|'done'|'failed';

interface Tag { id: number; name: string; color: string; }
interface Speaker { id: number; label: string; display_name: string; color: string; is_auto: boolean; }
interface RecordingSummary {
  id: number; uuid: string; title: string; description: string | null;
  status: RecordingStatus; stage: Stage; progress: number; error_message: string | null;
  media_type: 'audio'|'video'; mime_type: string; original_filename: string;
  size_bytes: number; duration_ms: number; recorded_at: string; language: string;
  source: 'upload'|'browser_recording'; tags: Tag[]; speakers: Speaker[];
  has_transcript: boolean; insight_kinds: string[];
  created_at: string; updated_at: string; processed_at: string | null;
}
interface LibraryStats {
  total: number; ready: number; processing: number; failed: number;
  total_duration_ms: number; this_week: number; hours: number;
}
interface Segment {
  id: number; idx: number; speaker_id: number | null;
  start_ms: number; end_ms: number; text: string; confidence: number | null;
}
interface ViewpointNode {
  id: string; label: string; detail?: string;
  kind?: 'topic'|'point'|'task'|'risk'; time_ms?: number; speaker?: string;
  children: ViewpointNode[];
}
interface Chapter { id: string; title: string; start_ms: number; end_ms: number; summary: string; bullets: string[]; }
interface Insight { id: string; title: string; content: string; category: 'decision'|'risk'|'insight'|'opportunity'; }
interface ActionItem { id: string; title: string; owner: string; due: string; status: 'open'|'done'; detail: string; }
interface Citation { recording_id: number; segment_id: number | null; start_ms: number; end_ms: number; speaker: string; text: string; }
interface ChatMessage {
  id: number; role: 'user'|'assistant'; content: string;
  citations: Citation[]; created_at: string;
}
interface Insights {
  recording_id: number; generated_at: string | null; model: string | null;
  summary: { markdown: string } | null;
  chapters: Chapter[] | null;
  viewpoint_tree: { root?: ViewpointNode | null; roots?: ViewpointNode[] } | null; // v0.3.4 观点森林：roots 每项一个专题；旧数据只有 root
  insights: Insight[] | null;
  action_items: ActionItem[] | null;
  suggested_questions: string[] | null;
  keywords: { name: string; weight: number }[] | null;
  missing: string[];   // 未生成的 kind
}
```

## 2. 鉴权

### POST /api/auth/login
```json
{ "username": "admin", "password": "admin123" }
```
→ `200`：`{ "user": {id,username,display_name,is_admin}, "token": "<jwt>" }`，并 `Set-Cookie: echoic_token=...; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800`
→ `401 UNAUTHORIZED`（用户名或密码错误，统一提示）
→ `429 RATE_LIMITED`（同 IP 5 次/分钟）

### POST /api/auth/logout → `{ "ok": true }`（清 Cookie）

### GET /api/auth/me → `{ "user": {...} }`

### POST /api/auth/change-password
```json
{ "old_password": "admin123", "new_password": "newpass456" }
```
→ `{ "ok": true }`；`400 VALIDATION_ERROR`（旧密码错误/新密码过短）

## 3. 资料库 / 录音

### GET /api/recordings
Query：`query`（标题/描述/转录全文模糊）、`status`（逗号分隔，如 `ready,processing`）、`tag_id`、`sort`（`recent`|`oldest`|`title`|`duration`，默认 recent）、`page`、`page_size`。
```json
{
  "items": [RecordingSummary],
  "total": 12, "page": 1, "page_size": 24,
  "stats": { "total":12,"ready":10,"processing":1,"failed":1,
             "total_duration_ms": 8200000,"this_week":3,"hours":2.3 }
}
```

### GET /api/recordings/{id} → `RecordingSummary & { job: JobInfo | null; chat_message_count: number }`
```ts
interface JobInfo { id: number; status: 'queued'|'running'|'done'|'failed'|'cancelled';
  stage: string; progress: number; attempts: number; error: string | null; started_at: string | null; finished_at: string | null; }
```

### POST /api/recordings/upload（multipart/form-data）
| 字段 | 必填 | 说明 |
|---|---|---|
| file | 是 | 音频/视频文件，单文件 ≤ `max_upload_mb`（默认 2048） |
| title | 否 | 默认取文件名（去扩展名） |
| description | 否 | |
| recorded_at | 否 | ISO-8601；默认当前时间 |
| language | 否 | `zh`/`en`/...；默认设置里的 default_language |
| tag_ids | 否 | 逗号分隔（`1,2`） |
| source | 否 | `upload`（默认）或 `browser_recording` |
→ `201 RecordingSummary`（status=queued，已入队）；`413` 文件过大；`415` 不支持的媒体。

### PATCH /api/recordings/{id}
可部分更新：`title`、`description`、`recorded_at`、`language`、`tag_ids: number[]`、`speaker_names: { [speaker_id: number]: string }`。
→ `RecordingSummary`

### DELETE /api/recordings/{id} → `{ "ok": true }`（删除 DB 行 + 媒体目录）

### POST /api/recordings/{id}/reprocess
```json
{ "from": "all" }        // "all" | "asr" | "insights" | "embeddings"
```
→ `{ "ok": true, "status": "queued" }`。`asr` 会重建转录/说话人/分析；`insights` 仅重算 AI 结果；`embeddings` 仅重建切片向量。

### POST /api/recordings/{id}/cancel → `{ "ok": true }`（processing/queued → cancelled）

### GET /api/recordings/{id}/media
返回原始媒体流。`Content-Type` 按文件；支持 `Range: bytes=...` → `206 Partial Content`；`?download=1` 加 `Content-Disposition: attachment`。视频同样走该接口。鉴权失败 401。

### GET /api/recordings/{id}/transcript
```json
{ "recording_id": 1, "language": "zh", "duration_ms": 2437000,
  "speakers": [Speaker], "segments": [Segment] }
```

### PATCH /api/recordings/{id}/speakers/{speaker_id}
```json
{ "display_name": "吴克强", "color": "#1677ff" }
```
→ `Speaker`

### PATCH /api/recordings/{id}/segments/{segment_id}
```json
{ "text": "修改后的文本", "speaker_id": 3 }
```
（两字段均可选）→ `Segment`

### GET /api/recordings/{id}/transcript/export?format=txt|srt|md|json
`Content-Disposition: attachment; filename*=UTF-8''<title>.<ext>`；`format=json` 同 transcript 结构。导出 txt 格式：`[00:01:23] 说话人：文本`；srt 标准字幕。

## 4. AI 分析结果

### GET /api/recordings/{id}/insights → `Insights`
未生成的部分为 `null`，`missing` 列出缺失 kind。所有 kind 就绪时 `generated_at`/`model` 取最新值。

### POST /api/recordings/{id}/insights/regenerate
```json
{ "kinds": ["summary","chapters","viewpoint_tree"] }   // 省略 = 全部
```
→ `{ "ok": true, "status": "queued" }`

### POST /api/recordings/{id}/chat（SSE，见 §7）
`Content-Type: application/json`，body：`{ "message": "这次会议定了哪些交付节点？" }`
返回 `text/event-stream`，事件格式见 §7。

### GET /api/recordings/{id}/chat → `{ "messages": [ChatMessage] }`
### DELETE /api/recordings/{id}/chat → `{ "ok": true }`

## 5. 搜索 / 标签 / 仪表盘

### GET /api/search?q=Graph500&limit=20
```json
{
  "query": "Graph500",
  "recordings": [ { "...RecordingSummary": true, "hit_count": 3 } ],
  "segments": [ { "recording_id":1,"recording_title":"TraceOpt paper discussion",
                  "segment_id":12,"speaker":"吴克强","start_ms":385000,
                  "snippet":"…Graph500 基准测试在 ARM 机器上…" } ]
}
```

### GET /api/tags → `{ "items": [Tag & { recording_count: number }] }`
### POST /api/tags → body `{ "name":"论文","color":"#1677ff" }` → `201 Tag`；重名 409。
### PATCH /api/tags/{id} → `Tag`；### DELETE /api/tags/{id} → `{ "ok": true }`（同时解除关联）

### GET /api/dashboard
```json
{ "stats": LibraryStats, "recent": [RecordingSummary], "processing": [RecordingSummary],
  "top_tags": [Tag & {"recording_count": number}], "storage": {"media_bytes": 123456789, "recordings": 12} }
```

## 6. 设置 / 系统

### GET /api/settings
```json
{
  "ai": {
    "asr":  { "provider":"mock", "base_url":"", "model":"", "language":"zh", "diarization":true,
              "has_api_key":false, "api_key_masked":"", "configured":true },
    "llm":  { "provider":"mock", "base_url":"", "model":"", "temperature":0.3,
              "has_api_key":false, "api_key_masked":"", "configured":true },
    "embeddings": { "provider":"none", "base_url":"", "model":"", "dimensions":1024,
              "has_api_key":false, "api_key_masked":"", "configured":false }
  },
  "prompts": { "summary":"…","chapters":"…","viewpoint_tree":"…","suggested_questions":"…","chat":"…" },
  "system": { "demo_mode":true, "auto_process":true, "max_upload_mb":2048, "default_language":"zh",
              "version":"0.1.0", "worker": { "embedded":true, "running":true, "queue_depth":0 } }
}
```
- `provider` 取值：ASR：`mock|dashscope_file|dashscope_realtime|openai`；LLM：`mock|openai`（`openai` 兼容 DashScope/DeepSeek/Ollama/vLLM，靠 base_url 区分）；embeddings：`none|openai|dashscope`。
- API Key 仅返回掩码（如 `sk-****3f9a`）；空串表示未配置。

### PUT /api/settings
局部合并：
```json
{ "ai": { "llm": { "provider":"openai", "base_url":"https://dashscope.aliyuncs.com/compatible-mode/v1",
                   "model":"qwen-plus", "api_key":"sk-xxx" } },
  "prompts": { "summary": "自定义提示词…" },
  "system": { "auto_process": true, "default_language": "zh" } }
```
- `api_key`：省略或 `""` = 保持原值；`"__clear__"` = 清除。
- 修改后立即生效（下一次处理/问答使用）。
→ `200` 同 GET。
### POST /api/settings/test
```json
{ "kind": "llm", "draft": { "provider":"openai","base_url":"...","model":"...","api_key":"sk-xxx" } }
```
draft 可选，用未保存的配置测试。→ `{ "ok": true, "latency_ms": 812, "message": "连接成功，模型响应正常" }`；失败 `{ok:false,message:"..."}`（HTTP 仍 200）。
### POST /api/settings/prompts/reset → 恢复默认提示词 → GET settings 结构。

### GET /api/system/health（无需登录）
```json
{ "status":"ok", "version":"0.1.0", "db":"ok", "media":"ok", "ffmpeg":"ok",
  "worker":"ok", "demo_mode":true, "time":"2026-10-26T14:00:00Z" }
```
### GET /api/system/info → `{version,data_dir,disk:{total_bytes,used_bytes,free_bytes},recordings,segments,chunks,worker:{...}}`
### POST /api/system/seed-demo → `{ "ok": true, "created": 1 }`（幂等；管理员）
### GET /api/system/logs?lines=200（管理员，可选，返回内存日志尾部；不存在则 404 也可）

## 7. AI 问答 SSE 协议

请求 `POST /api/recordings/{id}/chat`，响应头 `Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`。
每行 `data: {json}\n\n`，事件对象 `type`：

| type | 字段 | 说明 |
|---|---|---|
| `start` | `message_id` | 开始；AI 消息占位 id |
| `delta` | `text` | 增量文本（前端追加） |
| `citations` | `citations: Citation[]` | 引用来源（可在末尾一次性给） |
| `done` | `message_id`, `usage?` | 完成 |
| `error` | `message` | 出错（前端展示） |

示例：
```
data: {"type":"start","message_id":42}

data: {"type":"delta","text":"根据转录稿，"}

data: {"type":"delta","text":"交付节点有两个…"}

data: {"type":"citations","citations":[{"recording_id":1,"segment_id":12,"start_ms":385000,"end_ms":392000,"speaker":"吴克强","text":"…"}]}

data: {"type":"done","message_id":42}
```
注意：用户消息先落库；AI 消息在 done 前若中断也保存已生成部分。前端需处理 `error` 与网络中断。

---

## 9. v0.2 新增：多用户 / 角色 / 分享 / consume（协作与分享）

> 完整设计见 [`docs/08-P1-协作与分享设计.md`](./08-P1-协作与分享设计.md)。以下为接口速查，错误格式与鉴权约定同 §0。

### 9.1 角色与用户

- `users.role`：`admin` | `member`；admin 可见全部录音/用户/设置/系统，member 仅自己（`owner_id`）的录音。不可见资源返回 **404**；无权操作可见资源返回 **403**。
- `GET /api/auth/me` 返回 UserOut 增加 `email / role / disabled / last_login_at`。
- `POST /api/auth/register`（仅 `system.allow_registration=true` 时可用）body `{username,password,display_name?,email?}` → 201 UserOut（member）。
- 登录停用账号 → 403；登录成功更新 `last_login_at`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/users?query=&role=&include_disabled=&page=&page_size=` | admin；`{items:[UserAdminOut],total}` |
| POST | `/api/users` | admin；创建用户（password ≥ 8）→ 201 |
| PATCH | `/api/users/{id}` | admin；`{display_name?,email?,role?,disabled?}`（不能移除最后一个管理员） |
| POST | `/api/users/{id}/reset-password` | admin；`{new_password}` |
| DELETE | `/api/users/{id}` | admin；不能删自己/最后管理员；拥有录音时 409 |

`RecordingSummary` 新增 `owner_id: int|null`、`owner: {id,username,display_name}|null`；`PATCH /api/recordings/{id}` 支持 `owner_id`（仅 admin）。

### 9.2 分享链接

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/recordings/{id}/shares` | 所有者/admin；含已撤销项 |
| POST | `/api/recordings/{id}/shares` | `{password?,expires_at?,include_transcript=true,include_insights=true,allow_download=false}` → 201 ShareOut（含 `token` / `url_path`） |
| GET | `/api/shares?recording_id=&include_revoked=` | 当前用户（admin 全部） |
| DELETE | `/api/shares/{id}` | 软撤销 → `{ok:true}` |
| GET | `/api/public/shares/{token}` | 公开；元数据；无效/撤销/过期 404；`view_count+1` |
| POST | `/api/public/shares/{token}/unlock` | 公开；`{password}` → 设置 HttpOnly Cookie（Path=`/api/public/shares/{token}`，2h）；错误 401，限速 429 |
| GET | `/api/public/shares/{token}/media` | 公开；Range 206；密码分享需先解锁；`?download=1` 受 `allow_download` 限制 |
| GET | `/api/public/shares/{token}/transcript` | 受 `include_transcript` 限制，否则 403 |
| GET | `/api/public/shares/{token}/insights` | 受 `include_insights` 限制，否则 403 |

公开页地址：`/s/{token}`。公开接口不暴露用户/owner 信息，不提供 AI 问答。

### 9.3 consume 目录

- 环境变量：`ECHOIC_CONSUME_ENABLED`、`ECHOIC_CONSUME_DIR`、`ECHOIC_CONSUME_INTERVAL_SEC`、`ECHOIC_CONSUME_OWNER_USERNAME`。
- `GET /api/system/info`（admin）新增 `consume:{enabled,dir,interval_sec,pending_files,failed_files,last_scan}`。
- `POST /api/system/consume/scan`（admin）→ `{ok,result:{scanned,imported,duplicates,failed},at}`。
- 行为：顶层文件 → sha256 去重 → ffprobe 校验 → 移入媒体目录 → 创建录音（`source=consume`，owner=管理员）→ 入队；失败文件移至 `consume/failed/` 并记录错误。

### 9.4 设置

- `GET/PUT /api/settings` 仅 admin；`system` 增加 `allow_registration`、只读 `consume:{enabled,dir,interval_sec}`。
- 标签写操作（POST/PATCH/DELETE `/api/tags*`）仅 admin；读取所有登录用户可用。

## 8. 引擎/进程行为

- 上传成功后由 API 立即写入 `recordings(status=queued)` 与 `jobs(status=queued)`；内嵌 worker 每秒轮询。
- 处理进度通过 `GET /api/recordings/{id}` 轮询（前端 2s 一次，ready/failed 停止）。
- 服务重启时 `processing/running` 的任务重置为 `queued`（最多 1 次自动重启，attempts 超限则 failed）。
- 演示模式（默认 provider=mock）下所有 AI 输出来自内置高质量示例数据；上传真实音频时 mock ASR 会产生演示转录（页面顶部明确标注“演示模式”）。


---

## 10. v0.3 新增：回收站 / 实时转写 / 向量检索 / 对象存储（P1 batch 2+3）

> 完整契约见 [`docs/10-v0.3.0-实现契约.md`](./10-v0.3.0-实现契约.md)。鉴权、错误格式、权限模型同 §0/§9。

### 10.1 回收站与批量（R-23）

- 全局软删除：列表/搜索/仪表盘/媒体/转录/分享/问答等默认排除 `deleted_at IS NOT NULL`；不可见资源 404。
- `DELETE /api/recordings/{id}` → 移入回收站：`{ok:true,id,deleted_at,restore_before}`（同时撤销其公开分享）。
- `GET /api/recordings?trashed=true` → 回收站列表（admin 全部 / member 仅自己；`stats` 为回收站范围）。
- `POST /api/recordings/{id}/restore` → 恢复，返回 `RecordingSummary`（恢复不重新启用已撤销的分享链接）。
- `DELETE /api/recordings/{id}/purge` → 彻底删除（仅回收站内；清理 DB + 媒体/对象存储），`{ok:true,purged:true}`。
- `POST /api/recordings/batch`：
```json
{"action":"trash|restore|purge|reprocess|tag_add|tag_remove",
 "recording_ids":[1,2,3],"tag_ids":[1,2],"from":"all|asr|insights|embeddings"}
```
→ `{action,succeeded,failed,results:[{id,ok,error?}]}`；单次 ≤ `ECHOIC_BATCH_MAX_ITEMS`（默认 100）、部分成功；`tag_add/tag_remove` 仅 admin；权限逐项校验。
- 保留期 `ECHOIC_TRASH_RETENTION_DAYS`（默认 30），worker 每小时清理过期项。

### 10.2 实时转写 Beta（R-19，默认关闭）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/live/sessions` | `{language?,merge_mode?}` → 201 `{id,uuid,status,provider,model,merge_mode,ws_url,...}`；`ECHOIC_LIVE_ENABLED=false` → 403 `LIVE_DISABLED` |
| GET | `/api/live/sessions/{id}` | owner/admin：会话状态 |
| GET | `/api/live/sessions/{id}/segments` | `{segments:[LiveSegmentOut]}` |
| POST | `/api/live/sessions/{id}/stop` \| `/cancel` | 幂等结束/取消 |
| WS | `/api/live/sessions/{id}/stream` | Cookie `echoic_token` 或 `?token=`；二进制 PCM16LE 16k mono 音频帧；JSON `{type:"start"}` / `{type:"stop"}`；服务端事件 `ready/partial/final/error/done`；close codes 4403/4404/4409/4413/1000/1011 |
| GET | `/api/recordings/{id}/live` | 绑定该录音的实时会话 + 片段（详情页“实时稿 Beta”） |

上传接口 `POST /api/recordings/upload` 新增可选 form 字段 `live_session_id`，绑定后按会话 `merge_mode` 合并：`replace`（默认，保留离线说话人转录）或 `promote`（用单说话人实时稿作为最终转录并重算分析；离线 ASR 失败时自动回退 promote）。

### 10.3 向量 / 混合检索（R-20 Phase 1）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/vector/status` | admin：`{backend,available,embeddings_configured,embedding_model,dim,chunks_total,chunks_indexed,stale_chunks,fallback_reason}` |
| POST | `/api/vector/reindex` | admin：`{recording_ids?,force?,dry_run?}` → `{ok,processed_recordings,chunks,skipped,errors}`；单次 ≤200 录音；无 embedding provider → 400 |
| GET | `/api/vector/search?q=&mode=hybrid|semantic&limit=` | 登录用户；ACL（admin 全部/member 自己）→ `{query,mode,results:[{recording_id,title,segment_id,speaker,start_ms,end_ms,text,score}],fallback_reason}` |

- Embedding provider 支持 `mock`（演示/测试，确定性 256 维）；生产可用 OpenAI 兼容或 DashScope embeddings。
- `ECHOIC_VECTOR_BACKEND=auto|numpy|pgvector`；pgvector 不可用时自动降级 numpy 并返回 `fallback_reason`。
- CLI：`python -m app.tools.vector_reindex --all [--dry-run] [--force] [--recording-id N]`。
- AI 问答 RAG 自动使用混合检索（无向量时行为与 v0.2 等价）。

### 10.4 系统

- `GET /api/system/features`（登录）：`{live:{enabled,provider,max_minutes,merge_mode},vector:{backend,search_enabled,embeddings_configured},storage:{backend},trash:{retention_days}}`，供前端能力探测。
- `GET /api/system/health` 新增 `storage/storage_backend/storage_detail` 字段；对象存储不可用时整体 `degraded`。
- 对象存储：`ECHOIC_STORAGE_BACKEND=local|s3` + `ECHOIC_S3_*`；S3/MinIO 模式支持上传、管线 staging、Range 媒体代理（可选 307 预签名）、删除与迁移 CLI `python -m app.tools.storage_migrate --from local --to s3 --dry-run|--apply|--verify`。
