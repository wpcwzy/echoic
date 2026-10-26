# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

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
