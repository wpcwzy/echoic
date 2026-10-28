# Echoic v1.0 QA 验收报告

> 版本：**v1.0.0**（`backend/app/config.py` / `frontend/package.json` 均为 1.0.0）
> 执行：release-eng（task-30），2026-10-28（Asia/Shanghai）
> 范围：v1.0 全部新增能力（视频分镜、审计日志、Qdrant 向量后端、pgvector staging、真实 DashScope 长稳）
> + 既有版本回归（v0.1 / v0.2 / v0.3 / v0.3.10 smokes、注册/资料、分享/公开页、PWA/吸顶视频）
> 结论：**PASS（全部完成）** —— 代码/本地/容器验收全绿、真实 60 分钟 DashScope soak 通过、
> 发布 commit `aeacd38` 的 CI（含 `pgvector-staging`）与 GHCR 多架构镜像均已验证通过（见 §9）。
> 本报告不做提交/推送；所有改动均为 QA 产物（`e2e/`、`docs/screenshots/v1.0/`、`scripts/verify.sh`），未修改应用代码。

---

## 1. 环境

| 项 | 值 |
|---|---|
| 工作区 | `/home/wpcwzy/echoic`（WSL2，20 vCPU / 23 GiB RAM） |
| Python | 3.12.7（`backend/.venv`；fastapi 0.162.0 / sqlalchemy 2.0.55 / pytest 8.4.2 / qdrant-client 1.20.0 / psycopg 3.3.6） |
| Node | v24.19.0；Playwright 1.48+（chromium-1248，`.cache/ms-playwright`） |
| ffmpeg | 5.1.8-0+deb12u1（`/usr/bin/ffmpeg`） |
| Docker | Docker Desktop 27.3.1（daemon 可用；本机 WSL 环境） |
| Qdrant | `qdrant/qdrant:v1.20.0`（`@sha256:9bfc89f9…`，真实容器） |
| PostgreSQL + pgvector | `pgvector/pgvector:pg16`（PostgreSQL 16.15，pgvector 0.8.7，真实容器） |
| 浏览器 | Chromium headless，桌面 1680×1050 / 移动 390×844（DPR 2） |
| QA 服务 | 生产式 `uvicorn app.main:app` :8101（`ECHOIC_STATIC_DIR=frontend/dist`，独立数据目录 `.cache/e2e-v1-data`，mock AI 管线，`ECHOIC_VECTOR_BACKEND=qdrant`，真实 Qdrant） |

---

## 2. PASS/FAIL 矩阵

| # | 项目 | 命令/方式 | 结果 |
|---|---|---|---|
| 1 | 后端全量测试 | `cd backend && .venv/bin/python -m pytest -q` | **PASS**：514 passed, 4 skipped（165.65s；4 个 skip 为无 DSN 的 pgvector staging，符合设计） |
| 2 | pgvector staging（真实 PG16+pgvector） | Docker `pgvector/pgvector:pg16` + `ECHOIC_PGVECTOR_TEST_DSN` | **PASS**：**4 passed in 0.93s**（PG 16.15 / pgvector 0.8.7） |
| 3 | 前端类型检查 + 生产构建 | `npm run typecheck && npm run build` | **PASS**：tsc 0 错误；vite build 成功（dist 2.2MB，`index-*.js` ~1.96MB） |
| 4 | 一键验证 `scripts/verify.sh`（含全部历史冒烟） | `./scripts/verify.sh` | **PASS**：13 通过 / 0 失败 / 0 跳过（v0.1 30 项、v0.2 34 项、v0.3、v0.3.10、v1.0 全部 PASS） |
| 5 | v1.0 冒烟 `e2e/smoke_v1.py`（真实 Qdrant 环境） | `smoke_v1.py --expect-vector-backend qdrant …` | **PASS**：43/43（0 skip / 0 fail） |
| 6 | 视频分镜 | smoke_v1 §2 | **PASS**：上传 3 段硬切视频 → ready → 2 个分镜（time_ms=2000/4000，360px 宽 JPEG），图片 content-type/魔数/Cache-Control、排序、401/404 权限全部通过 |
| 7 | Qdrant 后端 | smoke_v1 §1（真实服务） | **PASS**：`backend=qdrant`、reindex 139 chunks、语义/混合搜索正常、Qdrant 集合 `echoic_chunks_256` points_count>0（green） |
| 8 | 审计日志 | smoke_v1 §7 | **PASS**：列表/过滤(action,target_type,q)/分页/排序、成员 403、未登录 401、10 类关键动作齐全、detail 无密码/Key 明文（敏感字段 `[redacted]`） |
| 9 | 分享/公开页（视频） | smoke_v1 §5 | **PASS**：创建分享 201 → 公开元数据（标题/描述/media_type=video/权限）→ media Range **206** → transcript/insights/download(200, attachment) → view_count 递增 |
| 10 | 注册/资料/改密回归 | smoke_v1 §6 | **PASS**：allow_registration、注册 201、重复 409、PATCH /auth/me、旧密码错误 400/401、过短新密码 400/422、改密成功、旧密码失效/新密码可登录 |
| 11 | Playwright UI 巡检（16 张截图） | `node e2e/screenshots_v1.mjs` | **PASS**：16/16 截图；0 pageerror；0 横向溢出；0 图片/CDN 请求失败（仅媒体 `ERR_ABORTED` 预期噪声）；行为检查全过 |
| 12 | 视频吸顶 | screenshots §06 | **PASS**：`.player-video-dock` position=sticky，滚动容器 `.detail-scroll` scrollTop=568，y 176→56 |
| 13 | 分镜点击 seek | screenshots §05 | **PASS**：点击 00:02 分镜 → `video.currentTime=2s`，`is-active` 高亮 |
| 14 | 审计页过滤 + 详情展开 | screenshots §12 | **PASS**：Action=recording.upload 过滤后 3 条，展开 detail JSON（`live_session_id: [redacted]`） |
| 15 | 公开视频页（桌面/移动） | screenshots §10/§16 | **PASS**：标题/简介/slogan/播放器/只读分享均正确；移动端无溢出 |
| 16 | 既有版本回归 | verify.sh 内 v0.1/v0.2/v0.3/v0.3.10 smokes | **PASS**（见 §4 的 e2e 期望值维护说明） |
| 17 | 真实 DashScope 60 分钟 soak | `scripts/soak_live.py --minutes 60 …`（真实 Key + 真实音频，Lead 后台 job） | **PASS**：status=ok / exit 0 / duration 3601.39s / planned_duration_completed=true / audio.synthetic=false / provider_errors=0 / reconnects=0（详见 §5） |
| 18 | CI（含 pgvector-staging）+ GHCR 多架构镜像 | `aeacd38` / tag `v1.0.0` 发布后验证 | **PASS**：CI run 40669665551 全部成功（pgvector staging 42s，日志 `4 passed in 0.59s`）；Docker Publish main/tag 均 success；GHCR 6 个 tag 均为 amd64+arm64+attestations 的 OCI index（详见 §9） |

---

## 3. 关键证据

### 3.1 后端测试（pytest）

```
514 passed, 4 skipped in 165.65s (0:02:45)
```

- 4 skipped = `backend/tests/test_vector_pgvector_staging.py`（无 `ECHOIC_PGVECTOR_TEST_DSN` 时模块级 skip，符合设计）；真实 PG 容器下单独运行 **4 passed**。
- 覆盖基线：v1.0 新增 Qdrant 14 项 / 视频分镜 19 项 / 审计 16 项 + 既有 465 项。

### 3.2 pgvector staging（真实容器）

```bash
docker run -d --name echoic-v1-pgvector -e POSTGRES_USER=echoic -e POSTGRES_PASSWORD=echoic \
  -e POSTGRES_DB=echoic -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
ECHOIC_PGVECTOR_TEST_DSN='postgresql+psycopg://echoic:echoic@127.0.0.1:55432/echoic' \
  backend/.venv/bin/python -m pytest -q backend/tests/test_vector_pgvector_staging.py
# → 4 passed in 0.93s
```

- PostgreSQL `16.15 (Debian 16.15-1.pgdg12+2)`，`pgvector 0.8.7`（`pg_available_extensions` / `CREATE EXTENSION` 均验证）。
- GitHub Actions job `pgvector-staging`（`.github/workflows/ci.yml`）使用完全相同的 service 配置与 DSN；
  v1.0.0 发布 CI 已实测通过：**job success，日志 `4 passed in 0.59s`**（见 §9.2）。

### 3.3 前端构建

```
tsc --noEmit：0 errors
vite build：✓ built；dist 2.2MB（index.html 2.17kB，CSS 136kB，JS ~1.96MB）
```

### 3.4 scripts/verify.sh（一键验证）

```
== 环境检查 ==                     PASS（python/ffmpeg/node）
== 后端测试（pytest） ==           PASS（514 passed, 4 skipped）
== 前端类型检查 + 构建 ==          PASS（tsc / vite build）
== 启动服务并健康检查 ==           PASS
== E2E v0.1 冒烟（30 项） ==       PASS
== E2E v0.2 冒烟（34 项） ==       PASS
== E2E v0.3 冒烟 ==                PASS
== E2E v0.3.10 冒烟 ==             PASS
== E2E v1.0 冒烟 ==                PASS（42 通过 / 1 跳过*）
验证结果：13 通过 / 0 失败 / 0 跳过
全部检查通过 ✔
```

\* verify.sh 的临时服务为 `sqlite_vec` 后端且无外部 Qdrant，`smoke_v1` 的「Qdrant 服务端集合校验」按设计 SKIP；真实 Qdrant 全链路见 §3.5。

### 3.5 v1.0 冒烟（真实 Qdrant）

命令：

```bash
ECHOIC_E2E_BASE_URL=http://127.0.0.1:8101 backend/.venv/bin/python e2e/smoke_v1.py \
  --expect-vector-backend qdrant --qdrant-url http://127.0.0.1:16333 --qdrant-api-key qa-secret \
  --data-dir .cache/e2e-v1-data --log e2e/last-run-v1.log
```

结果：**43/43 PASS（0 skip / 0 fail）**，关键摘录：

- `v1.vector.status`：backend=qdrant，indexed=139，model=mock-embedding，dim=256；
- `v1.vector.qdrant-server-collection`：Qdrant 集合 `echoic_chunks_256`，`points_count=139`，status=green（直连 Qdrant REST 校验）；
- `v1.scenes.list`：分镜数=2，`times_ms=[2000, 4000]`；`v1.scenes.image`：JPEG 3437 bytes，Cache-Control: private；
- `v1.audit.expected-actions`：10 类动作齐全（auth.login/register/profile_update/change_password、recording.upload/trash/restore、tag.create、settings.update、share.create）；
- `v1.share.public-media-range`：206，`bytes 0-1023/96021`，type=video/mp4；公开 transcript=8 段；公开下载 200（attachment）；view_count=4；
- `v1.auth.*`：注册/资料/改密全通过（含旧密码失效验证）。

### 3.6 Playwright UI 截图（`docs/screenshots/v1.0/`，16 张）

| 文件 | 内容 |
|---|---|
| `01-login-register-entry.png` | 登录页（浅色，含「立即注册」入口） |
| `02-register.png` | 注册页 |
| `03-library-light.png` | 资料库（浅色） |
| `04-video-detail-scenes.png` | 视频详情 · 分镜条（浅色，2 个分镜） |
| `05-scene-seek.png` | 点击分镜 → seek 00:02（当前分镜高亮） |
| `06-sticky-video.png` | 滚动后视频吸顶（正文/观点树继续滚动） |
| `07-share-dialog.png` | 分享弹窗（标题/简介/slogan 预览、复制链接/复制文案） |
| `08-video-detail-dark.png` | 视频详情 · 分镜条（深色） |
| `09-library-dark.png` | 资料库（深色） |
| `10-public-video-light.png` | 公开分享页（视频、标题/简介/slogan、只读分享） |
| `11-audit-light.png` | 审计日志页（admin，过滤器 + 表格） |
| `12-audit-dark-expanded.png` | 审计日志 · recording.upload 过滤 + 详情展开（脱敏 `[redacted]`） |
| `13-profile-light.png` | 个人资料（资料编辑 + 改密） |
| `14-mobile-library.png` | 移动端资料库（390×844） |
| `15-mobile-video-scenes.png` | 移动端视频详情 · 分镜条 |
| `16-mobile-public-video.png` | 移动端公开分享页（视频） |

- 主题检查 8 项：light/dark 的 `data-theme`、背景亮度均符合预期（dark lum≈0.07）；
- 横向溢出 0；未捕获页面异常 0；控制台错误 8 条均为媒体 Range 的预期噪声（`ERR_ABORTED`，播放器 seek/切页导致）；
- 视觉复核：无遮挡/错位/不可读文本；分镜条、吸顶视频、分享预览、审计详情均正常。

### 3.7 Qdrant 运行时（与 compose profile 一致）

- 真实容器 `qdrant/qdrant:v1.20.0`，`/readyz` → `all shards are ready`；
- API Key 行为：`/collections` 无 Key → 401，带 `api-key` → 200（compose 的 `QDRANT__SERVICE__API_KEY` 注入验证）；
- 应用侧 `backend=qdrant`、reindex 139 chunks、集合 `echoic_chunks_256` green；
- compose profile 健康检查（`bash /dev/tcp/127.0.0.1/6333`）实际为 healthy（task-29 已用真实 compose 验证）。

### 3.8 真实 DashScope 实时 ASR 60 分钟长稳（soak）

- 证据：`e2e/soak-live-v1.0.json`；关键字段见 §5（status=ok、duration=3601.39s、100% coverage、
  partial=6178 / final=61、provider_errors=0、reconnects=0、RSS 峰值 52.2MiB、sessions=1 graceful）。
- 工具：`scripts/soak_live.py`（v1.0 新增，真实 provider 协议、1x 实时推流、断线重连、RSS 采样、JSON+摘要、非零退出码）。

---

## 4. e2e 测试期望值维护（非应用代码改动）

任务要求「既有 smokes 全绿」。运行发现 3 处**过期断言**（应用行为为 v0.3.12 起的既定设计，非 v1.0 回归），已在 `e2e/` 内更新，并逐条说明：

| 冒烟文件 | 过期断言 | 当前既定行为（依据） | 处理 |
|---|---|---|---|
| `smoke_v02.py` | member `POST /api/tags` 期望 403 | v0.3.12「自定义标签」起所有登录用户可创建标签（CHANGELOG v0.3.12；改名/删除仍 admin-only） | 改为断言 member **201**；admin-only 探测改用 `GET /api/system/audit-logs`→403 |
| `smoke_v03.py` | member 批量 `tag_add` 期望 403 | v0.3.12 起成员可对自己的录音批量打标签（越权逐项失败） | 改为：自己的录音 **succeeded=1 且标签生效**；对他人录音 **0 成功/1 失败**；回收站内先 restore |
| `smoke_v0310.py` | health.version 必须 `0.3.10` | v1.0 版本号升级为 1.0.0 | 改为只要求 version 非空（版本无关） |

> 上述均为 QA 测试维护；未改动任何 `backend/` / `frontend/` 应用代码。

---

## 5. 真实 DashScope 60 分钟长稳（soak）— PASS ✅

- 执行方式：Lead 后台 job，真实 Key（`key_source=env:ECHOIC_REALTIME_ASR_API_KEY`，报告不记录明文）+ 真实语音样本
  （`.cache/qwen-test.wav`，3.83s，循环 938 次以覆盖 60 分钟；`audio.synthetic=false`）；
  全程 1x 实时推流，直接使用后端 provider 协议连接 DashScope 实时 ASR（不依赖 Echoic 服务）。
- 证据文件：**`e2e/soak-live-v1.0.json`**（56KB，含完整时间线/会话/RSS 采样）。

| 字段 | 值 |
|---|---|
| `status` / `exit_code` | **ok / 0** |
| `duration_s` | **3601.39**（约 1h00m01s） |
| `planned_duration_completed` | **true**（`planned_seconds=3600`，`streaming_coverage_pct=100.0`） |
| `audio` | `synthetic=false`，source_duration_s=3.83，loops=938，`streamed_ms=3599800`，frames_sent=35998 × 100ms |
| `provider` / `model` / `base_url` | `dashscope_realtime` / `paraformer-realtime-v2` / `wss://dashscope.aliyuncs.com/api-ws/v1/inference` |
| `totals` | sessions=1，ready=1，**partial=6178，final=61**，provider_errors=0，drops=0，reconnects=0 |
| `sessions[0]` | outcome=**graceful**，audio_ms_streamed=3599700，无 error |
| `provider_errors` | `[]`（空） |
| `rss.max_mb` | **52.2 MiB**（713 个采样，每组 5s） |
| 时间线 | +0.21s ready/session_started → +3600.01s finish_requested → +3600.35s session_finished |
| 转写样本 | `final_samples` 12 条，内容为真实语音样本（"Hello world，这里是阿里巴巴语音实验室…"），时间戳连续递增 |

- 结论：**60 分钟真实长稳通过** —— 无断线、无重连、无 provider 错误、内存平稳（峰值 52.2MiB），
  finish-task 正常收敛尾部 final，报告满足全部通过标准（§2 #17）。
- 注意：样本音频较短（3.83s）循环使用，考察重点是**长稳/连接/内存/事件吞吐**，而非长内容识别质量；
  正式生产验收可用更长真实会议录音复跑（命令见 §8）。


---

## 6. 已知限制 / 观察（均非阻塞）

1. **公开分享页无深色主题**：`PublicSharePage` 未读取 `data-theme`，始终浅色（站内页面 light/dark 正常）。属 UX 改进项，不影响功能；已从截图矩阵移除「公开页深色」。
2. **Qdrant 客户端对代理环境变量敏感**：本机沙箱设置 `all_proxy=socks5://127.0.0.1:7890` 且 `NO_PROXY` 含 `[::1]` 模式时，qdrant-client 的 httpx 默认读取代理导致 `ImportError: socksio` / `InvalidURL: Invalid port ':1]'`，应用会**优雅降级 numpy** 并在 `fallback_reason` 说明（不崩溃）。QA 通过为服务进程清理代理 env 后完成真实 Qdrant 验证。建议（非本次改动）：为 Qdrant 客户端显式 `trust_env=False`（或仅对 localhost/局域网 URL 关闭代理），或在部署文档提示「本机 Qdrant 需将地址加入 NO_PROXY/清除 ALL_PROXY」。
3. **CI/GHCR 为发布后置证据**：见 §2 #18（真实 soak 已通过，见 §5）。
4. 移动端视频详情页右下角浮动「AI 问答」按钮与底部导航在极窄屏下视觉邻近（无遮挡关键内容，可点按）；分享弹窗内已有分享条目的操作按钮位于可滚动区域底部（可滚动查看），非缺陷。
5. 视频分镜为 ffmpeg 场景检测（阈值 0.3、最多 60、最小间隔 ~1.5s、360px JPEG），非 OCR/幻灯片文字识别；抽取失败仅告警、不影响处理（best-effort）。

---

## 7. 残余风险

- 真实 soak 使用 3.83s 语音样本循环，重点覆盖长稳而非长内容识别质量（生产可换长音频复跑）；
- `latest` 与 `v1.0.0`/`1.0.0`/`1.0`/`1` 来自同 commit 的两次独立构建（main push 与 tag push），
  OCI index digest 不同（`latest`/`sha-aeacd38` 一组、语义化版本一组）但 revision 标签均为 `aeacd38`，代码一致；
- 本次 QA 以本机进程 + Docker 容器为主，未做完整 `docker compose` 多服务编排联网验收（Qdrant compose profile 已单服务实测；S3/MinIO 属于 v0.3 能力，回归覆盖在历史 smokes）；
- 实时转写会话为单进程内存态（既定架构，多副本不支持会话共享）。

---

## 8. 产物与复现命令

**新增/修改（仅 QA 产物）**

- `e2e/smoke_v1.py`（新增：v1.0 冒烟 43 项）
- `e2e/screenshots_v1.mjs`（新增：16 张截图 + 视觉/行为断言）
- `e2e/screenshot-report-v1.json`（截图巡检报告）
- `e2e/last-run-v1.log`（v1.0 冒烟日志）
- `docs/screenshots/v1.0/*.png`（16 张）
- `scripts/verify.sh`（新增 v1.0 smoke 步骤）
- `e2e/smoke_v02.py` / `smoke_v03.py` / `smoke_v0310.py`（过期断言维护，见 §4）
- `e2e/soak-live-v1.0.json`（真实 60 分钟 DashScope soak 报告，56KB，§5）

**复现**

```bash
# 1) 全量后端 + 前端 + 历史冒烟 + v1.0 冒烟（Qdrant 项自动 SKIP）
./scripts/verify.sh

# 2) 真实 Qdrant 全链路（独立服务 + 独立数据目录）
ECHOIC_DATA_DIR=.cache/e2e-v1-data ECHOIC_STATIC_DIR=frontend/dist \
ECHOIC_DEMO_SEED=true ECHOIC_ALLOW_REGISTRATION=true \
ECHOIC_VECTOR_BACKEND=qdrant ECHOIC_QDRANT_URL=http://127.0.0.1:16333 \
ECHOIC_QDRANT_API_KEY=qa-secret backend/.venv/bin/uvicorn app.main:app --port 8101
backend/.venv/bin/python e2e/smoke_v1.py --base-url http://127.0.0.1:8101 \
  --expect-vector-backend qdrant --qdrant-url http://127.0.0.1:16333 --qdrant-api-key qa-secret
node e2e/screenshots_v1.mjs   # 需 HOME/.npm-home、PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright

# 3) pgvector staging（真实 PG16 容器）
docker run --rm -d --name echoic-pgvector -p 55432:5432 \
  -e POSTGRES_USER=echoic -e POSTGRES_PASSWORD=echoic -e POSTGRES_DB=echoic pgvector/pgvector:pg16
ECHOIC_PGVECTOR_TEST_DSN='postgresql+psycopg://echoic:echoic@127.0.0.1:55432/echoic' \
  backend/.venv/bin/python -m pytest -q backend/tests/test_vector_pgvector_staging.py
```

**日志/证据位置**

- pytest：`.cache/pytest-v1-tail.log`；verify.sh：`.cache/verify-v1c.log`；smoke：`e2e/last-run-v1.log`；
- 截图报告：`e2e/screenshot-report-v1.json`；截图：`docs/screenshots/v1.0/`；
- 截图生成日志：`.cache/screenshots-v1d.log`。

---

## 9. Release verification（v1.0.0，2026-10-28）

> 发布：commit **`aeacd38`**（release: v1.0.0 — Qdrant, pgvector staging, video scenes, audit logs, real-time soak）；
> annotated tag **`v1.0.0`**（tag object `8ffe74b`）与 main 均已推送（`git ls-remote` 校验：main=`aeacd38…`，tag=`8ffe74b…`）。
> 工作区无未提交应用代码改动；本报告 §9 为发布后追加的验证记录。

### 9.1 CI（`aeacd38`，push main）

- Run：**CI #40669665551** — `https://github.com/wpcwzy/echoic/actions/runs/40669665551`
- 结果：**success**（10:07:57Z → 10:12:12Z，约 4m15s）

| Job | 结果 | 耗时 | 备注 |
|---|---|---|---|
| Backend tests (pytest) | ✅ success | 4m12s（10:07:59Z→10:12:11Z） | 全量 pytest 通过 |
| Frontend build (tsc + vite) | ✅ success | 0m24s（10:07:59Z→10:08:23Z） | typecheck + vite build |
| **pgvector staging (real PostgreSQL 16)** | ✅ **success** | 0m42s（10:07:59Z→10:08:41Z） | 真实 `pgvector/pgvector:pg16` service |
| Docker image build (amd64, no push) | ⏭️ skipped | — | PR-only 条件（push 时按设计跳过） |

**pgvector-staging job 关键日志**（job `122752938662`）：

```
PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2) ready; pgvector available: 0.8.7 (installed=not-created-yet (test creates it))
....
4 passed in 0.59s
```

- 所有步骤 success：Checkout → Check pgvector staging test exists → Set up Python 3.12 → Install deps →
  **Verify pgvector extension is available in the service image** → **Run pgvector staging test** → Stop containers。
- 结论：**pgvector staging CI 通过**，真实 PG16+pgvector 链路（扩展/DDL/HNSW/upsert/search/ACL/delete/stats/stale/维度切换）在 GitHub Actions 上验证成功。

### 9.2 Docker Publish（GHCR multi-arch，amd64 + arm64）

| Run | 触发 | 结果 | 耗时 | URL |
|---|---|---|---|---|
| Docker Publish (GHCR) #40669665557 | push `main` | ✅ success | 约 6m22s（10:07:57Z→10:14:19Z） | https://github.com/wpcwzy/echoic/actions/runs/40669665557 |
| Docker Publish (GHCR) #40669675372 | push tag `v1.0.0` | ✅ success | 约 5m00s（10:08:01Z→10:13:01Z） | https://github.com/wpcwzy/echoic/actions/runs/40669675372 |

- 两个 run 的 job「Build & push multi-arch image」均 success；步骤含 Extract metadata、Build and push
  (linux/amd64 + linux/arm64)、Try to make GHCR package public、**Verify pullable tag list**。
- 镜像：`ghcr.io/wpcwzy/echoic`，**匿名拉取/检查通过**（无凭据 registry token + `docker manifest inspect`）。

### 9.3 GHCR tags / digest / platforms / labels（匿名校验）

| Tag | OCI index digest | platforms | version 标签 | revision 标签 |
|---|---|---|---|---|
| `v1.0.0` | `sha256:780bd6e8f5dbab66a385f9cb0c73701c4afd5a81e8aa1048848d1e6105471535` | linux/amd64 + linux/arm64 + 2× attestation | `v1.0.0` | `aeacd3819f7a28014faf8e48be515f6f5849107a` |
| `1.0.0` | 同上 | 同上 | `v1.0.0` | 同上 |
| `1.0` | 同上 | 同上 | `v1.0.0` | 同上 |
| `1` | 同上 | 同上 | `v1.0.0` | 同上 |
| `latest` | `sha256:840a2af540ddf3e1d2351d92aa9d9863b1e2797bdedb831eac2f4de235b9a209` | linux/amd64 + linux/arm64 + 2× attestation | `main` | `aeacd3819f7a28014faf8e48be515f6f5849107a` |
| `sha-aeacd38` | 同上（与 `latest` 同一 main-run 构建） | 同上 | `main` | `aeacd3819f7a28014faf8e48be515f6f5849107a` |

- 语义化版本 4 个 tag（`v1.0.0`/`1.0.0`/`1.0`/`1`）指向**同一个 OCI index**（tag-run 构建），
  amd64 config digest `sha256:6e60c414e154bf2e1a7dfaa19ae3b886b378dd25061bdc7272a7c2a85cd74355`；
- `latest`/`sha-aeacd38` 指向 main-run 的另一个 OCI index（同 commit 的独立构建），
  amd64 config digest `sha256:fd7b38d08446594c9ef5f02b148853f49a5b9454f06845070d92fa1620f400e7`；
- 两类镜像 labels 均含：`org.opencontainers.image.version`（`v1.0.0` / `main`）、
  `org.opencontainers.image.revision=aeacd3819f7a80…`、`org.opencontainers.image.source=https://github.com/wpcwzy/echoic`、
  `org.opencontainers.image.title=Echoic`、`org.opencontainers.image.created`；config `architecture/os = amd64/linux`（arm64 对应各自 config）；
- attestation：每个 index 含 2 个 `unknown/unknown` attestation-manifest（BuildKit provenance/SBOM），符合多架构+证明预期；
- **总结论：GHCR `v1.0.0`/`1.0.0`/`1.0`/`1`/`latest`/`sha-aeacd38` 均可匿名拉取，多架构与标签/版本/commit 元数据全部正确。**

### 9.4 发布验收结论

- ✅ CI（backend / frontend / **pgvector-staging**）全绿；
- ✅ Docker Publish（tag + main）全绿，GHCR 多架构镜像与标签齐全、匿名可拉取；
- ✅ 真实 60 分钟 DashScope soak（§5）、本地/容器 QA（§2–§8）全部通过；
- **v1.0.0 发布验收：PASS。**
