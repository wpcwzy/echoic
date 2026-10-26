#!/usr/bin/env bash
# ============================================================================
# Echoic 开发模式一键启动：后端 uvicorn --reload (:8000) + 前端 vite (:5173)
#
# 用法：
#   ./scripts/dev.sh                    # 首次会自动创建 backend/.venv 并安装依赖
#   ./scripts/dev.sh --backend-only
#   ./scripts/dev.sh --frontend-only
#   BACKEND_PORT=8001 FRONTEND_PORT=5174 ./scripts/dev.sh
#
# 说明：
#   - 前端 dev server 已配置代理 /api -> http://127.0.0.1:8000（见 frontend/vite.config.ts），
#     若修改 BACKEND_PORT 请同步修改 vite.config.ts 的 proxy.target；
#   - Ctrl-C 会同时停止两个子进程；
#   - npm 使用仓库内 .cache/npm 作为缓存目录，避免污染 HOME（沙箱/CI 友好）。
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
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

log() { printf '\033[1;34m[dev]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[dev]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[dev]\033[0m %s\n' "$*" >&2; exit 1; }

RUN_BACKEND=1
RUN_FRONTEND=1
for arg in "$@"; do
  case "$arg" in
    --backend-only) RUN_FRONTEND=0 ;;
    --frontend-only) RUN_BACKEND=0 ;;
    -h|--help)
      sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "未知参数：$arg（可用：--backend-only / --frontend-only / --help）" ;;
  esac
done

# npm 的 HOME 在部分受限环境不可写：统一把缓存/日志放到仓库内
export npm_config_cache="$NPM_CACHE"
export npm_config_logs_dir="$NPM_LOGS_DIR"
mkdir -p "$NPM_CACHE" "$NPM_LOGS_DIR"

# ---------- 后端依赖 ----------
if [ "$RUN_BACKEND" = 1 ]; then
  if [ ! -x "$VENV/bin/python" ]; then
    command -v "$PYTHON" >/dev/null 2>&1 || die "未找到 $PYTHON，请先安装 Python 3.11+（推荐 3.12）"
    log "创建 Python 虚拟环境：$VENV"
    "$PYTHON" -m venv "$VENV"
    "$VENV/bin/python" -m pip install --upgrade pip
    "$VENV/bin/pip" install -r "$BACKEND_DIR/requirements-dev.txt"
  fi
  command -v ffmpeg >/dev/null 2>&1 \
    || warn "未找到 ffmpeg：真实音视频处理会失败；mock 演示模式仍可运行（安装：sudo apt install ffmpeg）"
fi

# ---------- 前端依赖 ----------
if [ "$RUN_FRONTEND" = 1 ] && [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  command -v "$NPM" >/dev/null 2>&1 || die "未找到 npm，请先安装 Node.js 20+（推荐 22）"
  if [ -f "$FRONTEND_DIR/package-lock.json" ]; then
    log "安装前端依赖（npm ci）..."
    (cd "$FRONTEND_DIR" && "$NPM" ci --no-audit --no-fund)
  else
    log "安装前端依赖（未找到 lockfile，使用 npm install）..."
    (cd "$FRONTEND_DIR" && "$NPM" install --no-audit --no-fund)
  fi
fi

# ---------- 启动子进程 ----------
PIDS=()
cleanup() {
  trap - EXIT INT TERM
  log "正在停止子进程..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [ "$RUN_BACKEND" = 1 ]; then
  log "后端 API：http://127.0.0.1:${BACKEND_PORT}（调试模式可访问 /api/docs）"
  ( cd "$BACKEND_DIR" && exec "$VENV/bin/uvicorn" app.main:app \
      --host "$BACKEND_HOST" --port "$BACKEND_PORT" --reload ) &
  PIDS+=("$!")
fi

if [ "$RUN_FRONTEND" = 1 ]; then
  log "前端页面：http://localhost:${FRONTEND_PORT}（/api 代理到 :8000）"
  ( cd "$FRONTEND_DIR" && exec "$NPM" run dev -- --port "$FRONTEND_PORT" --host 0.0.0.0 ) &
  PIDS+=("$!")
fi

[ "${#PIDS[@]}" -gt 0 ] || die "没有需要启动的服务"
log "按 Ctrl-C 停止全部服务"
wait -n "${PIDS[@]}" || true
log "有服务退出，正在清理其余进程"
