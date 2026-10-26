#!/bin/sh
# ============================================================================
# Echoic 容器入口脚本
#   1) 校验数据目录可写（命名卷首次挂载会自动继承镜像内属主）
#   2) 可选：等待外部 PostgreSQL 就绪（ECHOIC_WAIT_FOR_DB=true）
#   3) exec 主进程（uvicorn / python -m app.worker），保证信号直达、优雅退出
#
# 可用环境变量：
#   ECHOIC_DATA_DIR    数据目录（默认 /data）
#   ECHOIC_APP_DIR     后端代码目录（默认 /app/backend）
#   ECHOIC_WAIT_FOR_DB 是否等待 DATABASE_URL 指向的数据库可连接（默认 false）
#   ECHOIC_UMASK       文件创建掩码（默认 027）
# ============================================================================
set -eu

DATA_DIR="${ECHOIC_DATA_DIR:-/data}"
APP_DIR="${ECHOIC_APP_DIR:-/app/backend}"
WAIT_FOR_DB="${ECHOIC_WAIT_FOR_DB:-false}"

log() {
  printf '[entrypoint] %s\n' "$*"
}

umask "${ECHOIC_UMASK:-027}"

# ---------- 1) 数据目录校验 ----------
if ! mkdir -p "$DATA_DIR" 2>/dev/null || [ ! -w "$DATA_DIR" ]; then
  log "错误：数据目录 ${DATA_DIR} 不可写（当前 UID=$(id -u)）。"
  log "Docker 命名卷会自动继承镜像内属主；若使用 bind mount，请先在宿主机执行："
  log "  sudo chown -R 1000:1000 <宿主机数据目录>"
  exit 1
fi
log "数据目录：${DATA_DIR}"

if [ -n "${ECHOIC_DATABASE_URL:-}" ]; then
  log "已配置外部数据库（ECHOIC_DATABASE_URL）"
fi

# ---------- 2) 可选：等待数据库就绪 ----------
if [ "$WAIT_FOR_DB" = "true" ] && [ -n "${ECHOIC_DATABASE_URL:-}" ]; then
  log "等待数据库连接就绪（最多 60 秒）..."
  python - <<'PY' || exit 1
import os
import socket
import sys
import time
from urllib.parse import urlparse

url = os.environ.get("ECHOIC_DATABASE_URL", "")
parsed = urlparse(url)
host = parsed.hostname
if parsed.scheme.startswith("sqlite") or not host:
    print("[entrypoint] SQLite 或未配置主机，跳过数据库等待")
    sys.exit(0)
port = parsed.port or 5432
deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"[entrypoint] 数据库 {host}:{port} 已就绪")
            sys.exit(0)
    except OSError:
        time.sleep(1)
print(f"[entrypoint] 错误：数据库 {host}:{port} 在 60 秒内未就绪", file=sys.stderr)
sys.exit(1)
PY
fi

# ---------- 3) 启动主进程 ----------
if [ "$#" -eq 0 ]; then
  set -- uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
fi

cd "$APP_DIR"
log "启动进程：$*"
exec "$@"
