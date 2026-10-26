# Echoic v0.3.0 验收报告（P1 batch 2/3：暗色主题、回收站/批量、对象存储、实时转写、向量检索）

- **验收日期**：2026-10-27 03:55 — 04:25（Asia/Shanghai）
- **验收人**：devops-qa（QA）
- **被测版本**：Echoic **v0.3.0**（`/api/system/health` → version=0.3.0）
- **总体结论**：**PASS（可以发布）**
  - v0.1 回归冒烟 **30/30 PASS**
  - v0.2 协作回归冒烟 **34/34 PASS**
  - v0.3 新能力冒烟 **34/34 PASS**（回收站/批量、实时转写 Mock、向量检索、storage local）
  - 后端 pytest **388 passed**（119.7s，含 moto S3、live mock WS、vector mock embeddings）
  - 前端构建通过（`index-DMo8e3bu.js` 1.92 MB / gzip **613.41 KB**；`index-Bpx04KvO.css` gzip 22.41 KB）
  - UI 截图 **8/8**，0 未捕获异常、0 横向溢出；深色主题 6 处状态校验全部通过（含刷新持久化）
  - `scripts/verify.sh` 一键验证 **11 组检查全部通过**（v0.1 + v0.2 + v0.3 三套冒烟）
  - **发布验证通过**：commit `7d80e6b` CI success；tag `v0.3.0` 多架构 GHCR 镜像发布 success，匿名可拉取
  - **无 P1/P2/P3 发布阻塞缺陷**；仅有若干观察项与发布后待验证项（见 §5/§6）

---

## 1. 验收环境与启动方式

| 项目 | 值 |
|---|---|
| 宿主 | WSL2，Linux 6.18.33.2-microsoft-standard-WSL2，x86_64 |
| Python / 后端依赖 | 3.12.7（`backend/.venv`；boto3 + moto[s3]、websockets、numpy） |
| Node / npm | v24.19.0 / 11.17.0（workspace HOME/cache，npmmirror 源） |
| FFmpeg | 5.1.8-0+deb12u1 |
| 浏览器 | Playwright + Chromium Headless Shell 156（`.cache/ms-playwright`） |
| Docker | **本地不可用**（MinIO/S3 运行时未实测；见 §6） |
| 服务 | 生产式单进程 uvicorn :8100 + 内嵌 worker；mock ASR/LLM/Embedding；demo seed 开 |

```bash
# 数据/consume 使用仓库内隔离目录（沙箱每个 shell 独立 /tmp，便于注入 consume 文件）
ECHOIC_DATA_DIR=$PWD/.cache/e2e-v03-data \
ECHOIC_CONSUME_DIR=$PWD/.cache/e2e-v03-consume ECHOIC_CONSUME_ENABLED=true \
ECHOIC_DEMO_SEED=true ECHOIC_EMBEDDED_WORKER=true \
ECHOIC_SECRET_KEY=e2e-secret-v03 \
ECHOIC_STATIC_DIR=$PWD/frontend/dist \
ECHOIC_LIVE_ENABLED=true ECHOIC_LIVE_PROVIDER=mock \
ECHOIC_VECTOR_BACKEND=auto \
./backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8100 --app-dir backend
```

健康检查（v0.3 新增 storage 字段）：

```json
{"status":"ok","version":"0.3.0","db":"ok","media":"ok","ffmpeg":"ok","worker":"ok",
 "storage":"ok","storage_backend":"local","storage_detail":"本地存储可用（…）","demo_mode":true}
```

---

## 2. 执行命令与结果

```bash
# 1) 前端生产构建
cd frontend && npm run build
# → tsc 零错误；vite 4.85s；index-DMo8e3bu.js gzip 613.41 KB + index-Bpx04KvO.css gzip 22.41 KB

# 2) 三套端到端冒烟（fresh 数据目录，同一生产式服务）
backend/.venv/bin/python e2e/smoke.py    --base-url http://127.0.0.1:8100   # 30/30 PASS
backend/.venv/bin/python e2e/smoke_v02.py --base-url http://127.0.0.1:8100 \
  --consume-dir $PWD/.cache/e2e-v03-consume                                # 34/34 PASS
backend/.venv/bin/python e2e/smoke_v03.py --base-url http://127.0.0.1:8100 \
  --data-dir $PWD/.cache/e2e-v03-data                                      # 34/34 PASS

# 3) 后端全量测试
cd backend && .venv/bin/python -m pytest -q
# → 388 passed in 119.73s

# 4) 一键验证（pytest + 前端 typecheck/build + 健康检查 + 三套冒烟）
./scripts/verify.sh --port 8123
# → 验证结果：11 通过 / 0 失败 / 0 跳过；全部检查通过

# 5) UI 截图 + 视觉巡检（Playwright，1680x1050；移动端 390x844）
cd e2e && ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node screenshots_v03.mjs
# → 8/8 截图 PASS；0 pageerror；0 overflow；主题切换 6 次校验全部通过
```

证据文件：

| 文件 | 内容 |
|---|---|
| `e2e/last-run.log` / `e2e/last-run-v02.log` / `e2e/last-run-v03.log` | 三套冒烟完整日志（30 / 34 / 34 项） |
| `e2e/screenshot-report-v03.json` | 截图清单、主题状态、控制台/页面异常、失败请求、溢出检测 |
| `docs/screenshots/v0.3/*.png` | 8 张官方 UI 截图 |
| `.cache/verify-v03-run.log` | verify.sh 完整输出（本地临时，不入库） |

---

## 3. PASS/FAIL 矩阵

### 3.1 总览

| 检查项 | 结果 |
|---|---|
| v0.1 回归（auth/上传/转录/分析/SSE/标签/设置/Range/重处理） | ✅ 30/30 PASS |
| v0.2 回归（角色/权限/分享/consume） | ✅ 34/34 PASS |
| v0.3 新能力（回收站/批量/实时 Mock/向量/storage local） | ✅ 34/34 PASS |
| 后端 pytest 全量 | ✅ 388 passed |
| 前端 tsc + vite build | ✅ PASS |
| `scripts/verify.sh` | ✅ 11 通过 / 0 失败 |
| UI 截图 + 视觉巡检 | ✅ 8/8，0 异常 / 0 溢出 |
| GitHub Actions CI（commit `7d80e6b`） | ✅ **success**（backend pytest + frontend build，2m59s） |
| GHCR v0.3.0 多架构镜像（tag `v0.3.0`） | ✅ **success**（amd64+arm64，匿名拉取已验证，见 §7） |

### 3.2 v0.3 新能力冒烟（`e2e/smoke_v03.py`，34 项，全部 PASS）

| 分组 | 用例与结果 |
|---|---|
| 基础 | health（含 storage=ok）、admin 登录、system.info、demo 数据、**features**（live.enabled=true/provider=mock/merge=replace；vector.backend=numpy/search_enabled；storage=local；trash.retention=30）、health.storage |
| 回收站 R-23 | 软删除响应含 `deleted_at/restore_before`；默认列表/搜索/仪表盘/详情/媒体/转录/分析/问答/分享/公开分享全部不可见（404）；回收站列表含 deleted_at；恢复后转录/分析/列表恢复；删除时分享自动撤销且恢复后仍失效；彻底删除 DB + 本地媒体目录（文件系统校验）；成员范围 |
| 批量操作 | 3 条录音批量移入回收站（含无效 id → 3 成功 1 失败，失败项带原因）；批量恢复；批量 tag_add/tag_remove；批量 reprocess(from=insights)；purge 仅对回收站生效（active → 逐条失败）；超过上限（101 > 100）→ 400；空列表 → 422；member：可批量操作自己的录音、tag 批量 403、混合越权 1 成功 1 失败 |
| 实时转写 Beta R-19 | 创建会话（mock provider、ws_url）；WS 全流程 `ready / partial×N / final×N / done`（12s PCM，total_ms≥6000）；片段持久化；**replace**：上传 wav 带 live_session_id → ready → `GET /recordings/{id}/live` 有会话+片段、离线转录仍保留多说话人；**promote**：实时稿替换为单说话人转录；stop 幂等；同一会话第二个 WS 生产者被 **4409** 拒绝；重复绑定同一 live_session → **409** |
| 向量/混合检索 R-20 | 设置 embeddings=mock → status（configured=true, backend=numpy, available）；reindex force → `processed=4, chunks=138`，`chunks_indexed>0`、`stale=0`；semantic 搜索命中（score>0）；hybrid 搜索命中；member ACL（用自有转录短语查询仅命中自己的录音、无越权泄露）；embeddings=none → semantic 返回 `fallback_reason`、reindex 400；恢复 mock 后 hybrid 无 fallback |
| storage / 回归 | storage local 媒体 Range 206（与 v0.1 一致）；demo 问答 SSE 回归正常 |

### 3.3 后端测试与一键验证

- `pytest -q`：**388 passed in 119.73s**（v0.2 的 179 项 + v0.3 新增 trash/batch/live/vector/storage/migrations/features 等，含 moto S3 上传→pipeline→Range 集成、live mock WS+merge、vector mock embeddings）；
- `scripts/verify.sh`（v0.1+v0.2+v0.3 冒烟、pytest、前端 typecheck/build、健康检查）：**11 通过 / 0 失败 / 0 跳过**。

---

## 4. UI 截图与视觉巡检（8/8）

| # | 文件 | 场景 | 结果 | 巡检结论 |
|---|---|---|---|---|
| 01 | `docs/screenshots/v0.3/01-library-dark.png` | 资料库 · 深色 | ✅ | 深色卡片/统计/筛选/批量选择/回收站徽标正常；文本对比清晰；无白底浮层；无溢出 |
| 02 | `docs/screenshots/v0.3/02-detail-dark.png` | 详情页 · 深色（观点树 + AI 面板） | ✅ | 播放器、页签、观点树浅绿卡片、AI 面板均正确深色适配；无白色残留面板 |
| 03 | `docs/screenshots/v0.3/03-trash.png` | 回收站 | ✅ | 保留期提示（30 天）、剩余天数、删除时间、恢复/彻底删除操作完整；空态与计数正常 |
| 04 | `docs/screenshots/v0.3/04-live-captions.png` | 录音页 · 实时字幕 Beta | ✅ | 实时字幕开关（默认开）+ Beta 说明 + 单次上限；mock provider 实时输出 2 段字幕（00:00 / 00:03）与「实时转写中」状态；录音计时/电平表正常 |
| 05 | `docs/screenshots/v0.3/05-semantic-search.png` | 全局搜索 · 语义检索 | ✅ | 关键词/混合检索/语义检索三种模式；「向量后端: numpy」提示；3 条结果带相关度 34%/29%/20% 与高亮片段、时间戳 |
| 06 | `docs/screenshots/v0.3/06-settings-index.png` | 设置 · 系统（检索索引） | ✅ | 开放注册开关、consume 卡片（目录/间隔/待处理/失败/最近扫描）、检索索引卡片（numpy / mock-embedding / 256 维 / 138/138 / 过期 0，试运行+重建索引+刷新状态）、后台处理服务（v0.3.0） |
| 07 | `docs/screenshots/v0.3/07-light-library.png` | 资料库 · 浅色对照 | ✅ | 浅色主题与 v0.2 视觉一致，切换正常 |
| 08 | `docs/screenshots/v0.3/08-mobile-dark.png` | 资料库 · 深色 · 390×844 | ✅ | 移动端单列布局 + 底部导航（资料库/录音/搜索/用户管理/设置），无横向溢出 |

主题巡检数据（`e2e/screenshot-report-v03.json`）：

- 用户菜单三态切换：通过 UI 菜单从浅色切换为深色成功，`html[data-theme]` 正确；刷新后 `localStorage` 与 `data-theme` 均保持 dark（防闪烁内联脚本生效）；
- 页面背景亮度：light `0.96` / dark `0.07`（library、detail、settings、mobile 一致），无“深色模式白底面板”；
- 诊断：**pageErrors 0**、**横向溢出 0**、**非预期控制台错误 0**；3 条控制台 401/404 属预期噪声（登录前 `/api/auth/me` 探测、无实时会话录音的 `/live` 404）；1 条 media `ERR_ABORTED` 为离开详情页时播放器主动中止。

---

## 5. 缺陷清单

**未发现发布阻塞缺陷（无 P1/P2/P3）**。以下为观察项（均不影响发布）：

| 编号 | 级别 | 描述 | Owner | 建议 |
|---|---|---|---|---|
| OBS-v03-01 | P3（文档/截图） | `06-settings-index.png` 为滚动定位截图，页面从系统卡片中部开始，缺少顶部页签上下文 | QA（截图脚本） | 可选：截图时额外向上滚动 120px 以包含「设置 / 系统」标题；非应用缺陷 |
| OBS-v03-02 | Info | 截图含 QA 测试数据（回收站 1 条 QA 示例录音、演示模式横幅、管理员账号） | QA | 有意保留以真实反映功能；正式发布文档可按需替换 |
| OBS-v03-03 | Info | 实时字幕截图中电平表为模拟输入（Playwright fake mic） | 环境 | 真实浏览器/麦克风行为由前端自测覆盖 |
| OBS-v03-04 | Info | 深色主题对比度为人工视觉巡检（未做 WCAG 自动化测量） | QA | 后续可加入 axe/contrast 自动检查（非本次范围） |

v0.2 及之前已修复项（Diarization 开关、思维导图缩放、资料库日期、CI ffmpeg 等）在 v0.3 回归中均正常。

---

## 6. 剩余风险（发布后关注）

| # | 风险 | 影响 | 建议 |
|---|---|---|---|
| R1 | **本地无 Docker**：MinIO compose profile / 对象存储运行时（pull、healthcheck `mcli ready`、minio-init 建桶、S3 上传/Range/迁移）未在真实 Docker 验证 | 中 | 在 Docker 主机执行 `docker compose --profile minio up -d` + 设置 `ECHOIC_STORAGE_BACKEND=s3` 做一次人工验收；S3 代码路径已有 moto 单测覆盖 |
| R2 | **真实 DashScope 实时转写**未联网验证（本次仅 mock provider）；无真实 Key/外网环境 | 中 | 目标机配置 DashScope ASR 后，用 ≥10s 真实麦克风录音验证 WS 事件与合并；Beta 文档已注明不自动重连 |
| R3 | **pgvector 后端**未实测（无 PG + pgvector 环境）；`auto` 已在本机正确降级 numpy 且状态/搜索可用 | 低 | 有 PG 环境时验证 `CREATE EXTENSION vector` 与降级日志；不可用时自动 numpy 是设计行为 |
| R4 | 回收站**自动过期清理**（worker 每小时）仅由 pytest 单测覆盖，E2E 未做时间穿越 | 低 | 上线后观察 `trash` 清理日志；保留期默认 30 天 |
| R5 | 浏览器矩阵：UI 截图仅 Chromium；深色主题/移动端 390px 已验证，Safari/Firefox 未覆盖 | 低 | 后续加 WebKit/Firefox 冒烟；公开分享页跟随系统深色已由前端实现 |
| R6 | 前端 bundle 1.92MB（gzip 613KB），接近 chunk 警告阈值 | 低 | 后续 route-level code-splitting（不影响功能） |
| R7 | v0.2→v0.3 数据库迁移：启动幂等迁移已在全新库验证；旧 v0.2 真实数据目录升级演练未做 | 中 | 升级前备份；在目标机用 v0.2 数据副本跑一次升级（迁移为加列/建表，向后兼容） |
| R8 | ~~GHCR `v0.3.0` 镜像与 CI 状态待验证~~ | — | ✅ 已完成（§7）：CI success、tag 发布 success、匿名多架构校验通过 |

---

## 7. 发布验证结果（GitHub Actions + GHCR，已完成）

- **发布提交**：`7d80e6b`（`release: v0.3.0 — P1 complete (dark theme, trash/batch, storage, live Beta, vector search)`），
  annotated tag `v0.3.0` 已推送（tag 对象 `22c4c6d` → commit `7d80e6b`）。
- **GitHub Actions（全部 success）**：

| 工作流 | 事件 / 分支 | 运行 | 耗时 | 结果 |
|---|---|---|---|---|
| CI | push main | [40395016050](https://github.com/wpcwzy/echoic/actions/runs/40395016050) | 2m59s | ✅ Backend tests (pytest) + Frontend build (tsc + vite) |
| Docker Publish (GHCR) | push main | [40395015986](https://github.com/wpcwzy/echoic/actions/runs/40395015986) | 4m49s | ✅ Build & push multi-arch image |
| Docker Publish (GHCR) | push tag `v0.3.0` | [40395025393](https://github.com/wpcwzy/echoic/actions/runs/40395025393) | 5m51s | ✅ Build & push multi-arch image |

- **GHCR 匿名验证**（无登录，`Accept: application/vnd.oci.image.index.v1+json`）：

| tag | HTTP | manifest 类型 | digest |
|---|---|---|---|
| `v0.3.0` / `0.3.0` / `0.3` / `0` / `latest` / `sha-7d80e6b` | 200 | **OCI image index** | `sha256:0958bce23bc0439635655831f5e1cfe458a0d0443c8e56547307d38e991a3ea8`（全部 tag 指向同一 index） |

- **多架构清单**：`linux/amd64` + `linux/arm64` + 2 个 attestation manifest（provenance/SBOM，`unknown/unknown`）；
  amd64 子 manifest digest `sha256:8c5e9bbb5af7b17e051212fff0f5e2c9fe9cd524573382c724889f2350d84dea`，
  config digest `sha256:9638236468d961a4836d45b9cdea2101a7afe5d7592f709309d1028655308d8e`，构建时间 `2026-10-26T20:22:49Z`。
- **镜像 OCI labels**（从 config 校验）：`title=Echoic`、`version=v0.3.0`、
  `revision=7d80e6b0da41d2fce97b84b6d272efc39f3db634`、`licenses=MIT`、`source=https://github.com/wpcwzy/echoic`。
- **结论**：v0.3.0 发布流水线、多架构镜像与公开拉取均验证通过；`docker pull ghcr.io/wpcwzy/echoic:v0.3.0`
  在 amd64/arm64 主机可匿名使用。发布验证项全部完成，无异常。

## 8. 交付物与复现

| 文件 | 说明 |
|---|---|
| `e2e/smoke_v03.py` | v0.3 端到端冒烟（34 项：回收站/批量/实时 Mock/向量/storage + 回归） |
| `e2e/screenshots_v03.mjs` | v0.3 Playwright 截图 + 视觉巡检（深色/浅色/回收站/实时字幕/语义搜索/索引设置/移动端） |
| `e2e/REPORT-v0.3.md` | 本报告 |
| `e2e/last-run-v03.log` | v0.3 冒烟完整日志 |
| `e2e/screenshot-report-v03.json` | 截图/主题/诊断元数据 |
| `docs/screenshots/v0.3/*.png` | 8 张官方 UI 截图（1680×1050 + 390×844） |
| `scripts/verify.sh`（改） | 一键验证纳入 v0.1/v0.2/v0.3 三套冒烟与 `ECHOIC_LIVE_ENABLED/PROVIDER` 测试环境 |
| `e2e/last-run.log`、`e2e/last-run-v02.log`（改） | v0.1/v0.2 官方回归重跑日志 |

复现：

```bash
cd frontend && npm run build
ECHOIC_DATA_DIR=/tmp/echoic-e2e-v03 ECHOIC_CONSUME_DIR=/tmp/echoic-e2e-v03-consume \
ECHOIC_CONSUME_ENABLED=true ECHOIC_DEMO_SEED=true ECHOIC_EMBEDDED_WORKER=true \
ECHOIC_SECRET_KEY=e2e-secret-v03 ECHOIC_LIVE_ENABLED=true ECHOIC_LIVE_PROVIDER=mock \
ECHOIC_STATIC_DIR=$PWD/frontend/dist PORT=8100 ./scripts/run-local.sh --no-build

backend/.venv/bin/python e2e/smoke.py --base-url http://127.0.0.1:8100
backend/.venv/bin/python e2e/smoke_v02.py --base-url http://127.0.0.1:8100
backend/.venv/bin/python e2e/smoke_v03.py --base-url http://127.0.0.1:8100 --data-dir /tmp/echoic-e2e-v03
cd e2e && ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node screenshots_v03.mjs
./scripts/verify.sh
```
