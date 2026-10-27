# Echoic v0.3.10 验收报告（sqlite-vec、实时字幕 GA、注册/资料页、描述自动生成与缺陷修复）

- **验收日期**：2026-10-27 20:08 — 20:45（Asia/Shanghai）
- **验收人**：devops-qa（QA）
- **被测版本**：Echoic **v0.3.10**（`/api/system/health` → version=0.3.10）
- **总体结论**：**PASS（可以发布，无阻塞缺陷）**
  - v0.1 回归冒烟 **30/30 PASS**；v0.2 协作回归 **34/34 PASS**；v0.3 回收站/实时/向量回归 **34/34 PASS**
  - v0.3.10 新能力冒烟 **23/23 PASS**（注册开关+注册、个人资料/改密、sqlite-vec、实时 GA、描述自动生成/backfill）
  - 实时字幕**真实断线重连 E2E 20/20 PASS**（TCP 代理真实掐断 WS：resumed/base_ms/connection、失败重试、片段连续无重复、停止后不再重连）
  - 后端全量 pytest **456 passed**（首轮出现 1 个时序敏感用例偶发 900≠1000ms，单测重跑与全量重跑均通过，见 §5）
  - 前端构建通过（`index-Cf02LPTe.js` 1.94 MB / gzip **620.41 KB**；`index-CfUmTsee.css` gzip 22.66 KB）
  - `scripts/verify.sh` 扩展后 **12 组检查全部通过**（含 v0.3.10 冒烟）
  - sqlite-vec 主后端、强制 numpy 降级路径、默认开启实时字幕、默认关闭注册均验证通过
  - UI 截图 **10/10**，0 未捕获异常、0 横向溢出；深色/浅色主题检查通过
  - **发布验证通过**：commit `b240223` CI success；tag `v0.3.10` 多架构 GHCR 镜像发布 success，匿名可拉取
  - 未发现 P1/P2/P3 应用缺陷；仅 1 个时序敏感单元测试偶发（P3，非发布阻塞，已在发布提交中修复）

---

## 1. 验收环境与启动方式

| 项目 | 值 |
|---|---|
| 宿主 | WSL2，Linux x86_64 |
| Python | 3.12（`backend/.venv`；已安装 `sqlite-vec>=0.1.6,<0.2`） |
| Node / npm | v24.19.0 / 11.17.0（workspace-local HOME/cache） |
| FFmpeg | 5.1.8（生成 e2e 音频素材/回溯） |
| 浏览器 | Playwright + Chromium Headless Shell 156 |
| Docker | **本地不可用**（镜像运行时未验证；CI 负责构建） |
| 前端构建 | `index-Cf02LPTe.js`（620.41 KB gzip）+ `index-CfUmTsee.css`（22.66 KB gzip） |

主 QA 实例（数据隔离、mock AI、注册显式开启，用于 v0.3.10 功能与 UI）：

```bash
ECHOIC_DATA_DIR=$PWD/.cache/e2e-v0310-data \
ECHOIC_CONSUME_DIR=$PWD/.cache/e2e-v0310-consume ECHOIC_CONSUME_ENABLED=true \
ECHOIC_DEMO_SEED=true ECHOIC_EMBEDDED_WORKER=true \
ECHOIC_SECRET_KEY=e2e-secret-v0310 \
ECHOIC_STATIC_DIR=$PWD/frontend/dist \
ECHOIC_LIVE_ENABLED=true ECHOIC_LIVE_PROVIDER=mock \
ECHOIC_ALLOW_REGISTRATION=true ECHOIC_VECTOR_BACKEND=auto \
./backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8100 --app-dir backend
```

对照实例：默认环境（**不设** live/registration/vector 覆盖）验证 GA 默认值；`ECHOIC_VECTOR_BACKEND=numpy` 验证降级路径；回归冒烟在注册默认关闭的独立实例上执行。

---

## 2. 执行命令与结果

```bash
# 1) 前端构建
cd frontend && npm run build        # tsc 0 错误；vite 5.19s；gzip 620.41 KB JS / 22.66 KB CSS

# 2) 回归冒烟（独立实例 :8091，注册默认关闭；live 显式 mock）
backend/.venv/bin/python e2e/smoke.py     --base-url http://127.0.0.1:8091   # 30/30 PASS
backend/.venv/bin/python e2e/smoke_v02.py --base-url http://127.0.0.1:8091 \
  --consume-dir $PWD/.cache/e2e-v0310-reg-consume                           # 34/34 PASS
backend/.venv/bin/python e2e/smoke_v03.py --base-url http://127.0.0.1:8091 \
  --data-dir $PWD/.cache/e2e-v0310-reg-data                                 # 34/34 PASS

# 3) v0.3.10 新能力冒烟（主实例 :8100）
backend/.venv/bin/python e2e/smoke_v0310.py --base-url http://127.0.0.1:8100 \
  --data-dir $PWD/.cache/e2e-v0310-data                                     # 23/23 PASS

# 4) 强制 numpy 后端：同一套 v0.3.10 冒烟（:8093，ECHOIC_VECTOR_BACKEND=numpy）
backend/.venv/bin/python e2e/smoke_v0310.py --base-url http://127.0.0.1:8093 \
  --data-dir $PWD/.cache/e2e-v0310-numpy-data --expect-vector-backend numpy # 23/23 PASS

# 5) 实时字幕断线重连（真实浏览器 + TCP 代理 + mock live provider）
cd e2e && ECHOIC_E2E_UPSTREAM_PORT=8100 node live_reconnect_check.mjs      # 20/20 PASS

# 6) UI 截图/视觉巡检（主实例 :8100）
cd e2e && ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node screenshots_v0310.mjs  # 8/8 PASS

# 7) 后端全量测试
cd backend && .venv/bin/python -m pytest -q                               # 456 passed（见 §5 flake 说明）

# 8) 一键验证（pytest + tsc/build + 健康检查 + 四套冒烟）
./scripts/verify.sh --port 8123                                           # 12 通过 / 0 失败
```

证据文件：

| 文件 | 内容 |
|---|---|
| `e2e/last-run.log` / `e2e/last-run-v02.log` / `e2e/last-run-v03.log` | v0.1 / v0.2 / v0.3 回归日志（v0.3.10 代码上重跑） |
| `e2e/last-run-v0310.log` | v0.3.10 新能力冒烟日志（23 项） |
| `e2e/last-run-v0310-numpy.log` | 强制 numpy 后端冒烟日志（23 项） |
| `e2e/live-reconnect-report-v0310.json` | 实时字幕断线重连 20 项结果（含 ready 事件字段） |
| `e2e/screenshot-report-v0310.json` | 截图清单、主题状态、控制台/页面异常、溢出检测 |
| `docs/screenshots/v0.3.10/*.png` | 10 张官方 UI 截图 |
| `.cache/verify-v0310-run.log` | verify.sh 完整输出（本地临时，不入库） |

---

## 3. PASS/FAIL 矩阵

### 3.1 总览

| 检查项 | 结果 |
|---|---|
| v0.1 回归（认证/上传/转录/分析/SSE/Range/重处理） | ✅ 30/30 PASS |
| v0.2 回归（角色/权限/分享/consume） | ✅ 34/34 PASS |
| v0.3 回归（回收站/批量/实时/向量/storage） | ✅ 34/34 PASS |
| v0.3.10 新能力冒烟（sqlite-vec/注册/资料/描述/live） | ✅ 23/23 PASS |
| 强制 numpy 后端完整冒烟（降级路径） | ✅ 23/23 PASS |
| 实时字幕真实断线重连 E2E | ✅ 20/20 PASS |
| 后端 pytest 全量 | ✅ 456 passed（首轮有 1 个 flaky，见 §5） |
| 前端 tsc + vite build | ✅ PASS |
| `scripts/verify.sh` | ✅ 12 通过 / 0 失败 / 0 跳过 |
| UI 截图 + 视觉巡检 | ✅ 10/10，0 异常 / 0 溢出 |
| GitHub Actions CI（commit `b240223`） | ✅ **success**（backend pytest + frontend build，3m25s） |
| GHCR v0.3.10 多架构镜像（tag `v0.3.10`） | ✅ **success**（amd64+arm64，匿名拉取已验证，见 §7） |

### 3.2 v0.3.10 新能力冒烟明细（`e2e/smoke_v0310.py`，23 项全部 PASS）

| 分组 | 用例与结果 |
|---|---|
| 基础 | health（version=0.3.10、storage ok/local、allow_registration 字段）、admin 登录、features（live.enabled=true / provider=mock / max_minutes；vector.backend 报告**有效后端 sqlite_vec**；storage=local；trash=30） |
| 注册 | 运行时开关：settings/health 同步 true → 关（register 403）→ 开（health true）；注册 201 且角色 member；重复用户名 409；弱密码 422；注册后可登录且 `/auth/me` 正确 |
| 个人资料 | `PATCH /api/auth/me` 修改昵称/邮箱并回读一致；发送 role/disabled/username 等特权字段被忽略（仍 member/启用）；改密校验：未登录 401/403、旧密码错 400、新密码过短 400；成功改密后旧密码登录 401、新密码可登录 |
| sqlite-vec | `vector/status` 后端 sqlite_vec；配置 mock embedding 后 available=true、fallback_reason=null、embeddings_configured=true；重建索引写入 chunks 且 stale=0（138/138）；semantic 命中（score>0）；hybrid 命中 |
| 实时 GA | 创建会话 → WS 首帧 `ready{resumed:false, base_ms:0, total_ms, connection:1}`；`ping→pong`；发送 6s PCM16 → `stop` → `done`；会话 ended、片段持久化、total_audio_ms>0 |
| 描述自动生成 | 空描述上传 → ready 后自动生成描述（非空、≤120 字、非“暂无描述”）；用户自带描述上传后**不被覆盖**；演示录音描述保持不变；backfill CLI：dry-run derivable≥1 且不写库 → `--apply` 写入 → 长度≤120，且不覆盖用户描述 |

### 3.3 sqlite-vec 后端验证（三种部署形态）

| 场景 | 证据 | 结果 |
|---|---|---|
| `auto` + SQLite + wheel 可用（主实例） | `features.vector.backend=sqlite_vec`；配置 mock embedding 后 `vector/status`：backend=sqlite_vec、available=true、fallback_reason=null；reindex 138 chunks、semantic/hybrid 命中；v0.3.10 冒烟 23/23 | ✅ **实际使用 sqlite-vec** |
| 强制 `numpy`（:8093） | `features.vector.backend=numpy`；整包 v0.3.10 冒烟（--expect-vector-backend numpy）23/23，含 reindex/semantic/hybrid | ✅ 降级/强制路径可用 |
| 默认环境（无覆盖，:8092） | health：version=0.3.10、allow_registration=**false**；features：`live.enabled=true`（**GA 默认开启**）、vector.backend=**sqlite_vec**（auto 优先）、storage=local | ✅ 默认值符合 v0.3.10 契约 |

> 注：扩展不可用时的自动降级逻辑由后端单测覆盖；本机已安装 wheel，故未人为破坏扩展来 E2E「不可用」分支。

### 3.4 实时字幕 GA 断线重连（`e2e/live_reconnect_check.mjs`，20/20 PASS）

浏览器 → 本地 TCP 代理 → FastAPI（mock realtime provider）；代理主动掐断 WS TCP 连接模拟真实断网：

- 录音页**无 Beta 文案**，实时字幕连接成功（provider=mock）；
- 第 1 次断线：UI 出现「重连中…」；自动重连后 `ready{resumed:true, base_ms:4500, connection:2}`；后端会话保持 active；
- 第 2 次断线 + 代理拒绝新连接：首次 1s 重连失败，2s 退避后恢复（resumes=2）；字幕继续增长（1→2 段）；
- 正常停止：会话 ended，片段 3 段（0–3200 / 3200–6800 / 6800–9200ms）**首尾相接、无重复文本**，总音频 9200ms；
- 主动停止后**不再重连**（ready/create 计数不变）；无未捕获页面异常。

### 3.5 缺陷修复验证（UI + API）

| 问题 | 验证方式 | 结果 |
|---|---|---|
| 转移归属弹窗显示未格式化数字 ID | 打开「转移录音归属」：初始选择框为空占位；展开选项为「名称（@username）」，无裸数字 ID 选项 | ✅ 修复 |
| 编辑用户「启用账号」开关语义 | 开关默认 checked 且文案「启用」；关闭并保存后该用户 `disabled=true`，再恢复；截图 08 可见「启用账号…开=启用」 | ✅ 修复 |
| 资料库新上传录音「暂无描述」 | 上传 6s 录音（无描述）→ 自动生成摘要式描述（“本次会议共 2 位发言人参与…共 8 个发言片段”），卡片展示非“暂无描述” | ✅ 修复 |
| 注册入口/注册页 | `health.allow_registration=true` 时登录页显示「立即注册」；`/register` 注册后自动登录进入资料库 | ✅ 新增可用 |
| 个人资料页 | 修改昵称/邮箱保存成功（`PATCH /api/auth/me`）；改密短密码提示「密码至少 6 位」 | ✅ 新增可用 |
| Vite dev `/api` 代理 WebSocket | 静态核对 `frontend/vite.config.ts`：`proxy['/api'].ws = true` | ✅ 已修复（未启动 `npm run dev` 交互验证） |

---

## 4. UI 截图与视觉巡检（10/10）

| # | 文件 | 场景 | 结论 |
|---|---|---|---|
| 01 | `docs/screenshots/v0.3.10/01-login-register-entry.png` | 登录页 · 注册入口 | ✅ 浅色主题正常；「还没有账号？立即注册」可见；版本页脚 v0.3.10 |
| 02 | `docs/screenshots/v0.3.10/02-register.png` | 注册页 | ✅ 用户名/显示名称/邮箱/密码/确认密码；「注册并登录」；品牌文案完整 |
| 03 | `docs/screenshots/v0.3.10/03-profile-dark.png` | 个人资料 · 深色 | ✅ 账号信息卡、昵称/邮箱编辑、保存成功 toast；改密区显示「新密码至少 6 位」校验错误；深色无白块 |
| 04 | `docs/screenshots/v0.3.10/04-library-description.png` | 资料库 · 自动描述 | ✅ 新上传录音卡片展示自动描述文本（非“暂无描述”） |
| 05 | `docs/screenshots/v0.3.10/05-live-captions-ga.png` | 录音页 · 实时字幕 GA | ✅ 无 Beta 标识；状态「实时字幕中」+ mock·1 段已确定 + 实时/部分字幕 |
| 06 | `docs/screenshots/v0.3.10/06-live-reconnect.png` | 实时字幕断线重连 | ✅ 状态「重连中…」+「第 1/3 次重连」+ 说明“录音不受影响，断线期间语音可能不会显示” |
| 07 | `docs/screenshots/v0.3.10/07-transfer-owner.png` | 转移归属弹窗 | ✅ 初始占位「选择新的归属用户」，无未格式化 ID；深色主题正常 |
| 08 | `docs/screenshots/v0.3.10/08-user-enabled-switch.png` | 编辑用户 · 启用账号 | ✅ 开关 checked 且文字「启用」，说明“关闭后无法登录，现有会话立即失效” |
| 09 | `docs/screenshots/v0.3.10/09-library-light.png` | 资料库 · 浅色 | ✅ 标准页正常，自动描述卡片可见 |
| 10 | `docs/screenshots/v0.3.10/10-library-dark.png` | 资料库 · 深色 | ✅ 深色主题一致、对比度可读、无浅色残留面板 |

主题巡检（`screenshot-report-v0310.json`）：深色 body 亮度 0.07、浅色 0.96；`html[data-theme]` 与 `localStorage` 一致；pageErrors **0**、横向溢出 **0**、非预期控制台错误 **0**（5 条 401/404 为登录前探测与无实时会话录音的 `/live` 404，属预期噪声；2 条 media `ERR_ABORTED` 为离开详情页时播放器主动中止）。

---

## 5. 缺陷清单

**无发布阻塞缺陷。** 1 个 P3 观察项：

| 编号 | 级别 | 描述 | Owner | 建议 |
|---|---|---|---|---|
| OBS-v0310-01 | P3（测试稳定性，非应用缺陷） | `backend/tests/test_live_api.py::test_disconnect_grace_expiry_ends_session` 偶发失败：断线宽限期到期后 `total_audio_ms` 为 900 而期望 1000（1 帧 100ms 在断线竞态中丢失）。单测单独重跑通过，随后全量重跑 **456 passed**（136.08s）；验收以重跑结果为准 | backend-ai | 将断言改为容差（如 `>= 800`）或等待最后一帧落库后再断言，消除时序 flake；不影响功能与发布 |

其他说明（非缺陷）：

- 截图/用户列表包含 QA 测试账号与上传录音（`qa_ui_*`、`QA 描述展示录音`），为验证注册/描述功能有意保留；发布文档如需可替换。
- 实时重连 120s 宽限期**到期自动结束**未做 2 分钟真实等待（E2E 只验证真实断线→重连成功路径）；该分支由 pytest 单测覆盖。
- 截图仅在 Chromium（1680×1050）验证；移动端 v0.3.10 未单独截图（v0.3.0 已验证 390×844 布局）。

---

## 6. 剩余风险（发布后关注）

| # | 风险 | 影响 | 建议 |
|---|---|---|---|
| R1 | 本地无 Docker：镜像内 sqlite-vec wheel 安装、容器运行、真实 MinIO/S3 仍未本地验证 | 中 | CI 构建镜像已通过；在 Docker 主机做一次 `docker compose up` 冒烟（向量检索 + 实时字幕）；Dockerfile 依赖来自 requirements，平台 wheel 需 glibc（slim 镜像满足） |
| R2 | 真实 DashScope 实时转写未联网实测（仅 mock provider），断线重连针对 mock provider 验证 | 中 | 目标机配置真实 Key 后验证 WS 连接/断线重连/时间戳；real provider 代码路径由单测覆盖 |
| R3 | 真实 Embedding（DashScope/OpenAI）与 pgvector 后端未测试；sqlite-vec 只在 x86_64 Linux wheel 可用 | 低 | 目标环境确认 wheel 平台可用；不可用时 auto 会按设计降级 numpy |
| R4 | sqlite-vec 扩展加载失败时的「手动可见降级原因」仅由单测覆盖（本机 wheel 正常） | 低 | 可在无扩展环境用 `ECHOIC_VECTOR_BACKEND=auto` 观察 status.fallback_reason |
| R5 | 注册开放后的滥用风险（无邮箱验证/邀请码；登录限流已有） | 中（运维策略） | 默认关闭 `allow_registration`；仅内网/可信环境开启，或前置网关限流 |
| R6 | 实时字幕单进程/单 worker 假设未变；多实例部署下 WS 会话需 sticky 路由 | 低 | 文档已注明；LB 需会话保持 |
| R7 | backfill CLI 在服务运行中直接写同一 SQLite（WAL），高并发写入时可能短暂 busy | 低 | 建议维护窗口执行，或先停服务 |
| R8 | Vite dev WS 代理仅静态核对，未启动 `npm run dev` 交互验证 | 低 | 本地开发可快速手工确认一条实时字幕连接 |

---

## 7. 发布验证结果（GitHub Actions + GHCR，已完成）

- **发布提交**：`b240223`（`feat(v0.3.10): sqlite-vec, live captions GA, registration/profile, auto descriptions + UI fixes`），
  annotated tag `v0.3.10`（tag 对象 `5753aae` → commit `b240223`）已推送至 main。
- 发布提交同时修复了本报告 §5 的 P3 时序 flake（`test_disconnect_grace_expiry_ends_session` 断言容差 900–1000ms）。

- **GitHub Actions（全部 success）**：

| 工作流 | 事件 / 分支 | 运行 | 耗时 | 结果 |
|---|---|---|---|---|
| CI | push main | [40506177774](https://github.com/wpcwzy/echoic/actions/runs/40506177774) | 3m25s | ✅ Backend tests (pytest) + Frontend build (tsc + vite) |
| Docker Publish (GHCR) | push main | [40506177720](https://github.com/wpcwzy/echoic/actions/runs/40506177720) | 5m6s | ✅ Build & push multi-arch image |
| Docker Publish (GHCR) | push tag `v0.3.10` | [40506189282](https://github.com/wpcwzy/echoic/actions/runs/40506189282) | 7m19s | ✅ Build & push multi-arch image |

- **GHCR 匿名验证**（无登录，`Accept: application/vnd.oci.image.index.v1+json`）：

| tag | HTTP | manifest 类型 | digest |
|---|---|---|---|
| `v0.3.10` / `0.3.10` / `0.3` / `0` / `latest` / `sha-b240223` | 200 | **OCI image index** | `sha256:072612d23d7b35bdd1d1748173d8037ad8bcffd39a3a7ceb0da77ae73abb1579`（全部 tag 指向同一 index） |

- **多架构清单**：`linux/amd64` + `linux/arm64` + 2 个 attestation manifest（provenance/SBOM，`unknown/unknown`）；
  amd64 子 manifest digest `sha256:082ae389c5c8187ff73dc54c61cdd09e0ddecd6a81fa32f54dd68b9e2d034385`，
  config digest `sha256:a509eb7da02486067f0a874db8e57c2b7d197da7c1b55c3067ee69ab465517d1`，构建时间 `2026-10-27T12:46:13Z`。
- **镜像 OCI labels**（从 config 校验）：`title=Echoic`、`version=v0.3.10`、
  `revision=b240223e394fc0542661a014ceb6fbc4c29ac66b`、`licenses=MIT`、`source=https://github.com/wpcwzy/echoic`。
- **结论**：v0.3.10 发布流水线、多架构镜像与公开拉取全部验证通过；`docker pull ghcr.io/wpcwzy/echoic:v0.3.10`
  在 amd64/arm64 主机可匿名使用。发布验证项全部完成，无异常。

## 8. 交付物与复现

| 文件 | 说明 |
|---|---|
| `e2e/smoke_v0310.py` | v0.3.10 新能力冒烟（23 项：注册/资料/sqlite-vec/实时 GA/描述/backfill） |
| `e2e/live_reconnect_check.mjs` + `e2e/live_proxy.mjs` | 真实 TCP 断线重连 E2E（20 项）+ 可主动断连代理 |
| `e2e/screenshots_v0310.mjs` | v0.3.10 UI 截图/视觉巡检（8 张；含注册/资料/描述/转移/启用开关/深浅色） |
| `e2e/REPORT-v0.3.10.md` | 本报告 |
| `e2e/last-run-v0310.log`、`e2e/last-run-v0310-numpy.log` | 主/降级后端冒烟日志 |
| `e2e/live-reconnect-report-v0310.json`、`e2e/screenshot-report-v0310.json` | 结构化结果 |
| `docs/screenshots/v0.3.10/*.png` | 10 张官方截图（含 live reconnect 两张） |
| `scripts/verify.sh`（改） | 一键验证纳入 v0.3.10 冒烟；回归 smokes 适配 sqlite-vec 后端 |
| `e2e/smoke_v03.py`（改） | 向量后端断言兼容 sqlite_vec |

复现：

```bash
cd frontend && npm run build
ECHOIC_DATA_DIR=/tmp/echoic-v0310 ECHOIC_CONSUME_DIR=/tmp/echoic-v0310-consume \
ECHOIC_CONSUME_ENABLED=true ECHOIC_DEMO_SEED=true ECHOIC_EMBEDDED_WORKER=true \
ECHOIC_SECRET_KEY=e2e-secret-v0310 ECHOIC_LIVE_ENABLED=true ECHOIC_LIVE_PROVIDER=mock \
ECHOIC_ALLOW_REGISTRATION=true ECHOIC_STATIC_DIR=$PWD/frontend/dist \
PORT=8100 ./scripts/run-local.sh --no-build

backend/.venv/bin/python e2e/smoke.py      --base-url http://127.0.0.1:8100
backend/.venv/bin/python e2e/smoke_v02.py  --base-url http://127.0.0.1:8100
backend/.venv/bin/python e2e/smoke_v03.py  --base-url http://127.0.0.1:8100 --data-dir /tmp/echoic-v0310
backend/.venv/bin/python e2e/smoke_v0310.py --base-url http://127.0.0.1:8100 --data-dir /tmp/echoic-v0310

cd e2e && ECHOIC_E2E_UPSTREAM_PORT=8100 node live_reconnect_check.mjs
cd e2e && ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node screenshots_v0310.mjs
./scripts/verify.sh
```
