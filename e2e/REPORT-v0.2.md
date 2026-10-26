# Echoic v0.2 验收报告（P1 batch 1：多用户 / 角色、分享链接、consume 导入）

- **验收日期**：2026-10-27 01:47 — 02:00（Asia/Shanghai）
- **验收人**：devops-qa（QA）
- **被测版本**：Echoic **v0.2.0**（`/api/system/health` → version=0.2.0；后端 commit 55811dd + 前端 commit a0dda7c）
- **总体结论**：**PASS**
  - v0.1 回归冒烟 **30/30 PASS**
  - v0.2 协作功能冒烟 **34/34 PASS**（角色/权限、分享链接、consume 导入、用户管理）
  - 后端测试 **179 passed**（75.8s）
  - 前端构建通过（`index-Dft3D9nz.js` 1.87 MB / gzip 599 KB；CSS 102 KB / gzip 18.5 KB）
  - UI 截图 **9 张**，0 未捕获异常、0 横向溢出；v0.2 新页面无 P1/P2 缺陷
  - 一键验证 `scripts/verify.sh` **PASS**（10 组检查，含 v0.1 + v0.2 冒烟）
  - GitHub Actions（main `231a9ab`）：CI **success**、多架构镜像发布 **success**；GHCR 匿名拉取已验证

---

## 1. 验收环境

| 项目 | 值 |
|---|---|
| 宿主 | WSL2，Linux 6.18.33.2-microsoft-standard-WSL2，x86_64 |
| Python / 后端依赖 | 3.12.7（`backend/.venv`，httpx 0.28.1） |
| Node / npm | v24.19.0 / 11.17.0（workspace 内 HOME/cache，npmmirror 源） |
| FFmpeg | 5.1.8-0+deb12u1 |
| 浏览器 | Playwright + Chromium Headless Shell 156（`.cache/ms-playwright`） |
| Docker | **本地不可用**（仅静态/YAML 校验；官方镜像由 GitHub Actions 真实构建发布） |
| 运行模式 | 生产式单进程 uvicorn + 内嵌 worker；mock ASR/LLM；demo seed 开；端口 `:8100` |

实际运行参数（与任务要求一致，仅隔离目录位置有说明）：

```bash
ECHOIC_DATA_DIR=/home/wpcwzy/echoic/.cache/e2e-v02-data   # 见下方说明
ECHOIC_CONSUME_ENABLED=true
ECHOIC_CONSUME_DIR=/home/wpcwzy/echoic/.cache/e2e-v02-consume  # 见下方说明
ECHOIC_CONSUME_INTERVAL_SEC=15
ECHOIC_DEMO_SEED=true
ECHOIC_EMBEDDED_WORKER=true
ECHOIC_SECRET_KEY=e2e-secret-v02
ECHOIC_STATIC_DIR=/home/wpcwzy/echoic/frontend/dist
PORT=8100
./scripts/run-local.sh --no-build
```

> **隔离目录说明**：本执行沙箱每个 shell 调用拥有独立 `/tmp` mount。为了让「consume 投放文件」与
> 「检查 consume/failed 落盘」在服务与测试进程之间可见，数据/consume 目录改用仓库内
> `.cache/e2e-v02-data`、`.cache/e2e-v02-consume`（均为 gitignore 的隔离目录），其余变量、
> 端口与生产式启动方式完全按任务要求。普通机器上可直接使用 `/tmp/echoic-e2e-v02*`。

前端产物（本次验收实际使用）：`frontend/dist/index.html` + `assets/index-Dft3D9nz.js` + `index-B3yjy2QU.css`。

---

## 2. 执行命令与结果

```bash
# 1) 前端生产构建（tsc + vite）→ 4.83s，green
cd frontend && npm run build

# 2) 生产式服务（:8100，fresh data dir，mock 管线 + 内嵌 worker + consume）
ECHOIC_DATA_DIR=... ECHOIC_CONSUME_DIR=... ECHOIC_DEMO_SEED=true \
ECHOIC_EMBEDDED_WORKER=true ECHOIC_SECRET_KEY=e2e-secret-v02 \
ECHOIC_STATIC_DIR=$PWD/frontend/dist PORT=8100 ./scripts/run-local.sh --no-build
# → GET /api/system/health 200（version=0.2.0, db/media/ffmpeg/worker=ok, demo_mode=true）

# 3) v0.1 回归冒烟（必须保持全绿）
backend/.venv/bin/python e2e/smoke.py --base-url http://127.0.0.1:8100 --process-timeout 180
# → 30/30 PASS，日志 e2e/last-run.log

# 4) v0.2 协作冒烟
backend/.venv/bin/python e2e/smoke_v02.py --base-url http://127.0.0.1:8100 \
  --consume-dir $PWD/.cache/e2e-v02-consume
# → 34/34 PASS，日志 e2e/last-run-v02.log

# 5) UI 截图 + 视觉巡检（Playwright，1680x1050 + 公开页移动端 390x844）
cd e2e && ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node screenshots_v02.mjs
# → 8/8 关键截图 PASS，0 页面异常、0 溢出；另补 1 张公开页移动端截图

# 6) 后端全量测试
cd backend && .venv/bin/python -m pytest -q
# → 179 passed in 75.80s

# 7) 一键验证（pytest + 前端 typecheck/build + 健康检查 + v0.1/v0.2 冒烟）
./scripts/verify.sh --port 8123
# → 验证结果：10 通过 / 0 失败 / 0 跳过；全部检查通过

# 8) GitHub Actions / GHCR 状态（只读）
gh run list --branch main --limit 4
# → CI success（2m15s, commit 231a9ab）；Docker Publish success（27s，缓存命中）
```

证据文件：

| 文件 | 内容 |
|---|---|
| `e2e/last-run.log` | v0.1 回归冒烟完整日志（30 项） |
| `e2e/last-run-v02.log` | v0.2 协作冒烟完整日志（34 项） |
| `e2e/screenshot-report-v02.json` | 截图清单、控制台错误、页面异常、失败请求、溢出检测 |
| `docs/screenshots/v0.2/*.png` | v0.2 UI 截图（1680×1050，另含 390×844 移动端） |

---

## 3. PASS/FAIL 矩阵

### 3.1 总览

| 检查项 | 结果 |
|---|---|
| v0.1 回归冒烟（`e2e/smoke.py`） | ✅ 30/30 PASS |
| v0.2 协作冒烟（`e2e/smoke_v02.py`） | ✅ 34/34 PASS |
| 后端 pytest（全量） | ✅ 179 passed |
| 前端 typecheck + build | ✅ PASS |
| `scripts/verify.sh` 一键验证 | ✅ 10 通过 / 0 失败 |
| UI 截图 + 视觉巡检 | ✅ 9 张，0 异常 / 0 溢出 |
| GitHub Actions CI（main `231a9ab`） | ✅ success（backend + frontend） |
| GHCR 多架构镜像发布（main `231a9ab`） | ✅ success（linux/amd64 + arm64） |

### 3.2 v0.2 协作冒烟（`e2e/smoke_v02.py`，34 项，全部 PASS）

| 分组 | 用例 | 关键证据 |
|---|---|---|
| 基础 | health / admin 登录 / system.info / demo 库 | version=0.2.0，demo 4 条录音 |
| 用户与角色 | users.list-admin / create-member / duplicate-409 / disabled-login-403 / register-gated / last-admin-protection / member.admin-api-403 | 创建 member1/member2 并登录；重复用户名 409、短密码 422；停用账号登录 403；注册开关关闭 403 → 开启后 201(member) → 已恢复关闭；不能降级/删除最后管理员；member 访问 8 个 admin 接口全部 403，GET /api/tags 200 正常 |
| 权限矩阵 | admin-upload / reassign-owner / member-upload / member-isolation / member-own-access / member-owner-protection / search-dashboard-scoped | admin 上传 → 转移 owner 给 member1；member 列表仅自己的 2 条；他人录音 detail/transcript/insights/media/chat/shares 全 404；本人 detail/transcript/insights/media(Range 206)/chat 可读、可改标题；member 转归属 403；admin total=6 / member total=2，搜索与 dashboard 按用户隔离 |
| 分享链接 | create-open / public-metadata / public-content / download-gate / include-flags / password-unlock / unlock-rate-limit / expiry / revoke / member-scope | 匿名元数据 200 且 view_count 1→2、无 owner 泄露；公开 transcript/insights/media(Range) 可读；下载门禁 403/允许时 200 attachment；include 关闭 → 403；密码分享未解锁 401、错误密码 401、正确解锁 200 + 路径级 HttpOnly Cookie（Max-Age=7200/SameSite=Lax），解锁后内容可读；限速序列 `[401×5, 429]`；过去时间创建 400、2s 有效期分享过期后 404；撤销后 404；member 仅能分享自己的录音 |
| consume 导入 | status / import-ready / duplicate / unsupported-skip / invalid-failed | 扫描端点结果含 scanned/imported/duplicates/failed；投放 10s wav → `{scanned:1, imported:1}` → 录音 ready（source=consume，原文件移出）；同内容再投 → `duplicates:1` 且录音总数不变；.txt 跳过；非法 wav → `failed:1` 并移入 `consume/failed/`；system.info consume 字段完整 |
| 回归 | demo-chat-sse | 演示录音 SSE 问答 start/delta/done 正常 |

### 3.3 UI 截图与视觉巡检（Playwright）

| # | 文件 | 场景 | 结果 | 巡检结论 |
|---|---|---|---|---|
| 01 | `docs/screenshots/v0.2/01-users.png` | 管理后台 · 用户管理 | ✅ | 表格（用户/角色/状态/录音/分享/最近登录/创建时间/操作）、搜索与角色筛选、分页正常；角色徽标、停用状态清晰 |
| 01b | `docs/screenshots/v0.2/01b-user-create-dialog.png` | 新建用户对话框 | ✅ | 用户名/显示名称/邮箱/角色/初始密码/确认密码布局正常，校验提示位齐全 |
| 02 | `docs/screenshots/v0.2/02-share-dialog.png` | 详情页 · 分享弹窗 | ✅ | 创建区（含转录/AI纪要/允许下载开关、密码、有效期预设 1h/24h/7d/30d/永久/自定义）+ 已创建链接列表（复制/撤销/访问数/密码标记）完整；背景 AI 面板为空态 |
| 03 | `docs/screenshots/v0.2/03-public-share.png` | 公开分享页（未登录） | ✅ | 品牌头部 + 「无需登录」入口；标题/元数据/播放器/只读页签（转写、AI纪要、观点树、章节）+「只读分享」标记；未重定向 /login |
| 04 | `docs/screenshots/v0.2/04-public-password.png` | 公开分享 · 密码页 | ✅ | 居中密码卡（加密锁图标 + 说明 + 输入 + 解锁按钮 + 2 小时提示），底部「由 Echoic 提供 · 自托管」页脚 |
| 05 | `docs/screenshots/v0.2/05-consume-settings.png` | 设置 · 系统 | ✅ | 系统默认值（上传上限/语言/自动处理/开放注册开关）+ 目录自动导入卡片（监控目录、扫描间隔、待处理/失败文件数、最近扫描结果、立即扫描/刷新状态）；toast「扫描完成：导入 0，重复 1，失败 0」 |
| 06 | `docs/screenshots/v0.2/06-member-library.png` | 成员视角资料库 | ✅ | 无「用户管理/设置」导航；仅自己的录音；空态文案「还没有你自己的录音」+ 成员权限说明；直接访问 `/users` 被重定向到 `/` |
| 07 | `docs/screenshots/v0.2/07-public-expired.png` | 公开分享 · 失效页 | ✅ | 「分享链接不存在或已失效」友好提示 + 页脚；未重定向 /login |
| 08 | `docs/screenshots/v0.2/08-public-mobile.png` | 公开分享 · 移动端 390×844 | ✅ | 单列响应式，播放器/页签/转写正常，无横向溢出、无页面异常 |

诊断汇总：页面未捕获异常 **0**；横向溢出 **0**；控制台错误 **7 条均为预期 401/403 鉴权噪声**（登录前 `/api/auth/me`、密码分享未解锁时的公开接口）；失败请求 3 条为离开页面时播放器 `media` 请求主动 abort（预期）。

---

## 4. 缺陷清单

### 4.1 已修复（验收期间）

| 编号 | 严重级 | 描述 | 归属 | 状态 |
|---|---|---|---|---|
| DEF-v02-CI-01 | **P1（工程基建，不阻塞功能）** | GitHub Actions 首个 CI run 中 backend job 卡在 `apt-get install ffmpeg` >20min。第一版修复（条件检测 + shell timeout，commit c4d3bba）后，commit a0dda7c 的 CI 仍在 `Ensure ffmpeg` 步骤失败（runner 镜像实际缺少可用 ffmpeg/ffprobe，apt 回退也不可用）。 | devops / Lead | ✅ **已修复**：Lead commit `231a9ab` 改用预编译静态 `FedericoCarboni/setup-ffmpeg@v3` action + `ffmpeg -version/ffprobe -version` 校验。修复后 main CI **2m15s success**，镜像发布 27s（全缓存命中）success |

### 4.2 v0.2 应用功能缺陷

**未发现 P1/P2 级应用缺陷**；角色权限、分享链接、公开分享页、consume 导入均通过 API 与 UI 双重验收。

### 4.3 观察项（P3 / 可选优化，不作为发布阻塞）

| 编号 | 描述 | Owner | 建议 |
|---|---|---|---|
| OBS-v02-01 | 分享解锁 Cookie 设置 `Secure=False`（当前 HTTP 本地验收符合预期）；HTTPS 反代下建议按 `X-Forwarded-Proto` 置 Secure | backend | 生产 HTTPS 部署前加固（P3 安全） |
| OBS-v02-02 | consume 对重复文件采取「保留原位」策略（符合设计），重复文件会持续计入 `pending_files` | backend/ops | 文档说明清理方式，或后续支持自动移入 `consume/duplicates/` |
| OBS-v02-03 | 分享解锁限速为进程内内存计数（单进程 OK；多 worker/重启后重置） | backend | 多实例部署时改用共享存储或网关限流（P3） |
| OBS-v02-04 | 截图环境包含 QA 创建的账号与 consume 遗留计数（属测试数据，非缺陷） | QA | 正式文档截图可在干净实例重拍（当前截图真实反映功能） |
| OBS-v02-05 | 公开分享页页脚在 1680×1050 首屏下方需滚动可见（移动端正常） | frontend | 可选：首屏紧凑布局（P3 观感） |

> v0.1 已修复缺陷（设置 Diarization 开关拉伸、思维导图缩放/空白 MiniMap、资料库日期不一致）在 v0.2 回归中保持正常，无复现。

---

## 5. CI/CD 与镜像状态（main `231a9ab`）

| 项目 | 状态 |
|---|---|
| CI（backend pytest + frontend tsc/vite） | ✅ success（2m15s；backend 2m10s / frontend 33s） |
| Docker Publish（多架构） | ✅ success（27s，BuildKit GHA 全缓存命中） |
| GHCR 镜像标签 | `latest`、`sha-231a9ab`、`sha-a0dda7c` 均匿名可拉取（manifest HTTP 200） |
| 多架构清单 | `application/vnd.oci.image.index.v1+json`：**linux/amd64 + linux/arm64**（另含 provenance attestation 条目） |
| 语义化版本标签 | 尚未生成（需推送 git tag `v0.2.0`，发布流水线将自动产出 `v0.2.0` / `0.2` / `0`） |
| Dependabot | 已按平台策略忽略 semver-major（commit da06938），仅保留 minor/patch 每周分组 PR |
| ffmpeg 供给 | 采用静态 action + 版本校验，CI 从 3m+ 稳定至 ~2m15s |

发布前建议：`git tag -a v0.2.0 -m "Echoic v0.2.0" && git push origin v0.2.0`，随后验证 `ghcr.io/wpcwzy/echoic:v0.2.0`。

---

## 6. 剩余风险

| # | 风险 | 影响 | 建议 |
|---|---|---|---|
| R1 | 本地无 Docker CLI，容器运行（卷权限/健康检查/entrypoint）未在真实 Docker 主机验证 | 中 | 在具备 Docker 的机器执行 `docker compose pull && docker compose up -d` 跑 §2 冒烟；官方镜像已由 CI 真实构建发布 |
| R2 | 真实 AI Provider 未联网验证（仅 mock；DashScope/OpenAI 请求形状由单元测试 respx 覆盖） | 中 | 用真实 Key 在设置页「测试连接」并上传真实音频端到端验证 |
| R3 | v0.1 → v0.2 升级迁移只在全新数据库验证；旧库升级（migrations）未用真实 v0.1 数据回归 | 中 | 复制真实 v0.1 数据目录做一次升级演练（v0.2 已含 migrations 单测） |
| R4 | 公开分享安全边界：媒体/元数据接口无限速（token 32 字符随机、密码分享有限速 5/min） | 低 | 公网部署建议网关层限速 + HTTPS；分享为显式创建、可随时撤销 |
| R5 | consume 依赖轮询（默认 15s）且要求文件稳定；超大文件/跨设备移动行为需真实环境验证 | 低 | 大文件投放后观察 `system.info.consume.last_scan`；跨设备回退为复制+删除 |
| R6 | 前端主包 1.87MB（gzip 599KB），低端网络首屏偏大 | 低 | 后续做路由级 code-splitting（P2 性能优化项） |
| R7 | UI 仅 Chromium 验证；未覆盖 Firefox/Safari 与移动端全页面矩阵 | 低 | 增加 WebKit 冒烟；移动端目前验证了公开分享页 390px |
| R8 | PostgreSQL profile、真实反向代理（HTTPS/Cookie Secure）未在 v0.2 回归 | 低 | 部署目标机验证（见 docs/DEPLOYMENT.md） |

---

## 7. 交付物与复现

本次 task-10 交付（未提交，交给 Lead git 管理）：

| 文件 | 说明 |
|---|---|
| `e2e/smoke_v02.py` | v0.2 协作 E2E（34 项：角色/权限/分享/consume/回归） |
| `e2e/screenshots_v02.mjs` | v0.2 Playwright 截图 + 视觉巡检（8 张 + 移动端） |
| `e2e/REPORT-v0.2.md` | 本报告 |
| `e2e/last-run-v02.log` | v0.2 冒烟完整日志 |
| `e2e/screenshot-report-v02.json` | 截图/诊断元数据 |
| `docs/screenshots/v0.2/*.png` | 9 张 v0.2 UI 截图 |
| `scripts/verify.sh`（改） | 一键验证新增 v0.2 冒烟与 consume 隔离目录 |
| `e2e/last-run.log`（改） | v0.1 回归重跑（30/30） |

复现：

```bash
cd frontend && npm run build
ECHOIC_DATA_DIR=/tmp/echoic-e2e-v02 ECHOIC_CONSUME_DIR=/tmp/echoic-e2e-v02-consume \
ECHOIC_CONSUME_ENABLED=true ECHOIC_DEMO_SEED=true ECHOIC_EMBEDDED_WORKER=true \
ECHOIC_SECRET_KEY=e2e-secret-v02 ECHOIC_STATIC_DIR=$PWD/frontend/dist PORT=8100 \
  ./scripts/run-local.sh --no-build

backend/.venv/bin/python e2e/smoke.py --base-url http://127.0.0.1:8100
backend/.venv/bin/python e2e/smoke_v02.py --base-url http://127.0.0.1:8100 \
  --consume-dir /tmp/echoic-e2e-v02-consume
cd e2e && ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node screenshots_v02.mjs
./scripts/verify.sh
```
