# Echoic 部署指南

> 本文面向**自托管部署**：用 Docker Compose 一条命令跑起「录音 → 转录 → AI 纪要 → 观点树 / 思维导图 → RAG 问答」完整链路。
> 开发调试见 [README 开发指南](../README.md#开发指南)；接口与架构分别见 [API.md](API.md)、[ARCHITECTURE.md](ARCHITECTURE.md)。
>
> **v0.3.0 新增（均为可选项，默认行为与 v0.2 一致）**：对象存储（S3/MinIO，§10）、实时字幕（§11）、
> 向量 / 混合检索（§12）、回收站与批量操作（§13）。默认仍为单容器 + SQLite + 本地存储，零外部依赖可运行。

---

## 1. 部署形态与端口

```
浏览器 ──HTTPS──▶ 反向代理(可选 Nginx/Caddy) ──▶ Echoic 容器 :8000
                                                  ├── FastAPI /api/*，SSE 流式问答
                                                  └── 前端静态资源（同源，无需单独 nginx）
                                            卷 echoic-data:/data（local 存储模式，默认）
                                              ├── echoic.db（SQLite，默认）
                                              ├── media/{uuid}/original.* · playback.m4a
                                              ├── consume/（v0.2 自动导入监听目录）
                                              └── secret.key（JWT/密钥加密）
                                    可选：对象存储（v0.3.0，S3/MinIO）──▶ media/{uuid}/* 外置到 bucket
```

- **单容器**：FastAPI 同时提供 API 与前端，无 Redis / Celery 依赖；
- **无外部服务**也能完整运行（默认 mock AI + SQLite + 本地存储 + 内嵌任务线程）；
- **可选扩展**（v0.3.0，均不改变默认部署）：对象存储 `--profile minio`、PostgreSQL `--profile postgres`、独立 worker `--profile worker`；
- 真实 AI 服务只需**出站 HTTPS** 访问用户配置的服务商（阿里云百炼 / OpenAI 兼容端点 / Whisper 兼容服务）；实时字幕（开启时）与语义检索也走同一出站网络。

| 项目 | 默认值 | 说明 |
|---|---|---|
| 容器内端口 | `8000` | 由 uvicorn 监听 |
| 宿主机端口 | `8000` | compose 中 `${ECHOIC_HTTP_PORT:-8000}` 可改 |
| 数据目录 | `/data`（卷 `echoic-data`） | SQLite + 密钥 + consume；local 模式还包含媒体 |
| 媒体存储（v0.3.0） | `local`（默认） | 可切换为 S3 兼容存储：`ECHOIC_STORAGE_BACKEND=s3`（MinIO compose profile 或外部 S3/MinIO） |
| 镜像 | `ghcr.io/wpcwzy/echoic:${ECHOIC_IMAGE_TAG:-latest}` | CI 发布的**多架构镜像**（linux/amd64 + arm64），无需本地构建 |

---

## 2. 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Docker Engine | ≥ 24（推荐 25+） | `docker --version` |
| Docker Compose | v2 插件（`docker compose`） | 需要支持 BuildKit（v2 默认开启） |
| 磁盘 | ≥ 10GB 起 | 媒体文件按录音时长增长；1 小时 mp3 约 60MB |
| 内存 | ≥ 2GB（推荐 4GB） | numpy / 转码 / 前端构建 |
| CPU | 2 核起 | ffmpeg 转码为 CPU 操作；无需 GPU |
| 网络 | 出站 HTTPS | 仅接入真实 AI 服务时需要 |

> **镜像部署无需构建**：只需能访问 `ghcr.io`；运行阶段不需要联网（mock 模式）。
> 仅「从源码构建」（§3.1）才需要访问 npm / PyPI（或配置国内镜像，见 §3.3）。
> **v0.3.0 可选组件**：MinIO 对象存储、PostgreSQL、独立 worker 均通过 compose profile 启用，
> 默认单容器 / SQLite / 本地存储行为不变，无需额外系统依赖。

---

## 3. 快速开始（镜像优先，推荐）

```bash
# 1. 获取部署文件（推荐 git clone，便于后续升级）
git clone <repo-url> echoic && cd echoic

# 2. 准备环境变量（至少修改管理员口令）
cp .env.example .env
vi .env        # 修改 ECHOIC_ADMIN_PASSWORD；ECHOIC_SECRET_KEY 可留空自动生成

# 3. 拉取预构建多架构镜像并启动（无需本地构建）
docker compose pull
docker compose up -d

# 4. 查看状态
docker compose ps
docker compose logs -f echoic
```

启动成功的标志（`docker compose ps` 中 `echoic` 为 `healthy`，或日志出现 `starting Echoic v0.2.0`）：

```bash
curl -fsS http://localhost:8000/api/system/health
# {"status":"ok","version":"0.2.0","db":"ok","media":"ok","ffmpeg":"ok","worker":"ok","demo_mode":true,...}
```

浏览器打开 **http://localhost:8000**。

> 生产环境建议在 `.env` 中固定版本：`ECHOIC_IMAGE_TAG=v0.2.0`（默认 `latest` 会跟随 main 分支构建，升级前请先看 CHANGELOG）。

### 3.1 从源码构建（可选）

适用于开发、定制或无法访问 GHCR 的离线环境：

```bash
# 本地构建镜像（标签 echoic:local，不覆盖/不依赖官方镜像标签）；首次约 3~8 分钟
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

构建参数（可选，写入 `.env` 即可被 build override 使用）：

| 变量 | 说明 |
|---|---|
| `NPM_REGISTRY` | npm 源；默认 `https://registry.npmjs.org`（国内可设 `https://registry.npmmirror.com`） |
| `PIP_INDEX_URL` | PyPI 源；默认官方，国内可设清华源 |
| `ECHOIC_LOCAL_IMAGE` | 本地镜像标签，默认 `echoic:local` |

也可以直接 `docker build`：

```bash
docker build --build-arg NPM_REGISTRY=https://registry.npmjs.org -t echoic:local .
```

### 3.2 镜像标签与 GHCR 可见性

镜像地址：`ghcr.io/wpcwzy/echoic`（多架构 `linux/amd64` + `linux/arm64`，由 GitHub Actions 自动发布）。

| 标签 | 含义 |
|---|---|
| `latest` | main 分支最新构建（默认，适合体验/跟进最新功能） |
| `v0.2.0` / `0.2` / `0` | 推送 git tag 时自动生成的语义化版本（生产建议固定，如 `v0.2.0`） |
| `sha-xxxxxxx` | 每次构建的提交标签（可复现 / 回滚） |

- **公开包（当前状态，已验证）**：GHCR 包已设为 Public，匿名 `docker compose pull` / registry manifest 访问均验证通过，无需登录；
- **若包被改为私有**（匿名拉取返回 403）：先登录再拉取：

  ```bash
  echo "<GitHub PAT，至少 read:packages 权限>" | docker login ghcr.io -u <GitHub用户名> --password-stdin
  docker compose pull
  ```

  一次性改为公开：<https://github.com/users/wpcwzy/packages/container/echoic/settings> → Danger Zone → Change visibility → **Public**；
  （发布流水线末尾也会用 `GITHUB_TOKEN` 尝试自动设为 public，失败不影响发布。）
- 验证多架构清单：`docker buildx imagetools inspect ghcr.io/wpcwzy/echoic:latest`。

### 3.3 国内网络加速（仅源码构建需要）

镜像部署只依赖 `ghcr.io`（可用代理/镜像加速）；以下仅适用于 `docker-compose.build.yml` 源码构建：

```bash
# 方式一：写入 .env，compose build override 会作为 build args 传入
echo 'NPM_REGISTRY=https://registry.npmmirror.com' >> .env
echo 'PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple' >> .env
docker compose -f docker-compose.yml -f docker-compose.build.yml build --no-cache echoic

# 方式二：直接 docker build 传参
docker build \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t echoic:local .
```

> **构建期网络说明**：仓库中的 `frontend/package-lock.json` 目前由国内镜像源
> `registry.npmmirror.com` 生成（`resolved` 字段指向该域名）。
> Dockerfile 已设置 `replace-registry-host=always`：
> - 国内构建：默认即可（或显式传 `NPM_REGISTRY=https://registry.npmmirror.com`）；
> - 海外/离线构建：传 `NPM_REGISTRY=https://registry.npmjs.org` 即可让 npm 按配置源重新下载；
> - 如需彻底摆脱锁文件中的镜像源，可在**本地**重新生成 lockfile 后提交：
>   `cd frontend && rm -rf node_modules package-lock.json && npm install --registry=https://registry.npmjs.org`。
>
> 另：开发机上的 `frontend/.npmrc` 可能含本机绝对路径（cache/logs-dir），
> 该文件已被 `.dockerignore` 排除；Dockerfile 在构建阶段会生成只含容器内路径的项目级 `.npmrc`。

### 3.4 可选：启用 MinIO 对象存储（v0.3.0）

```bash
# 1) .env 中启用 S3 后端（使用 compose 内置 MinIO）
#    ECHOIC_STORAGE_BACKEND=s3
#    ECHOIC_S3_ENDPOINT=http://minio:9000
#    ECHOIC_S3_ACCESS_KEY=echoic                    # 与 ECHOIC_MINIO_ROOT_USER 一致
#    ECHOIC_S3_SECRET_KEY=change-me-echoic-minio    # 与 ECHOIC_MINIO_ROOT_PASSWORD 一致（生产必改）
#    ECHOIC_S3_BUCKET=echoic
#    ECHOIC_S3_PATH_STYLE=true

# 2) 启动 minio + 一次性 bucket 初始化 + echoic
docker compose --profile minio up -d

# 3) 确认状态（minio 应为 healthy；minio-init 退出码 0 表示 bucket 已创建）
docker compose ps
docker compose logs minio-init
curl -fsS http://localhost:8000/api/system/health   # storage 状态应为 ok
```

- MinIO 控制台：`http://localhost:9001`（账号见 `ECHOIC_MINIO_ROOT_USER/PASSWORD`）；
- 已有本地媒体迁移到 S3：见 §10 对象存储中的 `storage_migrate` 命令；
- MinIO 官方社区镜像已停止公开分发（2026-10 状态），compose 默认使用活跃维护的
  MinIO 协议兼容镜像 `pgsty/silo`（固定 release tag、多架构），可用 `ECHOIC_MINIO_IMAGE` 覆盖；
- 详细说明、备份与故障排查见 §10 与 §15。

---

## 4. 首次登录与初始化

1. 打开 `http://<host>:8000` 会自动跳转 `/login`；
2. 使用 `.env` 中的管理员账号登录（默认 **admin / admin123**）；
3. 首次启动自动完成：
   - 创建 SQLite 数据库与数据表；
   - 创建管理员用户；
   - 生成 `secret.key`（若未显式设置 `ECHOIC_SECRET_KEY`）；
   - `ECHOIC_DEMO_SEED=true`（默认）时写入内置演示录音「TraceOpt 论文讨论」，**无需任何 API Key 即可体验全部功能**；
4. 上线前请立即在「设置 → 账户」修改默认密码。

> `.env` 中的 `ECHOIC_ADMIN_*` **只在首次启动、数据库为空时生效**，之后改 `.env` 不会覆盖已有账号密码。

### 4.1 将演示模式切换为真实 AI

两种方式任选：

- **推荐：网页配置**（即时生效、无需重启）
  「设置 → AI 服务」填写 ASR / LLM / Embedding 的 provider、base_url、model、API Key → 点「测试连接」→ 保存。
- **环境变量**（仅首次启动写入数据库，之后仍以网页配置为准）
  参考 `.env.example` 中「阿里云百炼 DashScope 配置示例」，取消注释并填入 Key。

---

## 5. 配置 AI 服务

### 5.1 默认 mock（零配置）

| 能力 | 行为 |
|---|---|
| ASR | 按音频时长生成演示中文会议转录（含说话人、时间戳） |
| LLM | 返回内置高质量示例纪要/观点树/章节/洞察 |
| Embedding | 关闭，问答使用字面 + 关键词混合检索 |

适合：功能演示、UI 验收、内网无外网环境。
上传真实音频后 mock ASR 也会产生"演示转录"，页面顶部会显示**演示模式**提示条。

### 5.2 阿里云百炼 DashScope（推荐中文场景）

1. 在 [百炼控制台](https://bailian.console.aliyun.com/) 创建 API Key，开通语音识别与 Qwen 模型；
2. 「设置 → AI 服务」按下表填写（或写入 `.env` 后重建空数据库）：

| 模块 | provider | base_url | model |
|---|---|---|---|
| ASR（整场录音，推荐，支持说话人分离） | `dashscope_file` | `https://dashscope.aliyuncs.com` | `qwen-audio-3.1-asr-flash-filetrans`（推荐）或 `paraformer-v2` / `fun-asr` |
| ASR（仅录音页实时字幕，无说话人分离） | `dashscope_realtime` | `wss://dashscope.aliyuncs.com/api-ws/v1/inference` | `paraformer-realtime-v2` |
| LLM | `openai` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus`（或 qwen-max / qwen-turbo） |
| Embedding（可选） | `dashscope` | `https://dashscope.aliyuncs.com` | `text-embedding-v3` |

> **整场会议转写请用 `dashscope_file`**：Echoic 会通过百炼临时上传凭证把本地文件变成 `oss://` URL，
> 不需要公网可访问地址，也不需要自建 OSS；支持 `diarization_enabled` 并落库 `speaker_id`。
> 注意：**百炼所有实时 ASR 模型都不支持说话人分离**（截至 2026-10），实时字幕只作为边录边看的单说话人参考；
> 若开启实时字幕，最终稿仍会用文件转写（`ECHOIC_LIVE_MERGE_MODE=replace`）。
> 文件转写开说话人分离时官方建议单文件 ≤2 小时；模型支持与参数详见
> [百炼说话人分离调研](research/bailian-diarization.md)。
>
> v0.3.5 起「设置 → AI 服务」把两者拆成独立卡片：**语音识别 · 文件转写** 与
> **实时转写 · 录音页字幕**。实时转写默认 `auto`，即复用文件转写的 Key；也可单独填写不同
> 账号/地域的 Key。实时字幕仍需 `ECHOIC_LIVE_ENABLED=true` 才在录音页显示。

### 5.3 OpenAI 兼容端点（DeepSeek / Ollama / vLLM / LM Studio …）

| 模块 | provider | base_url 示例 | model 示例 |
|---|---|---|---|
| LLM | `openai` | `https://api.deepseek.com/v1` | `deepseek-chat` |
| LLM | `openai` | `http://192.168.1.10:11434/v1`（Ollama） | `qwen2.5:14b` |
| ASR | `openai` | `http://192.168.1.10:8000/v1`（faster-whisper-server） | `whisper-1` |
| Embedding | `openai` | `http://192.168.1.10:11434/v1` | `bge-m3` |

- base_url 必须以 `http://` 或 `https://` 开头；
- 内网地址允许（私有化场景需要），设置页仅管理员可改；
- ASR 使用 OpenAI `/audio/transcriptions`（multipart、verbose_json、时间戳），兼容 Whisper / faster-whisper / FunASR 等实现。

### 5.4 API Key 与安全

- Key 仅在数据库中保存 **Fernet 加密值**（密钥由 `ECHOIC_SECRET_KEY` 或 `secret.key` 派生）；
- 接口只返回掩码（`sk-****3f9a`），日志不打印明文；
- 保存新 Key 时输入框留空 = 保持原值，填写 `__clear__` = 清除；
- **不要**在 `.env` 中提交真实 Key 到 git（`.env` 已被 `.gitignore` 忽略）。

---

## 6. 数据、备份与升级

### 6.1 数据构成

| 路径（卷内） | 内容 | 备份必要性 |
|---|---|---|
| `/data/echoic.db`（+ `-wal`/`-shm`） | SQLite 全部业务数据 | **必须** |
| `/data/media/{uuid}/` | 原始音视频与 `playback.m4a` | **必须** |
| `/data/secret.key` | JWT / API Key 加密密钥 | **必须**（丢失后已保存的 API Key 无法解密） |
| `/data/consume/` | v0.2 自动导入监听目录（处理完的原文件会移走/保留策略见日志） | 建议备份（源文件） |
| S3 bucket（`ECHOIC_S3_BUCKET`，v0.3.0） | `s3` 存储模式下全部媒体对象（key 形如 `media/{uuid}/original.*`） | **必须**（可用 `mc mirror` / 对象存储备份工具；compose MiniO 场景可备份卷 `echoic-minio`） |
| 回收站数据（v0.3.0） | 软删除的录音仍保留在 DB 与媒体/对象存储中，直到彻底删除或超过 `ECHOIC_TRASH_RETENTION_DAYS` | 包含在上面的备份中；**过期自动彻底清理不可恢复** |
| PostgreSQL（可选 profile） | 使用 PG 时的业务数据 | 用 `pg_dump` 单独备份 |

### 6.2 备份

```bash
# 推荐：停服后冷备（保证 SQLite 一致性）
docker compose stop echoic
docker run --rm -v echoic-data:/data -v "$PWD/backups":/backup alpine \
  tar czf /backup/echoic-data-$(date +%Y%m%d).tar.gz -C /data .
docker compose start echoic

# 在线热备（WAL 模式下通常安全，尽量在空闲时执行）
docker run --rm -v echoic-data:/data -v "$PWD/backups":/backup alpine \
  sh -c 'tar czf /backup/echoic-data-$(date +%Y%m%d).tar.gz -C /data .'
```

使用 PostgreSQL 时：

```bash
docker compose exec postgres pg_dump -U echoic echoic | gzip > backups/echoic-pg-$(date +%Y%m%d).sql.gz
```

使用 S3 / MinIO 时（v0.3.0，媒体在 bucket 内）：

```bash
# 方式一：用 mc 镜像把 bucket 镜像到本地目录（示例）
docker run --rm --network echoic_default -v "$PWD/backups":/backup \
  -e MC_HOST_local="http://echoic:<口令>@minio:9000" \
  docker.io/pgsty/silo:RELEASE.2026-10-26T00-00-00Z \
  mcli mirror --overwrite local/echoic /backup/s3-echoic-$(date +%Y%m%d)

# 方式二：直接备份 compose 内置 MinIO 数据卷（需停 minio 保证一致性）
docker compose --profile minio stop minio
docker run --rm -v echoic-minio:/data -v "$PWD/backups":/backup alpine \
  tar czf /backup/echoic-minio-$(date +%Y%m%d).tar.gz -C /data .
docker compose --profile minio start minio
```

> DB 备份与 S3 媒体备份应尽量在同一时间窗口完成；恢复时先恢复 DB，再恢复 bucket/对象。

### 6.3 恢复

```bash
docker compose down
docker run --rm -v echoic-data:/data -v "$PWD/backups":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/echoic-data-YYYYMMDD.tar.gz -C /data'
docker compose up -d
```

> 使用 S3/MinIO（v0.3.0）时：恢复顺序为 **先恢复数据库，再恢复 bucket/媒体对象**（见 6.2 的 S3 备份命令）；
> `secret.key` 必须与数据库一并恢复，否则已保存的 API Key 与分享链接 Cookie 无法解密。

### 6.4 升级

```bash
# 1) 先备份（见 6.2）
# 2) 拉取新镜像（默认 latest；若固定了 ECHOIC_IMAGE_TAG 则拉到对应版本）
docker compose pull
# 3) 重建容器（数据卷保留）
docker compose up -d
# 4) 查看日志与健康状态
docker compose logs -f --tail=100 echoic
curl -fsS http://localhost:8000/api/system/health
```

- 语义化版本升级：修改 `.env` 中 `ECHOIC_IMAGE_TAG=v0.3.0` → `docker compose pull && docker compose up -d`；
- 回滚：把 `ECHOIC_IMAGE_TAG` 改回旧版本（或 `sha-xxxxxxx`）后重复 pull/up；
- 源码构建环境：`git pull` 后执行
  `docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`；
- 数据库表结构由应用启动时自动迁移（**幂等**，兼容 v0.1/v0.2 数据目录；SQLite 与 PostgreSQL 均适用；**升级前务必先备份**）。

#### v0.3.0 升级步骤（回收站 / 对象存储 / 实时 / 向量）

```bash
# 1) 备份（DB + 媒体/对象存储 + secret.key，见 6.2）
# 2) 固定目标版本并拉取镜像
sed -i 's/^ECHOIC_IMAGE_TAG=.*/ECHOIC_IMAGE_TAG=v0.3.0/' .env
docker compose pull
# 3) 启动（自动执行幂等迁移：recordings.deleted_at/deleted_by、chunks.embedding_model、
#    live_sessions/live_segments 新表等）
docker compose up -d
# 4) 校验
curl -fsS http://localhost:8000/api/system/health      # status/db/media/storage/worker 应为 ok
curl -fsS http://localhost:8000/api/system/features    # 需登录；live/vector/storage/trash 能力开关
docker compose logs --tail=100 echoic                  # 查看 migration 日志
```

- **旧数据兼容**：v0.3.0 默认仍为 local 存储 / 无实时 / 向量自动降级，升级后功能行为与 v0.2 一致；
- 若计划启用 S3：升级完成后按 §10 配置并先 `storage_migrate --dry-run` → `--apply` → `--verify`，确认无误再清理本地媒体；
- 若计划启用向量检索：在设置页配置 Embedding 后执行一次全量 reindex（§12）；
- 升级失败回滚：`ECHOIC_IMAGE_TAG` 改回 `v0.2.0` 后 `docker compose pull && docker compose up -d`（数据库迁移为向后兼容的加列/建表，回滚不会删除数据）。

---

## 7. 资源估算

| 场景 | CPU | 内存 | 磁盘 | 说明 |
|---|---|---|---|---|
| 演示 / 个人（每周 2~3 小时录音） | 1~2 核 | 1~2GB | 5~20GB | mock 或云端 AI，转码为主要开销 |
| 团队自用（每周 10 小时+） | 2~4 核 | 4GB | 50GB+ | 建议独立 worker + PostgreSQL |
| 真实模型本地推理（Whisper/Ollama） | 8 核 + GPU | 16GB+ | 视模型 | 模型服务独立部署，Echoic 只作为客户端 |

- 转码（16k wav / playback.m4a）为 CPU 峰值来源，处理 1 小时录音约需 1~3 分钟；
- 向量（Embedding）存于 SQLite BLOB，512 维 × 数万切片约数百 MB（v0.3.0 语义检索）；实时转写为 CPU/带宽轻量（16k mono PCM ≈ 32KB/s），但真实 ASR 按服务商时长计费；
- 对象存储（v0.3.0）：若用内置 MinIO，额外预留与媒体量相当的磁盘（bucket 卷 `echoic-minio`）；
- 上传上限由 `ECHOIC_MAX_UPLOAD_MB` 控制（默认 2048MB），反向代理需同步调整 `client_max_body_size`；使用 `ECHOIC_S3_PRESIGN_REDIRECT=true` 时大文件下载不经过应用服务器带宽。

---

## 8. 反向代理与 HTTPS

生产环境强烈建议置于 HTTPS 反向代理之后（Cookie 鉴权、麦克风录音 `getUserMedia` 在非 localhost 的 HTTP 下不可用）。

### 8.1 Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name echoic.example.com;

    ssl_certificate     /etc/letsencrypt/live/echoic.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/echoic.example.com/privkey.pem;

    # 大文件上传（与 ECHOIC_MAX_UPLOAD_MB 保持一致或更大）
    client_max_body_size 2048m;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE（AI 问答流式输出）必须关闭缓冲
        proxy_set_header Connection        "";
        proxy_buffering                    off;
        proxy_cache                        off;
    }
}
```

> 容器已用 `--proxy-headers` 启动；Nginx 需传递 `X-Forwarded-For`，登录限速按真实 IP 生效。
> Caddy 用户可直接 `reverse_proxy 127.0.0.1:8000`（Caddy 自动处理 HTTPS 与流式响应）。

### 8.2 同源部署说明

前端与 API 同源（同一 `:8000`），因此：
- 无需配置 CORS（`ECHOIC_CORS_ORIGINS` 仅开发跨端口时用）；
- Cookie 为 HttpOnly + SameSite=Lax，HTTPS 下自动安全；
- 媒体播放走 `GET /api/recordings/{id}/media`，已支持 HTTP Range（拖动/跳播无需整段下载）。

---

## 9. 可选：PostgreSQL 与独立 worker

### 9.1 PostgreSQL 16

```bash
# .env 中新增（口令与 POSTGRES_PASSWORD 一致）
echo 'ECHOIC_DATABASE_URL=postgresql+psycopg://echoic:echoic@postgres:5432/echoic' >> .env
# 让入口脚本等待数据库端口就绪，避免主服务先于 PG 启动导致崩溃重启
echo 'ECHOIC_WAIT_FOR_DB=true' >> .env

# 镜像优先部署（会自动拉取 postgres:16-alpine 与 echoic 镜像）
docker compose --profile postgres up -d
# 指定版本/升级时：docker compose --profile postgres pull && docker compose --profile postgres up -d
```

注意：
- 从 SQLite 切换到 PG **不会自动迁移已有数据**（新库为空），需要重新上传或自行导出/导入；
- 备份改用 `pg_dump`；
- `ECHOIC_WAIT_FOR_DB=true` 时，容器入口脚本（`docker/entrypoint.sh`）会在启动 uvicorn 前轮询数据库端口（最多 60 秒）；
- compose 不支持按 profile 动态 `depends_on`，因此 worker 与 postgres 同时使用时**务必**设置该变量。

### 9.2 独立 worker（处理与 API 隔离）

```bash
# .env 中关闭内嵌线程
echo 'ECHOIC_EMBEDDED_WORKER=false' >> .env

# 启动主服务 + worker（worker 复用同一镜像与同一数据卷）
docker compose --profile worker up -d

# 若同时使用 PostgreSQL：再叠加 --profile postgres，并设置 ECHOIC_WAIT_FOR_DB=true
docker compose --profile postgres --profile worker up -d
```

- `echoic-worker` 通过 `python -m app.worker` 轮询数据库任务队列；
- 两者必须挂载**同一个 `echoic-data` 卷**并使用同一数据库；
- worker 的 `depends_on` 只能等待主服务健康；与 postgres 组合时由 `ECHOIC_WAIT_FOR_DB=true` 保证数据库就绪；
- 任务状态在 DB 中持久化，worker 重启会自动恢复未完成任务。

### 9.3 内嵌 worker vs 独立 worker：怎么选？

**没有绝对的“性能更好”，取决于负载形态**；默认的内嵌模式对个人/小团队通常是更优解：

| 维度 | 内嵌（`ECHOIC_EMBEDDED_WORKER=true`，默认） | 独立（`--profile worker`） |
|---|---|---|
| 部署复杂度 | 最低：一个容器/进程 | 多一个容器，需共享数据卷与数据库 |
| 资源开销 | 少一个进程，内存/启动开销更低 | 多约 100–300MB 常驻内存；CPU 可独立限制 |
| API 延迟 | 转写/ffmpeg/嵌入等重任务与 API 同进程，长任务可能抢占 CPU/磁盘 IO，接口偶发变慢 | API 与处理彻底隔离，重任务不拖慢页面/上传/播放 |
| 并发/扩展 | 单进程内线程队列；适合个人或少量并发 | worker 可独立重启/扩容；配合 PostgreSQL 可多 worker |
| 故障影响 | worker 崩溃/重启可能影响 API 进程（内嵌线程同进程） | worker 崩溃不导致 API 不可用，任务在 DB 中可恢复 |
| 数据库 | SQLite 足够 | SQLite 可用但只建议 1 个 worker；多 worker/更高中并发建议 PostgreSQL |
| 适用场景 | 单人/家庭、每周少量会议、追求简单 | 团队多人、长录音多、需要稳定 API 响应、定时批量导入 |

**实践建议**：

- 个人私有部署（大多数 Echoic 用户）：保持默认**内嵌**，少一个服务、少一份运维成本；实际瓶颈通常是 ASR/LLM 网络调用而不是 worker 线程本身。
- 团队/多用户、经常同时上传长录音：改为**独立 worker**，API 保持流畅；如任务量大再上 PostgreSQL。
- 使用 SQLite 时不要同时开内嵌 worker 和独立 worker（会争抢写锁）；只保留一种。
- 内嵌/独立只是“执行位置”，任务数据、进度与恢复逻辑一致；切换时改 `ECHOIC_EMBEDDED_WORKER` 并重启即可（已有排队任务会由新 worker 接管）。

---

## 10. 对象存储（S3 / MinIO，v0.3.0）

v0.3.0 新增**存储抽象**：媒体文件可外置到 S3 兼容对象存储，DB/密钥/consume 仍保留在 `/data`。

| 模式 | 配置 | 说明 |
|---|---|---|
| `local`（默认） | `ECHOIC_STORAGE_BACKEND=local` | 行为与 v0.2 完全一致，媒体在 `echoic-data` 卷 |
| `s3` | `ECHOIC_STORAGE_BACKEND=s3` + `ECHOIC_S3_*` | 媒体存入 bucket；支持 MinIO / AWS S3 / 其他 S3 兼容服务 |

### 10.1 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `ECHOIC_STORAGE_BACKEND` | `local` | `local` / `s3` |
| `ECHOIC_S3_ENDPOINT` | 空 | S3 端点；compose 内 MinIO 为 `http://minio:9000`，本机/宿主机访问为 `http://localhost:9000` |
| `ECHOIC_S3_BUCKET` | `echoic` | bucket 名（compose MinIO 由 `minio-init` 自动创建） |
| `ECHOIC_S3_ACCESS_KEY` / `ECHOIC_S3_SECRET_KEY` | 本地体验默认值 | 访问密钥；使用内置 MinIO 时**必须与** `ECHOIC_MINIO_ROOT_USER/PASSWORD` 一致 |
| `ECHOIC_S3_REGION` | `us-east-1` | 区域（MinIO 任意值，AWS 按实际区域） |
| `ECHOIC_S3_PATH_STYLE` | `true` | MinIO 必须 `true`；AWS S3 可用 `false`（virtual-hosted） |
| `ECHOIC_S3_PREFIX` | 空 | 对象键前缀（多实例共享 bucket 时隔离用） |
| `ECHOIC_S3_PRESIGN_REDIRECT` | `false` | `true` 时媒体接口返回 307 预签名 URL；默认 `false` = 同源代理 Range（推荐） |

### 10.2 使用 compose 内置 MinIO

```bash
# .env 关键项（启用内置 MinIO）
ECHOIC_STORAGE_BACKEND=s3
ECHOIC_S3_ENDPOINT=http://minio:9000
ECHOIC_S3_ACCESS_KEY=echoic                  # = ECHOIC_MINIO_ROOT_USER
ECHOIC_S3_SECRET_KEY=change-me-echoic-minio  # = ECHOIC_MINIO_ROOT_PASSWORD（生产必改）
ECHOIC_S3_BUCKET=echoic
ECHOIC_S3_PATH_STYLE=true

docker compose --profile minio up -d         # 启动 minio + minio-init + echoic
docker compose ps                            # minio healthy；minio-init exited(0)
docker compose logs minio-init               # bucket 创建结果
```

- `minio`：S3 API `9000`（`ECHOIC_MINIO_PORT`）、Console `9001`（`ECHOIC_MINIO_CONSOLE_PORT`），数据卷 `echoic-minio`；
- `minio-init`：一次性容器，`mcli mb --ignore-existing` 幂等创建 bucket，失败自动重启重试；
- **镜像说明**：MinIO 官方社区镜像已停止公开分发（2026-10：Docker Hub `minio/minio` 已 404，quay.io 需认证）。
  compose 默认使用活跃维护的 MinIO 协议兼容分支 `docker.io/pgsty/silo:RELEASE.2026-10-26T00-00-00Z`
  （多架构 amd64/arm64，镜像内置 `mcli` 客户端）；如你有官方镜像访问权限，可用
  `ECHOIC_MINIO_IMAGE` 覆盖（`minio` 与 `minio-init` 复用同一镜像）。
- **本机进程 / 宿主机访问**：把 `ECHOIC_S3_ENDPOINT` 改为 `http://localhost:9000` 并确保端口已映射；
  容器之间使用服务名 `minio` 走 compose 网络即可。
- **安全**：生产必须修改默认账号口令；建议只在内网/反代后暴露 S3 与 Console 端口；
  外部 S3（AWS/OSS 等）建议使用仅授权该 bucket 的独立 AccessKey，而不是账号根密钥。
  口令使用 URL 安全字符（默认模板中的 `MC_HOST_local` 会把它拼进 URL）。

### 10.3 从 local 迁移到 S3（storage_migrate）

```bash
# 0) 先完成 §6.2 备份；先配置好 ECHOIC_S3_*（不要急着删除本地媒体）
# 1) 预演：只统计不写入
docker compose exec echoic python -m app.tools.storage_migrate --from local --to s3 --dry-run
# 2) 执行迁移（默认保留源文件，可重试；逐录音复制 original/playback）
#    可选：--limit N 分批迁移；--create-bucket 首次自动建 bucket
docker compose exec echoic python -m app.tools.storage_migrate --from local --to s3 --apply
# 3) 校验：对比对象 size/hash（--no-hash 可只比大小）
docker compose exec echoic python -m app.tools.storage_migrate --from local --to s3 --verify
# 4) 可选：全部校验通过后清理源文件（谨慎；默认保留）
docker compose exec echoic python -m app.tools.storage_migrate --from local --to s3 --apply --prune
```

- 迁移期间无需停机，但建议避免同时上传/处理大文件；失败可重复执行（幂等/可重试）；
- 先 `--dry-run` 看计划，再 `--apply`，最后 `--verify`；**确认 verify 全部通过后再考虑 --prune**（保留源文件是最安全的回滚手段）；
- 回滚：设置 `ECHOIC_STORAGE_BACKEND=local` 并重启即可（若本地媒体已删除，可用
  `storage_migrate --from s3 --to local --apply` 反向迁移）。

### 10.4 健康状态与降级

- `GET /api/system/health` 增加 `storage` 状态：`ok` / `degraded`（具体字段以 v0.3.0 镜像实际输出为准；
  也可通过 `GET /api/system/features` 的 `storage.backend` 或 `GET /api/system/info` 查看当前后端）；
- bucket 缺失、连接失败或凭据错误时：健康检查显示 degraded，上传接口返回中文错误；
- `ECHOIC_S3_PRESIGN_REDIRECT=true` 时媒体接口返回 307 预签名 URL——要求对象存储地址对**最终用户浏览器**可达，
  若 MinIO 仅内网可达请保持 `false`（同源代理 Range）。

---

## 11. 实时字幕（R-19，v0.3.10 GA，默认开启）

> **说明**：实时字幕用于录音过程中的即时参考，**处理完成后的离线转写仍是最终稿**；
> 实时转写不做说话人分离；WebSocket 异常断开支持 120 秒内自动重连/续传。
> 实时会话状态在进程内，单副本部署（不支持多 worker 共享实时会话）。

### 11.1 开启方式

```bash
# .env（或启动环境变量；v0.3.10 起默认 true，可设 false 关闭）
ECHOIC_LIVE_ENABLED=true          # 总开关（kill switch）
ECHOIC_LIVE_PROVIDER=auto         # auto / mock / dashscope_realtime
ECHOIC_LIVE_MAX_MINUTES=60        # 单次会话上限（成本护栏）
ECHOIC_LIVE_MERGE_MODE=replace    # replace（默认）/ promote
docker compose up -d              # 重启生效
```

- 开启后录音页出现「实时字幕」入口（前端通过 `GET /api/system/features` 探测）；
- `ECHOIC_LIVE_PROVIDER=auto`：无 DashScope Key 时自动 mock（零网络/零费用）；配置了文件转写或实时转写的 DashScope Key 时走 `dashscope_realtime`；
- 使用 DashScope 实时时需先在「设置 → AI 服务 → 实时转写」配置独立 provider/base_url/Key/model（也可复用文件转写 Key）；
- 需要浏览器麦克风权限：非 localhost 必须 HTTPS（与普通网页录音相同）。

### 11.2 能力与接口

| 接口 | 说明 |
|---|---|
| `POST /api/live/sessions` | 创建实时会话（`LIVE_ENABLED=false` → 403 `LIVE_DISABLED`） |
| `GET /api/live/sessions/{id}` / `.../segments` | 会话状态 / 已确定片段 |
| `POST /api/live/sessions/{id}/stop` / `cancel` | 结束（幂等）/ 取消并丢弃 |
| `WS /api/live/sessions/{id}/stream` | 实时音频流：Cookie/`?token=` 鉴权；PCM16LE 16kHz mono；事件 `ready/partial/final/error/done` |
| `GET /api/recordings/{id}/live` | 查看绑定到录音的实时稿 |

- WS 关闭码：未启用/无权限 → `4403`；会话不存在 → `4404`；非 active / 重复生产者 → `4409`；音频积压（约 10s 队列）→ `4413`；正常结束 → `1000`；
- `merge_mode=replace`：保留离线 ASR 转录（含说话人）为最终稿，实时稿另存可查；
- `merge_mode=promote`：用实时稿（单说话人）作为最终转录并重算 AI 分析；离线 ASR 失败时也会自动回退 promote。
- **断线续传（GA）**：意外断网后 120 秒内可重连同一会话/WS URL，服务端按已接收音频位置续接时间戳并返回 `ready {resumed:true, base_ms, total_ms, connection}`；超过 120 秒未重连则会话结束（已保存片段仍保留）。
- **心跳**：客户端可发送 `{"type":"ping"}`，服务端回 `{"type":"pong"}`；`heartbeat` 事件被忽略。

### 11.3 限制、成本与容量

- 无说话人分离、无实时 AI 分析，最终以离线转写为准；
- **单进程**：实时会话状态在内存中，多副本/多 worker 部署不支持会话共享（文档与 UI 均已标注）；
- **成本护栏**：`ECHOIC_LIVE_MAX_MINUTES` 到时会发送 error + done 并结束；实时 ASR 按音频时长计费，
  建议仅按需开启并关注服务商计费；
- **背压**：服务端队列有上限（约 10s 音频），超出会返回 error 并关闭连接；
- 关闭开关后旧会话不可继续；已有录音的离线处理不受影响。

---

## 12. 向量 / 混合检索（R-20，v0.3.0）

### 12.1 概念与配置

- 处理完成时按 TranscriptSegment 生成 chunks；配置 Embedding 后将向量写入 `chunks.embedding`（记录 `embedding_model`）；
- 搜索/问答支持 **关键词 / 混合（RRF）/ 语义** 三种模式；无向量或索引为空时自动降级关键词并返回原因（`fallback_reason`）；
- Embedding provider（设置页「AI 服务」或环境变量）：`mock`（演示/测试）、`dashscope`（text-embedding-v3）、
  `openai`（任何 OpenAI 兼容 embedding 服务）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `ECHOIC_VECTOR_BACKEND` | `auto` | `auto`（PG 优先 pgvector；SQLite 优先 sqlite-vec；不可用降级 numpy）/ `numpy` / `sqlite_vec` / `pgvector` |
| `ECHOIC_VECTOR_TOP_K` | 8 | 语义/混合检索返回条数上限 |
| `ECHOIC_VECTOR_MIN_SCORE` | 0.2 | 语义命中最低相似度 |

### 12.2 索引构建 / 重建

- **设置页 → 系统 → 检索索引**（admin）：查看后端/模型/已索引/过期数量，一键重建（支持 dry-run 提示）；
- API：
  - `GET /api/vector/status`（admin）：`{backend,available,embeddings_configured,embedding_model,dim,chunks_total,chunks_indexed,stale_chunks,fallback_reason}`；
  - `POST /api/vector/reindex`（admin，同步执行）：`{recording_ids?, force?, dry_run?}`，单次 ≤200 录音；
  - `GET /api/vector/search?q=&mode=hybrid|semantic&limit=`（登录用户，ACL：admin 全部 / member 仅自己）。
- CLI：`python -m app.tools.vector_reindex --all [--dry-run] [--recording-id ID] [--force]`
  （容器内：`docker compose exec echoic python -m app.tools.vector_reindex --all`）。
- 更换 Embedding 模型/维度后需 `--force` 全量重建；`embedding_model` 不一致的 chunks 计为 stale。

### 12.3 后端选择与降级

| 后端 | 说明 |
|---|---|
| `numpy`（auto 最终回退） | 进程内加载可见录音的 chunks 矩阵做余弦相似度；单进程内存，适合个人/小团队 |
| `sqlite_vec`（SQLite 推荐） | 随镜像安装的 sqlite-vec 扩展；`vec0` 虚拟表做 KNN；auto 在 SQLite 上优先尝试，扩展不可加载时自动降级 numpy 并记录原因 |
| `pgvector`（experimental） | 仅 PostgreSQL + pgvector 扩展可用；启动时尝试 `CREATE EXTENSION` 与建表；不可用自动降级 numpy 并记录原因 |

- 未配置 Embedding 时：语义/混合搜索禁用（前端置灰并展示原因），问答行为与 v0.2 等价（字面/关键词检索）；
- 索引 stale（模型变更/未建索引）时：搜索结果可能只覆盖部分录音，建议先 reindex；
- 存储占用：向量以 float32 BLOB 存在 DB；每 1 万 chunk × 1024 维约 40MB（另加索引开销）。

---

## 13. 回收站与数据保留（R-23，v0.3.0）

### 13.1 软删除语义

- `DELETE /api/recordings/{id}` 默认**软删除**（移入回收站），返回 `{ok,id,deleted_at,restore_before}`；
- 默认的列表 / 详情 / 搜索 / 仪表盘 / 标签计数 / 分享 / 聊天 / 媒体等**全部排除**已删除录音（访问 → 404）；
- 已删除录音的公开分享自动失效（404）；删除后分享被软撤销，**恢复后不会自动恢复分享**，需要重新创建；
- 回收站入口：资料库筛选「回收站」；支持恢复 / 彻底删除（二次确认）；批量操作见 §13.3。

### 13.2 保留期与自动清理

| 变量 | 默认 | 说明 |
|---|---|---|
| `ECHOIC_TRASH_RETENTION_DAYS` | 30 | 回收站保留天数；过期后由后台 worker **每小时**彻底清理 |
| `ECHOIC_BATCH_MAX_ITEMS` | 100 | 批量操作单次最大条目数（超限 400） |

- 恢复窗口：删除后 `restore_before` 之前可恢复；超过保留期的录音会被自动彻底删除；
- 彻底删除会清理 DB 行 + 本地媒体目录 / S3 对象前缀 + 分享关系，**不可恢复**；
- 回收站中的录音仍占用磁盘/对象存储，并包含在备份中；若需立即释放空间，可在回收站执行「彻底删除」。

### 13.3 批量操作

- 支持动作：`trash`（默认，移入回收站）/ `restore` / `purge` / `reprocess` / `tag_add` / `tag_remove`；
- 权限逐条校验、**部分成功**（返回 `succeeded/failed/results`，失败项带原因）；tag 操作为 admin-only；
- 前端仅对当前筛选页选中项生效（文案会提示）；批量删除一律走回收站，不提供批量直接彻底删除。

---

## 14. 本地开发与类生产运行（不经 Docker）

> 适用：没有 Docker 的开发机 / WSL / 只想快速跑通验收。

```bash
# 依赖：Python 3.11+（推荐 3.12）、Node.js 20+（推荐 22）、ffmpeg
sudo apt install -y ffmpeg

# 一键开发模式：后端 :8000（--reload）+ 前端 :5173（Vite 代理 /api）
./scripts/dev.sh
# 首次自动创建 backend/.venv、安装依赖；Ctrl-C 同时停止

# 类生产模式：构建前端 → FastAPI 同源托管 → :8100
./scripts/run-local.sh
# 可组合环境变量做隔离验收：
ECHOIC_DATA_DIR=/tmp/echoic-e2e ECHOIC_DEMO_SEED=true ECHOIC_SECRET_KEY=e2e-secret \
  ./scripts/run-local.sh

# 一键验证（后端测试 + 前端构建 + 健康检查 + 可选 e2e）
./scripts/verify.sh
```

脚本说明：

| 脚本 | 作用 |
|---|---|
| `scripts/dev.sh` | 创建 venv、装依赖、同时启动 uvicorn --reload(:8000) 与 vite(:5173)，trap 清理 |
| `scripts/run-local.sh` | 构建前端并由后端托管，uvicorn :8100；`--no-build` / `--copy` 可选 |
| `scripts/verify.sh` | pytest + 前端 tsc/build + 临时数据目录启动健康检查 + 可选 e2e 冒烟 |

---

## 15. 故障排查

| 现象 | 排查与解决 |
|---|---|
| 容器起不来，`docker compose ps` 显示 `unhealthy` | `docker compose logs echoic --tail=200`；常见原因：`.env` 缺失、端口被占用、数据卷权限 |
| `docker compose pull` 返回 403 / `denied` | GHCR 包仍为私有：先 `docker login ghcr.io`（PAT 需 `read:packages`），或把包设为 Public（见 §3.2） |
| `exec format error` / 镜像平台不匹配 | 确认拉取的是多架构镜像（`docker buildx imagetools inspect ghcr.io/wpcwzy/echoic:latest`）；ARM 主机需 Docker 支持 arm64 |
| `curl /api/system/health` 返回 `ffmpeg: missing` | 镜像内应自带 ffmpeg；自建镜像请确认 `apt-get install ffmpeg` 未失败 |
| 健康检查 `db: error` / 日志 `database is locked` | SQLite 并发写争用：确认只运行 1 个写进程（关闭内嵌 worker 或用独立 worker 时**不要**同时开内嵌线程）；PG 场景检查 `ECHOIC_DATABASE_URL` |
| 上传大文件 413 | 调大 `ECHOIC_MAX_UPLOAD_MB`，并同步反向代理 `client_max_body_size` |
| 上传返回 415 | 扩展名/编码不支持；支持 mp3/wav/m4a/aac/ogg/webm/mp4/mov/mkv，先用 `ffprobe` 验证文件完整性 |
| 任务一直 `queued` | worker 未运行：默认内嵌线程应自动启动；检查日志 `worker started`；独立 worker 场景确认 `echoic-worker` 容器在跑 |
| ASR「测试连接」报 `dashscope_file ... HTTP 404`／上传转写 404 | Base URL 填写错误：`dashscope_file` 应填 `https://dashscope.aliyuncs.com`（**不要**带 `/api/v1` 或 `/compatible-mode/v1`；工作空间域名填到域名根）。v0.3.1 起服务端会自动纠正常见路径并提供旧域名回退；仍失败请确认 Key 与地域一致、已开通 Paraformer 录音文件识别 |
| ASR「测试连接」成功但上传后报「实时转写握手超时（未收到 task-started）」 | v0.3.0 的实时转写握手缺陷，升级到 **v0.3.1** 修复。若仍出现：确认 Base URL 为 `wss://dashscope.aliyuncs.com/api-ws/v1/inference`（或只填域名）、模型为 `paraformer-realtime-v2`、Key 与地域一致，并检查该 Key 的实时转写并发额度；工作日志会打印实际 endpoint/model。整场录音转写建议改用 `dashscope_file`（带说话人分离） |
| 整场录音应该选哪个 ASR？ | 上传录音/视频做整场转写选 **`dashscope_file`（录音文件识别，支持说话人分离，无需公网 URL）**；`dashscope_realtime` 仅用于录音页实时字幕（GA，无说话人分离），不是整场转写主路径 |
| 任务 `failed` 且提示 ASR/LLM 连接失败 | 「设置 → AI 服务」点「测试连接」；检查 base_url / Key / 出站网络；日志中不含明文 Key |
| 放入 consume 目录的文件未自动导入（v0.2） | 确认 `ECHOIC_CONSUME_ENABLED=true`、目录在容器内可写（默认 `/data/consume`）、扩展名受支持；查看日志 `consume`；修改 `ECHOIC_CONSUME_INTERVAL_SEC` 可调整扫描间隔；归属用户由 `ECHOIC_CONSUME_OWNER_USERNAME` 指定（空=首个管理员） |
| 公开分享链接无法解锁 | 检查 `ECHOIC_SHARE_COOKIE_HOURS`（默认 2 小时）与链接是否过期；分享解锁 Cookie 仅对该分享记录有效 |
| v0.3 S3：`/api/system/health` 显示 storage degraded / 上传中文报错 | 检查 `ECHOIC_S3_ENDPOINT` 可达性、bucket 是否存在（`minio-init` 日志/`mcli ls local/echoic`）、`ECHOIC_S3_ACCESS_KEY/SECRET_KEY` 与 MinIO 账号是否一致、`ECHOIC_S3_PATH_STYLE=true`；容器内用服务名 `minio`，本机进程用 `localhost` |
| v0.3 S3：返回 403 / SignatureDoesNotMatch | 访问密钥不匹配或含 URL 特殊字符；确认 MinIO `ECHOIC_MINIO_ROOT_USER/PASSWORD` 与应用 `ECHOIC_S3_ACCESS_KEY/SECRET_KEY` 完全一致，口令使用 URL 安全字符 |
| v0.3 MinIO：`docker compose --profile minio pull` 404 / 无法拉取 | 官方社区镜像已停止公开分发（2026-10）；使用 compose 默认的 `pgsty/silo` 固定 tag，或通过 `ECHOIC_MINIO_IMAGE` 指定你有权访问的 MinIO 镜像仓库 |
| v0.3 媒体：浏览器 307 跳转到对象存储后打不开 | 设置了 `ECHOIC_S3_PRESIGN_REDIRECT=true` 但 MinIO/S3 地址对用户浏览器不可达；改回 `false`（同源代理 Range）或为对象存储配置公网 HTTPS/反代 |
| v0.3 实时：录音页没有实时字幕入口 / WS 连接失败 | 确认 `ECHOIC_LIVE_ENABLED=true` 并重启；`GET /api/system/features` 查看 live.enabled/provider；WS 关闭码 4403=未启用/无权限、4404=会话不存在、4409=非 active、4413=音频积压；检查浏览器麦克风权限与 HTTPS、DashScope ASR 配置与出站网络 |
| v0.3 实时：实时稿与最终稿不一致 / 没有说话人 | 预期行为：实时稿仅作参考（无说话人分离），处理完成后以离线转写为最终稿；如需用实时稿作为最终转录，设置 `ECHOIC_LIVE_MERGE_MODE=promote` 后重新处理 |
| v0.3 向量：语义搜索被禁用 / 结果不全 | 设置页配置 Embedding 后执行全量重建（§12）；`GET /api/vector/status` 查看 `embeddings_configured/stale_chunks/fallback_reason`；`ECHOIC_VECTOR_BACKEND=auto` 在 PG 无 pgvector 扩展时会自动降级 numpy |
| v0.3 回收站：删除的录音想找回 / 空间未释放 | 在资料库「回收站」中恢复（`restore_before` 之前）；彻底删除或超过 `ECHOIC_TRASH_RETENTION_DAYS` 后不可恢复；回收站录音仍占用磁盘/对象存储，可执行「彻底删除」立即释放 |
| v0.3 批量操作返回 400 | 单次条目数超过 `ECHOIC_BATCH_MAX_ITEMS`（默认 100）或条目非法；批量结果为部分成功（见返回 `results`） |
| 登录 429 | 登录限速 5 次/分钟/IP；等待 1 分钟，或确认反向代理传递了真实 `X-Forwarded-For` |
| 麦克风无法录音 | 非 HTTPS 且非 localhost 时浏览器禁止 `getUserMedia`；配置 HTTPS 或改用文件上传 |
| 忘记管理员密码 | 停止服务 → 用 sqlite 删除/重置用户后重启会重建管理员：`docker compose exec echoic python -c "..."`（或恢复备份）；更简单：删除空库重来仅适用于演示数据场景 |
| API Key 解密失败 | `secret.key` 与数据库不匹配（更换过卷/密钥）；在设置页重新填写 Key |
| 浏览器打开是 404 / 空白 | 官方镜像自带前端产物；若源码构建，确认 Dockerfile 前端阶段成功；本地非 Docker 运行需设置 `ECHOIC_STATIC_DIR` 或先 `npm run build` |
| 中文文件名导出乱码 | 导出使用 RFC5987 `filename*`，确保反向代理未改写 `Content-Disposition` |

日志与诊断：

```bash
docker compose logs -f echoic                 # 应用日志（访问日志、错误）
docker compose exec echoic sh                 # 进容器排查
curl -fsS http://localhost:8000/api/system/health
curl -fsS http://localhost:8000/api/system/info   # 需登录（管理员）
docker stats echoic                           # 资源占用
```

---

## 16. 安全建议清单

- [ ] 修改默认管理员密码（`admin/admin123` 仅用于首次登录）；
- [ ] 生产环境通过 HTTPS 暴露（反向代理），不要直接裸奔公网 8000；
- [ ] `.env` 权限设为 `600`，不要提交到 git；
- [ ] 定期备份 `echoic-data` 卷（尤其 `secret.key`）；
- [ ] 限制 API Key 权限范围，优先使用可随时吊销的子账号 Key；
- [ ] 如部署在公网：结合防火墙限制来源 IP、启用登录限速与 fail2ban；
- [ ] 关注上传上限与磁盘水位，防止磁盘写满导致 SQLite 损坏；
- [ ] v0.2 多用户 / 分享能力默认关闭或受限：保持 `ECHOIC_ALLOW_REGISTRATION=false`（禁止匿名注册），按需在用户管理创建账号；公开分享链接只在需要时创建，并注意 `ECHOIC_SHARE_COOKIE_HOURS` 时效；
- [ ] 定期 `docker compose pull` 获取安全更新（或固定语义化版本并关注 [CHANGELOG](../CHANGELOG.md)）。
- [ ] v0.3 对象存储：修改 MinIO 默认账号口令；MinIO S3 API/Console 端口不要直接暴露公网（反代 + HTTPS + 防火墙）；外部 S3 使用仅授权单 bucket 的独立密钥，并妥善备份 bucket 数据；
- [ ] v0.3 实时转写：按需开启 `ECHOIC_LIVE_ENABLED`，用 `ECHOIC_LIVE_MAX_MINUTES` 限制单次时长以控制第三方计费；
- [ ] v0.3 回收站：`ECHOIC_TRASH_RETENTION_DAYS` 到期会不可恢复地清理数据，回收站内容一并纳入备份策略；
- [ ] v0.3 向量检索：Embedding API Key 同样加密存储与掩码显示；更换 Embedding 模型/维度后需全量 reindex（旧向量会被判定 stale）。

---

## 17. 构建与镜像说明

### 13.1 官方镜像（推荐）

- 地址：`ghcr.io/wpcwzy/echoic`，多架构 `linux/amd64` + `linux/arm64`；
- 由 GitHub Actions 自动构建发布：[`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml)
  （QEMU + Buildx、GHCR 登录、BuildKit GHA 缓存、provenance 构建证明、latest/semver/sha 标签）；
- CI 构建参数：`NPM_REGISTRY=https://registry.npmjs.org`、`PIP_INDEX_URL=https://pypi.org/simple`；
- 发布新版本：推送 `v*` tag 即可（详见 [README 容器镜像与 CI/CD](../README.md#容器镜像与-cicd)）；
- 首次发布后需将 GHCR 包可见性设为 Public（见 §3.2）。

### 13.2 源码构建（本地自建镜像）

`Dockerfile` 为两阶段构建：

```
Stage 1  node:22-alpine      frontend-builder
         COPY frontend/package*.json →
           项目级 .npmrc（cache=/root/.npm、logs-dir=/root/.npm/_logs、replace-registry-host=always）
           npm ci（BuildKit 缓存挂载 /root/.npm）→ COPY frontend/ → npm run build → dist/

Stage 2  python:3.12-slim    runtime
         apt-get install ffmpeg curl tzdata
         pip install -r backend/requirements.txt
         COPY backend/ → /app/backend
         COPY docker/ → /app/docker（entrypoint）
         COPY --from=Stage1 dist/ → /app/backend/app/static
         user echoic(uid 1000)，VOLUME /data，EXPOSE 8000
         HEALTHCHECK curl /api/system/health
         ENTRYPOINT docker/entrypoint.sh
         CMD uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
```

- 入口脚本负责：数据目录可写校验、可选等待数据库、`exec` 主进程（信号直达）；
- v0.3.0 镜像内已包含 `boto3`（S3 对象存储）与 numpy 等依赖，无需额外构建参数；MinIO 为独立 compose profile，不进入 Echoic 镜像；
- 可自定义构建参数：`NODE_VERSION`、`PYTHON_VERSION`、`NPM_REGISTRY`、`PIP_INDEX_URL`；
- 镜像内以非 root（uid 1000）运行；bind mount 宿主机目录时需 `sudo chown -R 1000:1000 <目录>`；
- 本仓库开发机无 Docker CLI，本地未运行过容器；但官方镜像已由 GitHub Actions 在真实 Docker 环境**成功构建并推送 GHCR**（多架构），首次 Actions 运行记录见 GitHub Actions 页面。

---

## 18. 验证部署是否成功

```bash
# 1) 健康检查（无需登录）
curl -fsS http://localhost:8000/api/system/health | python3 -m json.tool

# 2) 登录（返回 token，同时种下 Cookie）
curl -fsS -c /tmp/echoic.cookie -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -m json.tool

# 3) 资料库列表（应看到演示录音）
curl -fsS -b /tmp/echoic.cookie 'http://localhost:8000/api/recordings?page=1&page_size=5' | python3 -m json.tool

# 4) v0.3.0 新能力自检
#    能力开关（需登录 Cookie 或 Bearer）
curl -fsS -b /tmp/echoic.cookie http://localhost:8000/api/system/features | python3 -m json.tool
#    对象存储（启用 S3 时）
docker compose ps                     # minio healthy / minio-init exited(0)
#    向量索引状态（admin）
curl -fsS -b /tmp/echoic.cookie http://localhost:8000/api/vector/status | python3 -m json.tool
#    回收站（admin）
curl -fsS -b /tmp/echoic.cookie 'http://localhost:8000/api/recordings?trashed=true&page_size=5' | python3 -m json.tool
#    实时转写（默认关闭应返回 403 LIVE_DISABLED）
curl -fsS -o /dev/null -w '%{http_code}\n' -b /tmp/echoic.cookie -X POST \
  -H 'Content-Type: application/json' -d '{}' http://localhost:8000/api/live/sessions

# 5) 完整自动化验收（本地环境）
./scripts/verify.sh            # 依赖齐全时包含 e2e/smoke.py / smoke_v02.py
```

详细端到端用例与截图见 `e2e/REPORT.md`、`docs/screenshots/`。
