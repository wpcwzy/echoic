# Echoic 部署指南

> 本文面向**自托管部署**：用 Docker Compose 一条命令跑起「录音 → 转录 → AI 纪要 → 观点树 / 思维导图 → RAG 问答」完整链路。
> 开发调试见 [README 开发指南](../README.md#开发指南)；接口与架构分别见 [API.md](API.md)、[ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 1. 部署形态与端口

```
浏览器 ──HTTPS──▶ 反向代理(可选 Nginx/Caddy) ──▶ Echoic 容器 :8000
                                                  ├── FastAPI /api/*，SSE 流式问答
                                                  └── 前端静态资源（同源，无需单独 nginx）
                                            卷 echoic-data:/data
                                              ├── echoic.db（SQLite，默认）
                                              ├── media/{uuid}/original.* · playback.m4a
                                              └── secret.key（JWT/密钥加密）
```

- **单容器**：FastAPI 同时提供 API 与前端，无 Redis / Celery 依赖；
- **无外部服务**也能完整运行（默认 mock AI + SQLite + 内嵌任务线程）；
- 真实 AI 服务只需**出站 HTTPS** 访问用户配置的服务商（阿里云百炼 / OpenAI 兼容端点 / Whisper 兼容服务）。

| 项目 | 默认值 | 说明 |
|---|---|---|
| 容器内端口 | `8000` | 由 uvicorn 监听 |
| 宿主机端口 | `8000` | compose 中 `${ECHOIC_HTTP_PORT:-8000}` 可改 |
| 数据目录 | `/data`（卷 `echoic-data`） | SQLite + 媒体 + 密钥 |
| 镜像 | `echoic:latest` | 多阶段构建产物 |

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

> 构建阶段需要访问 npm / PyPI（或配置国内镜像，见 §10）。
> 运行阶段**不需要**联网（mock 模式）。

---

## 3. 快速开始

```bash
# 1. 获取代码
git clone <repo-url> echoic && cd echoic

# 2. 准备环境变量（至少修改管理员口令）
cp .env.example .env
vi .env        # 修改 ECHOIC_ADMIN_PASSWORD；ECHOIC_SECRET_KEY 可留空自动生成

# 3. 构建并启动（首次构建 3~8 分钟，取决于网络）
docker compose up -d --build

# 4. 查看状态
docker compose ps
docker compose logs -f echoic
```

启动成功的标志（`docker compose ps` 中 `echoic` 为 `healthy`，或日志出现 `starting Echoic v0.1.0`）：

```bash
curl -fsS http://localhost:8000/api/system/health
# {"status":"ok","version":"0.1.0","db":"ok","media":"ok","ffmpeg":"ok","worker":"ok","demo_mode":true,...}
```

浏览器打开 **http://localhost:8000**。

### 3.1 国内网络加速（可选）

```bash
# 方式一：写入 .env，compose 会作为 build args 传入
echo 'NPM_REGISTRY=https://registry.npmmirror.com' >> .env
echo 'PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple' >> .env
docker compose build --no-cache echoic   # 或直接 compose up -d --build

# 方式二：直接 docker build 传参
docker build \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t echoic:latest .
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

1. 在 [百炼控制台](https://bailian.console.aliyun.com/) 创建 API Key，开通 Paraformer 语音识别与 Qwen 模型；
2. 「设置 → AI 服务」按下表填写（或写入 `.env` 后重建空数据库）：

| 模块 | provider | base_url | model |
|---|---|---|---|
| ASR | `dashscope_realtime` | `wss://dashscope.aliyuncs.com/api-ws/v1/inference` | `paraformer-realtime-v2` |
| LLM | `openai` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus`（或 qwen-max / qwen-turbo） |
| Embedding（可选） | `dashscope` | `https://dashscope.aliyuncs.com` | `text-embedding-v3` |

> ASR 使用 DashScope 实时 WebSocket 协议（本地 16k 单声道 wav 流式发送），无需公网 URL；
> 若使用 `dashscope_file` 则需要可公网访问的 OSS 地址，私有化场景建议实时接口或 OpenAI 兼容 Whisper。

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

### 6.3 恢复

```bash
docker compose down
docker run --rm -v echoic-data:/data -v "$PWD/backups":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/echoic-data-YYYYMMDD.tar.gz -C /data'
docker compose up -d
```

### 6.4 升级

```bash
# 1) 先备份（见 6.2）
# 2) 拉取新代码并重建镜像
git pull
docker compose up -d --build
# 3) 查看日志与健康状态
docker compose logs -f --tail=100 echoic
curl -fsS http://localhost:8000/api/system/health
```

- 数据库表结构由应用启动时自动迁移（SQLite `create_all`；如需精细迁移请先备份）；
- 旧镜像保留便于回滚：`docker tag echoic:latest echoic:rollback` 后再构建。

---

## 7. 资源估算

| 场景 | CPU | 内存 | 磁盘 | 说明 |
|---|---|---|---|---|
| 演示 / 个人（每周 2~3 小时录音） | 1~2 核 | 1~2GB | 5~20GB | mock 或云端 AI，转码为主要开销 |
| 团队自用（每周 10 小时+） | 2~4 核 | 4GB | 50GB+ | 建议独立 worker + PostgreSQL |
| 真实模型本地推理（Whisper/Ollama） | 8 核 + GPU | 16GB+ | 视模型 | 模型服务独立部署，Echoic 只作为客户端 |

- 转码（16k wav / playback.m4a）为 CPU 峰值来源，处理 1 小时录音约需 1~3 分钟；
- 向量（Embedding）存于 SQLite BLOB，512 维 × 数万切片约数百 MB；
- 上传上限由 `ECHOIC_MAX_UPLOAD_MB` 控制（默认 2048MB），反向代理需同步调整 `client_max_body_size`。

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

docker compose --profile postgres up -d --build
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

# 启动主服务 + worker（worker 复用 echoic:latest 镜像与同一数据卷）
docker compose --profile worker up -d --build

# 若同时使用 PostgreSQL：再叠加 --profile postgres，并设置 ECHOIC_WAIT_FOR_DB=true
docker compose --profile postgres --profile worker up -d --build
```

- `echoic-worker` 通过 `python -m app.worker` 轮询数据库任务队列；
- 两者必须挂载**同一个 `echoic-data` 卷**并使用同一数据库；
- worker 的 `depends_on` 只能等待主服务健康；与 postgres 组合时由 `ECHOIC_WAIT_FOR_DB=true` 保证数据库就绪；
- 任务状态在 DB 中持久化，worker 重启会自动恢复未完成任务。

---

## 10. 本地开发与类生产运行（不经 Docker）

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

## 11. 故障排查

| 现象 | 排查与解决 |
|---|---|
| 容器起不来，`docker compose ps` 显示 `unhealthy` | `docker compose logs echoic --tail=200`；常见原因：`.env` 缺失、端口被占用、数据卷权限 |
| `curl /api/system/health` 返回 `ffmpeg: missing` | 镜像内应自带 ffmpeg；自建镜像请确认 `apt-get install ffmpeg` 未失败 |
| 健康检查 `db: error` / 日志 `database is locked` | SQLite 并发写争用：确认只运行 1 个写进程（关闭内嵌 worker 或用独立 worker 时**不要**同时开内嵌线程）；PG 场景检查 `ECHOIC_DATABASE_URL` |
| 上传大文件 413 | 调大 `ECHOIC_MAX_UPLOAD_MB`，并同步反向代理 `client_max_body_size` |
| 上传返回 415 | 扩展名/编码不支持；支持 mp3/wav/m4a/aac/ogg/webm/mp4/mov/mkv，先用 `ffprobe` 验证文件完整性 |
| 任务一直 `queued` | worker 未运行：默认内嵌线程应自动启动；检查日志 `worker started`；独立 worker 场景确认 `echoic-worker` 容器在跑 |
| 任务 `failed` 且提示 ASR/LLM 连接失败 | 「设置 → AI 服务」点「测试连接」；检查 base_url / Key / 出站网络；日志中不含明文 Key |
| 登录 429 | 登录限速 5 次/分钟/IP；等待 1 分钟，或确认反向代理传递了真实 `X-Forwarded-For` |
| 麦克风无法录音 | 非 HTTPS 且非 localhost 时浏览器禁止 `getUserMedia`；配置 HTTPS 或改用文件上传 |
| 忘记管理员密码 | 停止服务 → 用 sqlite 删除/重置用户后重启会重建管理员：`docker compose exec echoic python -c "..."`（或恢复备份）；更简单：删除空库重来仅适用于演示数据场景 |
| API Key 解密失败 | `secret.key` 与数据库不匹配（更换过卷/密钥）；在设置页重新填写 Key |
| 浏览器打开是 404 / 空白 | 构建产物缺失：确认 `docker build` 前端阶段成功；本地运行需设置 `ECHOIC_STATIC_DIR` 或先 `npm run build` |
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

## 12. 安全建议清单

- [ ] 修改默认管理员密码（`admin/admin123` 仅用于首次登录）；
- [ ] 生产环境通过 HTTPS 暴露（反向代理），不要直接裸奔公网 8000；
- [ ] `.env` 权限设为 `600`，不要提交到 git；
- [ ] 定期备份 `echoic-data` 卷（尤其 `secret.key`）；
- [ ] 限制 API Key 权限范围，优先使用可随时吊销的子账号 Key；
- [ ] 如部署在公网：结合防火墙限制来源 IP、启用登录限速与 fail2ban；
- [ ] 关注上传上限与磁盘水位，防止磁盘写满导致 SQLite 损坏；
- [ ] 多用户/团队权限、分享链接等能力尚未实现（见 Roadmap），**当前定位为单管理员私有部署**。

---

## 13. 构建与镜像说明

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
- 可自定义构建参数：`NODE_VERSION`、`PYTHON_VERSION`、`NPM_REGISTRY`、`PIP_INDEX_URL`；
- 镜像内以非 root（uid 1000）运行；bind mount 宿主机目录时需 `sudo chown -R 1000:1000 <目录>`；
- 本仓库开发环境无 Docker CLI，Dockerfile 仅做静态审查 + YAML 解析校验（见 `e2e/REPORT.md`）。

---

## 14. 验证部署是否成功

```bash
# 1) 健康检查（无需登录）
curl -fsS http://localhost:8000/api/system/health | python3 -m json.tool

# 2) 登录（返回 token，同时种下 Cookie）
curl -fsS -c /tmp/echoic.cookie -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -m json.tool

# 3) 资料库列表（应看到演示录音）
curl -fsS -b /tmp/echoic.cookie 'http://localhost:8000/api/recordings?page=1&page_size=5' | python3 -m json.tool

# 4) 完整自动化验收（本地环境）
./scripts/verify.sh            # 依赖齐全时包含 e2e/smoke.py
```

详细端到端用例与截图见 `e2e/REPORT.md`、`docs/screenshots/`。
