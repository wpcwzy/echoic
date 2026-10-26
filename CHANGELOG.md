# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

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
