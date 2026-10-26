# syntax=docker/dockerfile:1.7
# ============================================================================
# Echoic 多阶段镜像
#   阶段 1 frontend-builder：node:22-alpine 中 npm ci + vite build（产物 dist/）
#   阶段 2 runtime：python:3.12-slim + ffmpeg，FastAPI 同源托管前端，非 root 运行
#
# 构建：docker build -t echoic:latest .
# 运行：docker compose up -d --build   （见 docker-compose.yml / docs/DEPLOYMENT.md）
# 说明：npm ci 依赖 frontend/package-lock.json；若 lockfile 缺失会回退 npm install 并告警。
# ============================================================================

ARG NODE_VERSION=22
ARG PYTHON_VERSION=3.12

# ------------------------------ 阶段 1：构建前端 ------------------------------
FROM node:${NODE_VERSION}-alpine AS frontend-builder

WORKDIR /build/frontend

# 仅拷贝依赖清单以命中 Docker 层缓存。
# 使用 package*.json 通配符：package-lock.json 存在与否都不会导致 COPY 失败。
COPY frontend/package*.json ./
# 项目级 npm 配置（/build/frontend/.npmrc）：
#  - 显式指定容器内可写的 cache / logs-dir，避免依赖 HOME；
#  - replace-registry-host=always：lockfile 中的 resolved 域名可被 registry 配置覆盖
#    （本仓库 lock 由 registry.npmmirror.com 生成，海外构建可用 NPM_REGISTRY 切换源）；
#  - NPM_REGISTRY 可变参数（如 https://registry.npmmirror.com 加速国内构建）。
ARG NPM_REGISTRY=""
RUN npm config set cache /root/.npm --location=project \
 && npm config set logs-dir /root/.npm/_logs --location=project \
 && npm config set replace-registry-host always --location=project \
 && if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi
RUN --mount=type=cache,target=/root/.npm \
    if [ -f package-lock.json ]; then \
      npm ci --no-audit --no-fund; \
    else \
      echo "WARN: frontend/package-lock.json 不存在，回退到 npm install；建议提交 lockfile 以保证可复现构建" >&2; \
      npm install --no-audit --no-fund; \
    fi

COPY frontend/ ./
RUN npm run build \
 && test -f dist/index.html

# ------------------------------ 阶段 2：运行时 ------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="Echoic" \
      org.opencontainers.image.description="自托管会议录音知识库（转录 / AI 纪要 / 观点树 / RAG 问答）" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    ECHOIC_DATA_DIR=/data \
    ECHOIC_STATIC_DIR=/app/backend/app/static

# ffmpeg/ffprobe：音视频标准化、转码、时长探测；curl：HEALTHCHECK；tzdata：日志时区
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# 非 root 运行（uid/gid 1000，与常见宿主机用户一致，便于 bind mount）
RUN groupadd --gid 1000 echoic \
 && useradd --uid 1000 --gid echoic --create-home --shell /usr/sbin/nologin echoic

WORKDIR /app

# 先安装 Python 依赖：业务代码变更不会击穿该层缓存
# 可选：PyPI 镜像源（如 https://pypi.tuna.tsinghua.edu.cn/simple 加速国内构建）
COPY backend/requirements.txt /app/backend/requirements.txt
ARG PIP_INDEX_URL=""
RUN python -m pip install --upgrade pip \
 && if [ -n "$PIP_INDEX_URL" ]; then \
      python -m pip install --index-url "$PIP_INDEX_URL" -r /app/backend/requirements.txt; \
    else \
      python -m pip install -r /app/backend/requirements.txt; \
    fi

# 后端源码、容器入口脚本、前端构建产物（由 FastAPI 直接托管，无需 nginx）
COPY backend/ /app/backend/
COPY docker/ /app/docker/
COPY --from=frontend-builder /build/frontend/dist/ /app/backend/app/static/

# /data 即命名卷挂载点：首次创建卷时继承镜像内目录属主，保证非 root 可写
RUN mkdir -p /data \
 && chown -R echoic:echoic /data \
 && chmod +x /app/docker/entrypoint.sh

WORKDIR /app/backend
USER echoic

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/system/health || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
