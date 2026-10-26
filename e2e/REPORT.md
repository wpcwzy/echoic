# Echoic 端到端验收报告（v0.1.0 MVP）

- **验收日期**：2026-10-26 23:36 — 2026-10-27 00:05（Asia/Shanghai）
- **验收人**：devops-qa（QA）
- **被测版本**：Echoic v0.1.0（后端 `/api/system/health` → version=0.1.0）
- **验收方式**：本机「类生产」单进程运行（FastAPI 同源托管前端构建产物）+ mock AI 管线（无需外部 Key/外网）
- **总体结论**：**PASS**
  - 端到端冒烟 **30/30 通过**
  - UI 截图 **6/6 产出**，0 未捕获异常、0 横向溢出；2 个 P2 视觉缺陷已由前端修复并复检通过
  - 后端测试 **129 passed**
  - 前端 `tsc --noEmit` + `vite build` **通过**
  - Docker/Compose 配置**静态校验通过**（本环境无 Docker CLI，未做运行时验证，见 §8/§9）

---

## 1. 验收环境

| 项目 | 值 |
|---|---|
| 宿主 | WSL2，Linux 6.18.33.2-microsoft-standard-WSL2，x86_64 |
| Python | 3.12.7（`backend/.venv`，httpx 0.28.1） |
| Node.js / npm | v24.19.0 / 11.17.0（npm 使用仓库内缓存 `.cache/npm`、npmmirror 源） |
| FFmpeg | 5.1.8-0+deb12u1（系统 ffmpeg/ffprobe 均可） |
| 浏览器 | Playwright + Chromium Headless Shell 156.0.8078.4（`PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright`） |
| Docker | **不可用**（本 WSL 环境无 Docker CLI，仅静态审查配置） |
| 运行模式 | `ECHOIC_ASR_PROVIDER=mock`、`ECHOIC_LLM_PROVIDER=mock`、`ECHOIC_EMBEDDINGS_PROVIDER=none`（演示模式） |
| 服务 | `http://127.0.0.1:8100`，uvicorn 单进程 + 内嵌 worker 线程 |
| 数据目录 | `/tmp/echoic-e2e`（全新隔离目录） |

运行环境变量（与生产式一致）：

```bash
ECHOIC_DATA_DIR=/tmp/echoic-e2e \
ECHOIC_DEMO_SEED=true \
ECHOIC_EMBEDDED_WORKER=true \
ECHOIC_SECRET_KEY=e2e-secret \
ECHOIC_STATIC_DIR=/home/wpcwzy/echoic/frontend/dist \
PORT=8100 \
./scripts/run-local.sh
```

启动脚本自动完成：`npm run build`（tsc + vite）→ 数据目录初始化 → 建库建管理员 → 演示数据 seed → 内嵌 worker → uvicorn :8100（FastAPI 同源托管 `frontend/dist`）。

前端构建产物（本轮验收实际使用）：

| 文件 | 大小 | gzip |
|---|---|---|
| `frontend/dist/assets/index-Bzfk3gyK.js` | 1,559.83 kB | 499.01 kB |
| `frontend/dist/assets/index-BArVKR0H.css` | 88.76 kB | 16.30 kB |

---

## 2. 执行命令与结果

```bash
# 1) 启动类生产服务（自带前端构建 + 健康检查等待）
ECHOIC_DATA_DIR=/tmp/echoic-e2e ECHOIC_DEMO_SEED=true ECHOIC_EMBEDDED_WORKER=true \
ECHOIC_SECRET_KEY=e2e-secret ECHOIC_STATIC_DIR=$PWD/frontend/dist PORT=8100 \
  ./scripts/run-local.sh
# → vite build 4.46s 成功；GET /api/system/health 200 {"status":"ok",...} 1s 内就绪

# 2) 清空演示录音问答历史，保证截图 AI 面板为空态；然后跑端到端冒烟
backend/.venv/bin/python e2e/smoke.py --base-url http://127.0.0.1:8100 --process-timeout 180
# → 30/30 PASS，日志：e2e/last-run.log

# 3) Playwright 官方截图（1680x1050，写入 docs/screenshots/）
cd e2e && PLAYWRIGHT_BROWSERS_PATH=$PWD/../.cache/ms-playwright \
  ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node screenshots.mjs
# → 6/6 截图，0 页面异常、0 横向溢出

# 4) 一键验证脚本（后端测试 + 前端构建 + 生产式临时服务 + 冒烟）
./scripts/verify.sh --port 8123
# → 129 passed in 50.55s；tsc/vite PASS；健康检查 PASS；冒烟 30/30 PASS

# 5) Docker/Compose 静态审查（本环境无 Docker CLI）
#    见 §8：YAML 解析 + 语义断言 + Dockerfile COPY/指令审查 + .env.example 覆盖检查
```

证据文件：

| 文件 | 内容 |
|---|---|
| `e2e/last-run.log` | 官方冒烟完整日志（123 行，含每条用例 PASS 与关键数据） |
| `e2e/screenshot-report.json` | 截图清单、控制台错误、页面异常、失败请求、溢出检测 |
| `docs/screenshots/*.png` | 6 张 UI 截图（1680×1050） |
| `.cache/verify-run.log` | 一键验证脚本完整输出（本地临时，不入库） |

---

## 3. PASS/FAIL 矩阵

### 3.1 后端 API / 管线端到端冒烟（`e2e/smoke.py`，30 项）

| 分组 | 用例 | 结果 | 关键证据 |
|---|---|---|---|
| 健康 | health | ✅ PASS | status=ok，db/media/ffmpeg/worker 均 ok，demo_mode=true |
| 鉴权 | auth.unauthorized | ✅ PASS | 未登录 GET /api/recordings、/api/dashboard → 401 |
| 鉴权 | auth.login-reject-bad-password | ✅ PASS | 错误密码 → 401 |
| 鉴权 | auth.login | ✅ PASS | Cookie `echoic_token` 含 HttpOnly/SameSite=Lax/Path=//Max-Age=604800，body 返回 token |
| 鉴权 | auth.me | ✅ PASS | Cookie 会话有效（admin/管理员） |
| 鉴权 | auth.bearer | ✅ PASS | `Authorization: Bearer <jwt>` 有效 |
| 系统 | system.info | ✅ PASS | recordings=4 segments=138 chunks=138，worker embedded/running，queue_depth=0 |
| 资料库 | dashboard | ✅ PASS | total=4 ready=4 processing=0 hours=1.5 |
| 资料库 | library.demo-recording | ✅ PASS | 演示录音 id=1「TraceOpt paper discussion」，status=ready |
| 资料库 | library.recording-detail | ✅ PASS | duration_ms=2437000，stage=done，progress=100，insight_kinds 7 类，job 字段存在 |
| AI 分析 | insights.demo | ✅ PASS | chapters=7 insights=6 questions=5 missing=[] model=mock-llm |
| 转录 | transcript.demo | ✅ PASS | speakers=4 segments=73 language=zh |
| 上传 | upload.invalid-media-415 | ✅ PASS | 上传 .txt → 415 UNSUPPORTED_MEDIA |
| 上传 | upload.audio | ✅ PASS | ffmpeg 生成 25s 双音 wav（800,078B）→ 201，status=queued，id=5 |
| 管线 | pipeline.process-ready | ✅ PASS | 1.5s 内 ready，stage=done，progress=100，has_transcript=true（mock 处理极快，轮询仅观察到 preparing→done，属预期） |
| 管线 | pipeline.artifacts | ✅ PASS | 上传录音 segments=8 speakers=2，analyses 6 类齐全，chunks 138→139 |
| 转录编辑 | transcript.speaker-rename | ✅ PASS | PATCH speaker → display_name="E2E说话人"，重新获取一致 |
| 转录编辑 | transcript.segment-edit | ✅ PASS | PATCH segment 文本生效并在 transcript 中持久 |
| 导出 | transcript.export | ✅ PASS | srt(585B，含 `-->` 与 `hh:mm:ss,mmm`)、txt(410B，含 `[hh:mm:ss]`)、json(1261B)、md(477B)，均有 attachment + filename |
| 搜索 | search.global | ✅ PASS | 片段命中 1 条（本次录音）、标题搜索 TraceOpt 命中 2 条录音 |
| 问答 SSE | chat.sse-stream | ✅ PASS | Content-Type=text/event-stream、X-Accel-Buffering=no、Cache-Control=no-cache；事件 start/delta×N/done，回答 262 字 |
| 问答 SSE | chat.history | ✅ PASS | 历史 2 条（user+assistant），assistant 内容 262 字、citations 5 条 |
| 设置 | settings.get | ✅ PASS | ASR/LLM/Embedding 掩码字段齐全、无明文 api_key；worker 状态正确 |
| 设置 | settings.put-partial | ✅ PASS | 局部合并 temperature 0.3→0.25→0.3 生效并可恢复 |
| 设置 | settings.prompts-reset | ✅ PASS | 默认提示词恢复（5 项） |
| 标签 | tags.crud | ✅ PASS | 创建 201、重名 409、列表、改名、删除、删除后消失 |
| 媒体 | media.range-206 | ✅ PASS | `Range: bytes=0-1023` → 206，Content-Range=bytes 0-1023/160138，audio/mp4；?download=1 attachment；未登录 401 |
| 重处理 | pipeline.reprocess-insights | ✅ PASS | `{"from":"insights"}` → queued → 1.5s 内 ready，summary/chapters 保留 |
| 清理 | recordings.delete | ✅ PASS | DELETE 后 DB 行 + 媒体目录删除，再次 GET → 404 |
| 登出 | auth.logout | ✅ PASS | 清 Cookie，/api/auth/me → 401 |

**端到端冒烟：30 PASS / 0 FAIL / 0 SKIP。**

### 3.2 UI 截图与视觉巡检（Playwright，1680×1050）

| # | 文件 | 场景 | 结果 | 巡检结论 |
|---|---|---|---|---|
| 01 | `docs/screenshots/01-login.png` | 登录页（未登录） | ✅ | 左右分栏品牌区+登录卡；演示账号提示与「一键填入」正常；无溢出 |
| 02 | `docs/screenshots/02-library.png` | 资料库（4 条演示录音） | ✅ | 统计卡 4 个、筛选/排序/视图切换、卡片网格（3+1）整齐；日期已统一；无溢出 |
| 03 | `docs/screenshots/03-detail-viewpoint.png` | 详情页 · 观点树 + 右侧 AI 面板（空态） | ✅ | 页签/播放器/观点树分组卡片/AI 面板猜你想问均正常；AI 面板为空态（截图前已清空会话） |
| 04 | `docs/screenshots/04-detail-transcript.png` | 详情页 · 转写页签 | ✅ | 73 段、4 位发言人；头像+姓名+时间戳+文本、当前段高亮、工具条正常 |
| 05 | `docs/screenshots/05-mindmap.png` | 思维导图弹窗 | ✅ | 27 节点，以可读缩放居中根节点显示，图例/控制条正常，无空白残留面板 |
| 06 | `docs/screenshots/06-settings.png` | 设置页 · AI 服务 | ✅ | ASR/LLM/Embedding 表单、掩码 Key、测试连接/保存、演示模式提示；说话人分离开关为标准 44×22 开关 |

- 页面未捕获异常（pageerror）：**0**
- 控制台错误：**1**（登录前 `/api/auth/me` 探测返回 401，属预期鉴权探测，非缺陷）
- 失败请求：**1**（离开详情页时 `/api/recordings/1/media` 音频请求被前端主动 abort，属预期）
- 横向溢出：**0 处**（6 个页面 scrollWidth 均等于视口 1680）

### 3.3 构建、测试与部署静态校验

| 项目 | 命令 | 结果 |
|---|---|---|
| 后端测试 | `cd backend && .venv/bin/python -m pytest -q`（经 verify.sh） | ✅ **129 passed in 50.55s** |
| 前端类型检查 | `cd frontend && npm run typecheck` | ✅ 通过（tsc --noEmit，0 错误） |
| 前端构建 | `cd frontend && npm run build` | ✅ vite build 4.4–4.5s；产物见 §1 |
| 一键验证脚本 | `./scripts/verify.sh --port 8123` | ✅ 环境检查/后端测试/前端构建/健康检查/冒烟 全部 PASS |
| Dockerfile 静态 | COPY 路径存在性 + 必备指令 | ✅ PASS（见 §8） |
| docker-compose 静态 | PyYAML 解析 + 10 项语义断言 | ✅ PASS（见 §8） |
| .env.example 覆盖 | 对照 `config.py` Settings 33 字段 | ✅ 无缺失 |
| Shell 脚本语法 | `bash -n` × 4（dev/run-local/verify/entrypoint） | ✅ PASS |

---

## 4. 截图（交付物）

`docs/screenshots/`（1680×1050 PNG，共 6 张，见上表）：
`01-login.png` · `02-library.png` · `03-detail-viewpoint.png` · `04-detail-transcript.png` · `05-mindmap.png` · `06-settings.png`

README 的截图占位可直接替换为（建议）：

```markdown
![资料库](../docs/screenshots/02-library.png)
![详情页](../docs/screenshots/03-detail-viewpoint.png)
![思维导图](../docs/screenshots/05-mindmap.png)
![设置](../docs/screenshots/06-settings.png)
```

---

## 5. 缺陷清单

### 5.1 已修复并复检通过（前端 owner）

| 编号 | 严重级 | 描述 | 修复 | 复检 |
|---|---|---|---|---|
| DEF-01 | P2 | 设置页「说话人分离（Diarization）」Switch 被 flex 拉伸为整列宽（~470px 蓝色长条） | `.settings-field--switch` 改为 row 布局，`.ant-switch { flex: 0 0 auto }` | ✅ 06-settings.png 中为 44×22 标准开关 |
| DEF-02 | P2 | 思维导图弹窗 fitView 默认缩放过小（27 节点挤在中间，文字不可读）；画布左下角有空白白色圆角矩形（空 MiniMap） | 移除 MiniMap/空 Panel；弹窗放大至 1480×74vh；dagre 间距收紧；fitView 缩放不足时以可读比例（60–90%）居中根节点 | ✅ 05-mindmap.png 中节点文字可读、无空白残留面板 |
| DEF-03 | P3 | 资料库卡片右上角日期与 meta 行日期不一致 | 统一使用 `recorded_at || created_at`，并加「录音时间」tooltip | ✅ 02-library.png 两处日期一致 |

### 5.2 遗留观察项（不阻塞发布，建议后续优化）

| 编号 | 严重级 | 描述 | Owner | 建议 |
|---|---|---|---|---|
| OBS-01 | P3 | 设置页右上角额外出现「管理员」小按钮，与顶栏用户菜单重复；mock 模式下 ASR 卡片仍显示「已配置」徽标，语义可能让人误以为已配置真实服务 | frontend | 移除重复入口；mock 时徽标改为「演示模式」或 tooltip 说明 |
| OBS-02 | P3 | 思维导图按可读缩放打开时显示树的中部（根节点在左中），查看全树需手动平移/点右上角「fit」 | frontend | 可增加「回到根节点 / 适应画布」重置按钮，并保留 Figma 式小地图可选 |
| OBS-03 | P3 | 同源码两次构建的 JS content-hash 不稳定（`index-Bzfk3gyK.js` 1,559.83 kB vs `index-DdjwzMK5.js` 1,559.88 kB，均构建成功、CSS 完全一致，仅受构建环境/缓存状态影响） | build | 若不接受可复现性偏差：固定 Node/npm 版本、清空 `node_modules/.vite` 缓存后构建；当前对功能无影响 |
| OBS-04 | Info | 控制台 1 条 401（登录前 `/api/auth/me`）与 1 条媒体请求 `net::ERR_ABORTED`（离开详情页） | — | 预期网络噪声，无需处理 |
| OBS-05 | Info | 截图中保留演示模式提示条（demo banner） | — | 有意保留以如实展示「演示模式：AI 为内置示例数据」；可在截图前手动关闭（sessionStorage） |

---

## 6. 部署静态审查（本环境无 Docker CLI，未做运行时验证）

**Dockerfile（多阶段）— PASS**

- Stage1 `node:22-alpine`：`COPY frontend/package*.json` → 项目级 `.npmrc`（cache=/root/.npm、logs-dir=/root/.npm/_logs、replace-registry-host=always）→ `npm ci`（BuildKit 缓存挂载）→ `COPY frontend/` → `npm run build`
- Stage2 `python:3.12-slim`：`apt-get install ffmpeg curl ca-certificates tzdata` → `pip install -r backend/requirements.txt` → `COPY backend/`、`COPY docker/`、`COPY --from=Stage1 dist → /app/backend/app/static`
- 非 root 用户 `echoic(uid 1000)`；`ENV ECHOIC_DATA_DIR=/data`、`ECHOIC_STATIC_DIR=/app/backend/app/static`；`VOLUME ["/data"]`；`EXPOSE 8000`；`HEALTHCHECK curl /api/system/health`；`ENTRYPOINT docker/entrypoint.sh`；`CMD uvicorn ... --proxy-headers`
- COPY 源路径全部存在（package-lock.json 已存在，lockfileVersion 3，336 依赖）；`frontend/.npmrc` 中的开发机绝对路径已被 `.dockerignore` 排除

**docker-compose.yml — PASS（PyYAML 解析 + 语义断言）**

- `echoic`：build `.`、`${ECHOIC_HTTP_PORT:-8000}:8000`、`env_file .env`、卷 `echoic-data:/data`、`restart: unless-stopped`、healthcheck `/api/system/health`、`init: true`、`no-new-privileges`
- `echoic-worker`（profile `worker`）：复用镜像、`command: python -m app.worker`、`ECHOIC_EMBEDDED_WORKER=false`、同一数据卷、`ECHOIC_WAIT_FOR_DB` 透传
- `postgres`（profile `postgres`）：`postgres:16-alpine` + `pg_isready` 健康检查 + `echoic-pgdata` 卷
- `.env.example` 覆盖全部 33 个 Settings 字段（含 DashScope 兼容模式示例、管理员口令警告、密钥生成说明、上传大小、`ECHOIC_WAIT_FOR_DB`）

**已知限制/风险**：本机无 Docker CLI，镜像构建与 `docker compose up` 运行时行为（健康检查、非 root 卷权限、entrypoint 等 DB、HEALTHCHECK 实际生效）**未验证**；仅静态审查通过。

---

## 7. 剩余风险与建议

| # | 风险 | 影响 | 建议 |
|---|---|---|---|
| R1 | Docker 镜像未实际构建/运行（环境限制） | 中 | 在具备 Docker 的机器执行 `docker compose up -d --build` 并跑一遍 §2 冒烟；重点验证 `/data` 卷权限（uid 1000）与 HEALTHCHECK |
| R2 | 真实 AI provider 未做联网集成验证（仅 mock；DashScope/OpenAI 请求形状由后端单测 respx 覆盖） | 中 | 用真实 Key 在设置页「测试连接」并上传 1 条真实音频端到端验证；重点观察 dashscope_realtime WebSocket 与 openai ASR 分段时间戳 |
| R3 | 上传媒体格式矩阵未全量验证（E2E 仅 wav；mp3/m4a/aac/ogg/webm/mp4/mov/mkv、>2GB 413 未实测） | 中 | 用各格式真实文件各测 1 条；超大文件验证 413 与反向代理 `client_max_body_size` |
| R4 | UI 仅在 Chromium headless 1680×1050 验证；≤768px 移动端、Firefox/Safari、深色模式未截图 | 中 | 增加 1440/1280/768 视口与 WebKit 冒烟；Safari 需注意 MediaRecorder mp4 分支 |
| R5 | `frontend/package-lock.json` 的 resolved 指向 `registry.npmmirror.com` | 低 | Docker 构建需该源可达，或传 `NPM_REGISTRY` 覆盖；长期建议重新生成 lock 指向 npmjs 或文档注明 |
| R6 | PostgreSQL 部署路径（profile）与 SQLite→PG 数据迁移未验证 | 中 | 单独环境验证 `--profile postgres`；迁移写脚本或文档说明 |
| R7 | 单管理员/无多用户、无 OAuth；登录限速仅逻辑单测，未做压测 | 低 | 公网部署时前置反向代理限流与 fail2ban；按 Roadmap 排期多用户 |
| R8 | 构建产物 content-hash 在同机不同环境间可能不同（见 OBS-03） | 低 | CI 固定环境；发布以构建成功与冒烟结果为准 |
| R9 | 媒体文件与 SQLite 同卷；未做磁盘写满、并发上传压力测试 | 低 | 增加磁盘水位监控与备份策略（见 docs/DEPLOYMENT.md §6） |

---

## 8. 复现指引

```bash
# 1) 安装依赖（首次）
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cd ../frontend && npm ci        # 或 npm install；npm 镜像/缓存见 frontend/.npmrc
cd ../e2e && npm install        # Playwright
PLAYWRIGHT_BROWSERS_PATH=$PWD/../.cache/ms-playwright npx playwright install chromium

# 2) 启动类生产服务（另开终端）
ECHOIC_DATA_DIR=/tmp/echoic-e2e ECHOIC_DEMO_SEED=true ECHOIC_EMBEDDED_WORKER=true \
ECHOIC_SECRET_KEY=e2e-secret ECHOIC_STATIC_DIR=$PWD/frontend/dist PORT=8100 \
  ./scripts/run-local.sh

# 3) 冒烟 + 截图
backend/.venv/bin/python e2e/smoke.py --base-url http://127.0.0.1:8100
cd e2e && ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 \
  PLAYWRIGHT_BROWSERS_PATH=$PWD/../.cache/ms-playwright node screenshots.mjs

# 4) 一键验证（含 pytest/build/health/冒烟）
./scripts/verify.sh
```

> 截图前如需 AI 面板空态：`DELETE /api/recordings/{demo_id}/chat` 清空演示录音会话。

---

## 9. 附录

- 冒烟日志：`e2e/last-run.log`（123 行，含每条用例耗时与关键返回数据）
- 截图元数据/前端诊断：`e2e/screenshot-report.json`
- 验收脚本：`e2e/smoke.py`（30 项）、`e2e/screenshots.mjs`（6 截图 + 视觉巡检）、`scripts/verify.sh`（一键验证）
- 相关文档：`docs/DEPLOYMENT.md`、`docs/API.md`、`docs/ARCHITECTURE.md`、`docs/FRONTEND.md`、`docs/PIPELINE.md`
