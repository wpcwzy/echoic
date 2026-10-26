#!/usr/bin/env bash
# ============================================================================
# Echoic 本机「类生产」启动：构建前端 → FastAPI 同源托管静态资源 → uvicorn :8100
# 与 Docker 镜像行为一致（单进程同时提供 API 与前端页面），用于本地验收与 e2e。
#
# 用法：
#   ./scripts/run-local.sh                  # 构建前端并启动（数据目录默认 ./data）
#   ./scripts/run-local.sh --no-build       # 跳过构建，复用已有 frontend/dist
#   ./scripts/run-local.sh --copy           # 把 dist 拷贝到 backend/app/static 后再启动
#   HOST=127.0.0.1 PORT=9000 ./scripts/run-local.sh
#
# 环境变量（与后端一致，按需覆盖）：
#   ECHOIC_DATA_DIR=/tmp/echoic-e2e ECHOIC_DEMO_SEED=true ECHOIC_SECRET_KEY=xxx \
#     ./scripts/run-local.sh
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"
VENV="$BACKEND_DIR/.venv"
PYTHON="${PYTHON:-python3}"
NPM="${NPM:-npm}"
NPM_CACHE="${NPM_CACHE:-$REPO_ROOT/.cache/npm}"
NPM_LOGS_DIR="${NPM_LOGS_DIR:-$REPO_ROOT/.cache/npm-logs}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8100}"

DO_BUILD=1
DO_COPY=0
for arg in "$@"; do
  case "$arg" in
    --no-build) DO_BUILD=0 ;;
    --copy) DO_COPY=1 ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg（可用：--no-build / --copy / --help）" >&2; exit 1 ;;
  esac
done

log() { printf '\033[1;34m[run-local]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[run-local]\033[0m %s\n' "$*" >&2; exit 1; }

export npm_config_cache="$NPM_CACHE"
export npm_config_logs_dir="$NPM_LOGS_DIR"
mkdir -p "$NPM_CACHE" "$NPM_LOGS_DIR"

# ---------- 后端依赖 ----------
if [ ! -x "$VENV/bin/python" ]; then
  command -v "$PYTHON" >/dev/null 2>&1 || die "未找到 $PYTHON，请先安装 Python 3.11+（推荐 3.12）"
  log "创建 Python 虚拟环境：$VENV"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --upgrade pip
  "$VENV/bin/pip" install -r "$BACKEND_DIR/requirements.txt"
fi
command -v ffmpeg >/dev/null 2>&1 || log "警告：未找到 ffmpeg，真实音视频上传处理会失败（演示数据仍可用）"

# ---------- 构建前端 ----------
if [ "$DO_BUILD" = 1 ]; then
  command -v "$NPM" >/dev/null 2>&1 || die "未找到 npm，请先安装 Node.js 20+（推荐 22）"
  if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    if [ -f "$FRONTEND_DIR/package-lock.json" ]; then
      log "安装前端依赖（npm ci）..."
      ( cd "$FRONTEND_DIR" && "$NPM" ci --no-audit --no-fund )
    else
      log "安装前端依赖（未找到 lockfile，使用 npm install）..."
      ( cd "$FRONTEND_DIR" && "$NPM" install --no-audit --no-fund )
    fi
  fi
  log "构建前端（npm run build）..."
  ( cd "$FRONTEND_DIR" && "$NPM" run build )
fi

[ -f "$FRONTEND_DIR/dist/index.html" ] \
  || die "前端构建产物缺失：$FRONTEND_DIR/dist/index.html（去掉 --no-build 或先执行 cd frontend && npm run build）"

# ---------- 静态资源位置 ----------
if [ "$DO_COPY" = 1 ]; then
  log "同步前端产物：frontend/dist -> backend/app/static"
  rm -rf "$BACKEND_DIR/app/static"
  cp -a "$FRONTEND_DIR/dist" "$BACKEND_DIR/app/static"
else
  # 不污染源码树：直接让后端托管 frontend/dist
  export ECHOIC_STATIC_DIR="${ECHOIC_STATIC_DIR:-$FRONTEND_DIR/dist}"
fi

# ---------- 运行时环境（本机默认值，可被外部环境变量覆盖）----------
export ECHOIC_DATA_DIR="${ECHOIC_DATA_DIR:-$REPO_ROOT/data}"
export ECHOIC_DEMO_SEED="${ECHOIC_DEMO_SEED:-true}"
export ECHOIC_EMBEDDED_WORKER="${ECHOIC_EMBEDDED_WORKER:-true}"

log "数据目录：$ECHOIC_DATA_DIR"
log "静态资源：${ECHOIC_STATIC_DIR:-$BACKEND_DIR/app/static（--copy 模式）}"
log "健康检查：http://127.0.0.1:${PORT}/api/system/health"
log "访问地址：http://127.0.0.1:${PORT}（按 Ctrl-C 停止）"

cd "$BACKEND_DIR"
exec "$VENV/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT" --proxy-headers
