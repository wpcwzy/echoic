# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.3.10] - 2026-10-27

### 新增

- **sqlite-vec 向量后端**：SQLite 部署新增 `sqlite_vec` 后端（依赖 `sqlite-vec`，镜像内安装）；`ECHOIC_VECTOR_BACKEND=auto` 在 SQLite 上优先 sqlite-vec，扩展不可用时自动降级 numpy 并在状态接口说明原因。新增 14 个后端契约/降级测试；后端切换重建索引直接复用 chunks 中的 embedding BLOB，不重新调用 Embedding API。
- **实时字幕 GA（R-19）**：默认开启（无 Key 时自动使用 mock，零网络/零费用）；WebSocket 异常断开支持 **120 秒内断线重连/续传**（同一会话、时间戳连续、无重复片段），新增 `ready {resumed, base_ms, total_ms, connection}` 事件与 `ping/pong`、会话超时清理、stop/cancel 幂等；前端新增「重连中（N/3）」状态与 1s/2s/4s 退避重连，移除全部 Beta 标识。
- **注册页与个人资料页**：登录页按 `health.allow_registration` 显示注册入口；新增公开 `/register`（注册后自动登录）与登录用户 `/profile`（修改昵称/邮箱、修改密码、查看账号信息）；后端新增 `PATCH /api/auth/me` 自助资料接口与公开健康检查字段 `allow_registration`。
- **录音描述自动生成**：上传录音完成转写/AI 分析后，若描述为空，自动从 AI 纪要的会议概述（回退到前几段转录）生成 ≤120 字描述；不覆盖用户已填写内容。新增 `python -m app.tools.backfill_descriptions [--apply]` 用于回填历史录音。

### 修复

- 资料库中新上传录音不再显示“暂无描述”（见上方自动生成；历史数据可用 backfill 工具补齐）。
- 转移录音归属弹窗打开时不再显示未格式化的数字 ID（初始值为空，选中后才显示用户选项）。
- 编辑用户对话框中“启用账号”开关的视觉状态与文字说明现在一致（开=启用，关=停用）。
- 开发模式 Vite `/api` 代理补上 `ws: true`，`npm run dev` 下实时字幕 WebSocket 可正常连接。
- 全量后端 **456 passed**。

## [0.3.9] - 2026-10-27

### 修复

- **AI 分析（章节等）偶发“模型输出无法解析为 JSON”**：真实 Qwen 输出可能包含解释性前缀、Markdown 代码围栏、尾逗号、字符串内未转义换行或截断。JSON 提取现在按「原文 → 围栏/括号子串 → 平衡 JSON 对象 → json-repair 容错修复」逐级降级，并优先返回对象而不是嵌套空数组；`chapters`/`viewpoint_tree` 的 `max_tokens` 提升到 8192，降低长会议输出被截断的概率；LLM Provider 在 `finish_reason=length` 时记录警告。新增 6 个容错解析测试；全量后端 415 passed。

## [0.3.8] - 2026-10-27

### 修复

- **Qwen-Audio Filetrans `REQUEST_INVALID_FILE_URL_VALUE`（真正原因）**：Qwen 文件转写读取私有 `oss://` 临时文件时，必须在提交任务的请求头中携带 `X-DashScope-OssResourceResolve: enable`；缺少该头时 Paraformer 仍可隐式解析，但 Qwen 会返回该错误。现已在提交 `oss://` 文件时自动添加该请求头。
- 已用真实百炼 Key 端到端验证：`qwen-audio-3.1-asr-flash-filetrans` + 临时上传 + `diarization_enabled=true` 成功返回转录与 `speaker_id`（说话人1），不再报 `REQUEST_INVALID_FILE_URL_VALUE`。
- 保留 v0.3.6 的临时凭证 `data` 结构解析、v0.3.7 的 ASCII 临时对象名作为防御性修复；新增请求头回归测试。全量后端 409 passed。

> 安全提示：调试期间使用过的 API Key 已出现在对话/日志中，建议测试通过后在百炼控制台轮换该 Key。

## [0.3.7] - 2026-10-27

### 修复

- **百炼文件转写 `REQUEST_INVALID_FILE_URL_VALUE`**：临时 OSS 对象名原先包含原文件名（中文/空格很常见），百炼对 `oss://` 临时 URL 校验严格，会在提交转写任务后返回该错误。现改为纯 ASCII 对象名（随机 UUID + 原扩展名，原文件名仍保留在 Echoic 数据库中）；任务失败信息对 `REQUEST_INVALID_FILE_URL_VALUE` 增加可操作提示。
- 新增中文文件名临时上传对象的回归测试；全量后端 408 passed。

## [0.3.6] - 2026-10-27

### 修复

- **Qwen-Audio Filetrans 临时上传凭证获取失败**：部分文件转写模型（`qwen-audio-3.1-asr-flash-filetrans` 等）调用百炼 `getPolicy` 临时上传凭证接口时会返回 HTTP 200 但 `output` 为空，导致「缺少 upload_host/upload_dir/policy/signature/oss_access_key_id」。现已支持跨模型回退：先尝试当前模型，再回退到 `paraformer-v2`/`fun-asr` 获取通用 OSS 上传凭证（凭证本身与业务模型无关），并递归解析 `output/data/upload_info` 等常见包装；错误信息会附带实际返回摘要，便于排查账号权限。
- 新增该场景的回归测试；全量后端 407 passed。

## [0.3.5] - 2026-10-27

### 新增

- **文件转写与实时转写可同时独立配置**：AI 服务页把 ASR 拆成两张卡片——「语音识别 · 文件转写」（整场录音最终转录，支持说话人分离）与「实时转写 · 录音页字幕」（Beta，单说话人）。两者各自保存 provider/base_url/model/API Key/语言，可分别使用不同百炼账号或地域。
- 实时转写默认 `auto`：未单独填 Key 时自动复用文件转写的 DashScope Key；也可显式选择 `mock` 或单独的 `dashscope_realtime`。新增 `ECHOIC_REALTIME_ASR_*` 环境变量（provider/api_key/base_url/model/language），与 `ECHOIC_ASR_*` 独立。
- 实时转写卡片会显示当前是否已启用（`ECHOIC_LIVE_ENABLED`）；配置可先保存，开启实时功能后立即生效。测试连接会按 `auto` 规则解析实际使用的 Key/地址。
- 后端新增独立实时 ASR 配置存储/脱敏/校验、`realtime_asr` 连接测试类型与 7 个相关测试；同步更新 API/部署文档。

### 兼容

- 旧数据的 `ai.asr` 配置保持不变并继续作为文件转写；首次升级会自动初始化 `ai.realtime_asr.provider=auto`，实时字幕会自动复用文件转写的 Key。
- 文件转写 ASR 下拉保留 `dashscope_realtime` 旧选项（标注“旧配置兼容，不分离”），避免已有配置失效。

## [0.3.4] - 2026-10-27

### 新增

- **观点树升级为“观点森林”**：长会议会按讨论主题提炼 2–6 个相互独立的专题，每个专题形成一棵观点树，页面按专题纵向排列为森林（每个专题拥有自己的主题 pill、连接线与分支/论点/风险节点），更贴近长会议的真实脉络。
- 观点树生成 Prompt 与 JSON 契约升级为 `{"roots":[ ... ]}`（每个 root 为一个专题）；旧数据 `{"root": ...}` 保持兼容——前端自动把旧根节点的子主题提升为森林专题，历史录音无需重新分析也能获得森林展示。
- 思维导图同步支持森林：多个专题树通过一个虚拟总根在导图中汇总展示；公开分享页的观点树页签同样支持。
- 新增后端观点森林解析/清洗测试（多专题、`trees` 别名、节点上限、时间戳夹紧、旧格式兼容）。

### 兼容说明

- API：`viewpoint_tree` 结构新增可选 `roots` 字段，`root` 变为可选；旧响应仍可用。
- 旧录音若想获得由新 Prompt 直接生成的多专题结果，可对录音执行「重新生成 AI 分析」（mock 模式仍会回退到内置示例结构）。

## [0.3.3] - 2026-10-27

### 修复

- **长页面左侧导航栏跟随页面滚动**：应用外壳改为固定视口高度（`100dvh`），内容区（`.app-content`）独立滚动，左侧 64px 导航栏与顶栏保持固定；原先长页面（设置 / 用户管理 / 长列表）会把整个页面撑高导致导航随之滚出视口。
- 移动端保持原生页面滚动 + 底部固定导航；详情页原有独立滚动布局不受影响。

## [0.3.2] - 2026-10-27

### 新增

- **支持修改录音名称**：资料库卡片/列表新增「重命名」按钮，详情页标题旁新增铅笔按钮与「更多 → 重命名录音」入口；保存后立即生效并同步更新页面标题、脑图/分享展示与下载文件名。重命名为非破坏性操作，不影响转录、AI 分析、标签、分享链接与原始音频数据。

### 修复

- 下载原始媒体文件时使用可编辑的录音名称（扩展名不变），而不是最初的原始文件名；公开分享下载同步生效。

## [0.3.1] - 2026-10-27

### 修复

- **DashScope 实时转写握手假死**：v0.3.0 的 `dashscope_realtime` 在上传录音后可能长时间等待并报「握手超时（未收到 task-started）」——原因是 Provider 在启动 WebSocket 读取循环之前就等待 `task-started` 事件，消息被缓冲但事件永远不会被设置。现在先启动 reader 再等待握手，并区分 `task-failed`（直接展示服务端原因）、超时（包含 endpoint/model 与并发/地域排查提示）。
- **DashScope 录音文件识别 404**：Base URL 常被误填为 `.../api/v1` 或 `.../compatible-mode/v1`，导致拼接出重复路径返回 404。现在会自动规范化 Base URL（支持域名/工作空间域名/常见后缀），并在工作空间域名 404 时自动回退到 `https://dashscope.aliyuncs.com`；404 错误信息会给出正确填写示例。
- **AI 设置页 Base URL 提示与默认值**：ASR 按服务商展示正确的 Base URL / 模型占位（文件识别 `https://dashscope.aliyuncs.com`，实时 `wss://dashscope.aliyuncs.com/api-ws/v1/inference`），切换服务商时自动填充默认值，并新增填写说明；DEPS/DEPLOYMENT/环境变量示例补充 ASR 选型与排障说明。
- 新增 DashScope ASR 回归测试（URL 规范化、实时握手不死锁、task-failed 报错、404 与旧域名回退）。

## [0.3.0] - 2026-10-27

P1 收官版本：**暗色主题、回收站与批量操作、对象存储、实时转写 Beta、向量/混合检索**。docs/09 中 v0.2.1 与 v0.3.0 两个里程碑合并为 v0.3.0 交付。

### 新增

- **回收站与批量操作（R-23）**：删除改为软删除（进入回收站，默认保留 30 天，worker 每小时清理过期项）；回收站列表/恢复/彻底删除；彻底删除同时清理媒体（local 或对象存储）与分享；删除时自动撤销该录音的公开分享链接；批量操作 `trash/restore/purge/reprocess/tag_add/tag_remove`（部分成功、权限逐项校验、单次 ≤100）；全局软删除不变量覆盖列表/搜索/仪表盘/媒体/转录/问答/统计/公开分享；前端多选 + 批量操作条 + 撤销 Toast + 回收站视图。
- **暗色主题（R-24）**：浅色/深色/跟随系统三态切换（用户菜单 + 设置），localStorage 持久化 + 无闪烁初始化；AntD 双算法 + 全站 CSS 变量化；覆盖登录、资料库、详情四 Tab、AI 面板、思维导图、设置、用户、搜索、回收站、公开分享页。
- **对象存储（R-21）**：`StorageBackend` 抽象（local / S3 兼容）；S3/MinIO 模式下上传、管线 staging（下载原始文件、上传 playback）、Range 媒体同源代理（可选 307 预签名）、删除级联；`python -m app.tools.storage_migrate --from local --to s3 --dry-run|--apply|--verify` 迁移 CLI（保留源文件，可 prune）；health 增加 storage 状态；compose 新增可选 `--profile minio`（MinIO 兼容存储 + bucket 初始化）。
- **实时转写 Beta（R-19）**：WebSocket 实时会话（`/api/live`）+ 增量字幕；Mock provider 离线可演示，DashScope paraformer-realtime-v2 provider（真实 Key 待联调）；录音页实时字幕（AudioWorklet 16k PCM）+ 停止后上传绑定 `live_session_id`；合并策略 `replace`（保留离线说话人转录，默认）/ `promote`（用实时单说话人稿作最终转录并重算分析，离线 ASR 失败自动回退）；详情页“实时稿 Beta”查看。
- **向量 / 混合检索（R-20 Phase 1）**：`VectorStore`（numpy 默认 + 实验性 pgvector，自动降级）；mock embeddings（演示/测试）/ OpenAI 兼容 / DashScope；全局语义/混合（RRF）搜索 API + ACL；AI 问答 RAG 接入混合检索（无向量时与 v0.2 等价）；索引重建 API + CLI、状态/过期检测、设置页索引管理；chunks 记录 `embedding_model`。
- **系统**：`/api/system/features` 能力探测；health 增加存储状态；启动迁移覆盖 v0.1/v0.2 数据目录（新增 trash 列、embedding_model、live 表）。
- **文档/工程**：docs/10 v0.3.0 实现契约、docs/09 后续方案、API/DEPLOYMENT/README/CHANGELOG 更新；CI 通过（388 后端测试）、E2E v0.3 34 项、S3 moto 端到端测试、compose MinIO profile。

### 迁移 / 兼容

- v0.1 / v0.2 数据目录启动自动升级；无需手动迁移（建议升级前备份 data 卷）。
- v0.2 API 向后兼容；`DELETE /api/recordings/{id}` 语义由“永久删除”改为“移入回收站”（彻底删除改用 `DELETE /{id}/purge`）。
- 默认行为保持单容器 / SQLite / 无 Redis；S3、MinIO、pgvector、实时转写均为可选增强，关闭/缺失时自动降级。

### 已知限制

- 实时转写为 Beta：默认关闭；不支持实时说话人分离（完成后以离线转写为准）；无自动重连/长连接续传；单进程部署（不支持多副本 WS）；真实阿里云 Key 的实时链路待目标机验证。
- 对象存储：S3/MinIO 运行时（真实网络、TLS、预签名、307+Range）未在本地 Docker 验证，代码路径由 moto 与单元测试覆盖；默认同源代理会占用 API 带宽。
- 向量检索：pgvector 路径未在本地 PG 环境实测（自动降级 numpy）；numpy 后端全量加载到内存，Phase 1 适合中小库；sqlite-vec 未纳入本版本（v0.3.1）。
- 暗色主题：截图由 QA 在 Chromium 验证；Firefox/Safari 未单独回归。
- 回收站恢复不重新启用已撤销的分享链接；彻底删除不可恢复。

## [0.2.0] - 2026-10-27

P1 第一批：**协作与分享**（多用户 / 角色 / 公开分享 / consume 导入）+ 发布工程（CI / GHCR）。

### 新增

- **多用户与角色（R-18）**：`admin` / `member` 两种角色；管理员可在「用户管理」页创建/编辑/停用/重置密码/删除用户（最后管理员、自锁、拥有录音等边界有保护）；开放注册开关（默认关闭，`POST /api/auth/register`）；录音按 `owner_id` 隔离，成员只能访问自己的录音（不可见资源返回 404），管理员可查看全部并转移录音归属；成员会话在账号停用后立即失效；启动时自动迁移 v0.1 数据库（新增 users.role/email/disabled/last_login_at）。
- **公开分享链接（R-17）**：详情页「分享」对话框创建/撤销分享；可选密码保护、有效期（1h/24h/7d/30d/永久/自定义）、是否包含转录 / AI 纪要、是否允许下载；公开只读页 `/s/{token}`（无需登录，独立布局与播放器，按开关展示转写/AI纪要/观点树/章节，移动端适配）；密码门 + HttpOnly 路径级 Cookie（2h）、解锁限速、访问计数；撤销/过期链接失效。
- **consume 目录自动导入（R-22）**：`ECHOIC_CONSUME_DIR`（默认 `<data>/consume`）投递即入库；稳定文件扫描、sha256 去重、ffprobe 校验、失败隔离到 `consume/failed/`、worker 自动扫描与管理端「立即扫描」；系统信息展示待处理/失败/最近扫描。
- **发布工程**：GitHub Actions CI（pytest + 前端构建 + PR 镜像构建）；多架构（amd64/arm64）镜像自动发布到 `ghcr.io/wpcwzy/echoic`（main→latest + sha，tag v*→语义化版本；GHA 缓存 + provenance）；GHCR 包公开、匿名可拉取；**docker compose 改为镜像优先**（`docker compose pull && up -d` 无需本地构建），保留 `docker-compose.build.yml` 源码构建；Dependabot 每周依赖更新（忽略 major）。
- **文档**：`docs/08-P1-协作与分享设计.md`（v0.2 契约）、`docs/09-P1-后续能力技术方案.md`（实时转写 / 向量检索 / 对象存储 / 回收站 / 暗色主题技术方案与 WBS）；API.md / DEPLOYMENT.md / README 更新（镜像部署、GHCR、consume、角色与分享）。

### 迁移 / 兼容

- v0.1 数据目录直接启动即可自动升级（SQLite/PostgreSQL 均适配）；`is_admin` 保留并与 `role` 同步，旧管理员自动回填 `role=admin`。
- v0.1 API 兼容（`/api/auth/me` 仍返回 `{user}` 包装）；新增字段均为增量。

### 已知限制

- 自助注册仅有开关与 API，暂未提供 `/register` 页面（v0.2.1）。
- 分享页明确不包含 AI 问答（避免公开匿名消耗 API 额度）；成员暂无个人资料/改密页面（设置页仅管理员）。
- consume 重复内容会留在目录中（计数提示），需人工清理；多实例共用目录时去重锁为进程内。
- Docker 镜像已在 GitHub runners 真实构建（amd64+arm64）并匿名拉取验证；本开发机仍未本地运行容器；真实阿里云 Key 的联网链路待目标机验证。

## [0.1.0] - 2026-10-26

首个可私有化部署的 MVP。

### 新增

- **资料库**：录音/视频上传（mp3/wav/m4a/aac/ogg/flac/webm/mp4/mov/mkv 等 9+ 格式）、列表/网格视图、状态筛选、标签、排序、统计卡片、全局搜索（标题/描述/转录片段 + 时间点跳转）。
- **网页录音**：浏览器 MediaRecorder 录音（设备选择、暂停/继续、计时、WebAudio 电平表），与上传共用处理管线。
- **处理管线**：ffprobe/ffmpeg 标准化 → 语音转写（说话人 + 时间戳）→ AI 分析（纪要/洞察/待办/关键词/章节/观点树/猜你想问）→ 可选向量切片 → 就绪；DB 任务队列 + 内嵌 worker（可切独立进程），支持进度、取消、失败重试、断点恢复。
- **转录稿**：说话人分组、时间戳 seek、段内搜索、说话人重命名、段落行内编辑、TXT/SRT/Markdown/JSON 导出。
- **AI 纪要**：Markdown 概述/关键结论/决议/待办、关键洞察卡片、关键词、待办勾选、按部分重新生成。
- **观点树 & 思维导图**：结构化观点树（topic/point/task/risk），React Flow + dagre 思维导图弹窗，节点点击定位录音时间。
- **章节**：自动切章（标题/时间区间/摘要/bullets），当前章节高亮，点击跳播。
- **AI 问答**：基于转录稿 + 纪要的 RAG 问答，SSE 流式输出、`[mm:ss]` 引用可点击定位、猜你想问、会话历史与清空。
- **播放器**：进度拖动、±15 秒、倍速、章节刻度、说话人头像、当前段落高亮自动跟随、HTTP Range 按需加载。
- **设置**：ASR / LLM / Embedding 三类 Provider 配置（Mock / DashScope 录音文件识别 / DashScope 实时 / OpenAI 兼容）、API Key 加密存储与掩码、连接测试、提示词编辑与恢复默认、系统默认值、账户改密。
- **安全**：HttpOnly Cookie JWT、bcrypt 密码、Fernet 加密密钥、登录限速、上传类型/大小校验、媒体鉴权、路径安全。
- **部署**：多阶段 Dockerfile（前端构建 + Python 运行 + ffmpeg）、docker compose 单容器一键启动、可选 PostgreSQL 16 / 独立 worker profile、命名卷持久化、healthcheck、非 root 用户；`scripts/dev.sh`、`scripts/run-local.sh`、`scripts/verify.sh`。
- **演示模式**：内置 4 条高质量示例录音（TraceOpt 论文讨论 40:37 等）与全套 AI 结果、合成音频；零 API Key、零外网即可完整演示。
- **文档**：架构、API、管线、前端、部署、需求与竞品分析、产品与 UX、实施计划与验收、DashScope API 调研、E2E 报告。

### 已知限制

- 单管理员起步，多用户/权限/分享链接未实现（Roadmap P1）。
- 无实时转写；AI 分析结果为整场处理，不支持按 kind 精确重算（重新生成会重算全部 AI 结果）。
- 向量检索使用进程内 numpy 余弦（无外部向量库）；无 embedding 时降级为中文二元组字面检索。
- Docker 配置已完成静态校验，但本次开发环境未启用 Docker，未在目标机执行 `docker compose build/up`；阿里云真实 Key 的端到端调用待目标机验证（Mock/离线路径已全量验证）。
- 转录稿未做虚拟滚动，超长录音（数小时）下渲染性能待优化。
