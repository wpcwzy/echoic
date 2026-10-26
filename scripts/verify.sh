#!/usr/bin/env bash
# ============================================================================
# Echoic 一键本地验证（CI / 发布前自检入口）
#
# 覆盖：
#   1) 后端单元 + 集成测试（pytest）
#   2) 前端类型检查 + 生产构建（tsc + vite build）
#   3) 生产式启动服务（临时数据目录）并检查 /api/system/health
#   4) 若存在 e2e/smoke*.py，则以生产式临时服务运行端到端冒烟
#      （v0.1 30 项 + v0.2 34 项 + v0.3：回收站/批量/实时 mock WS/向量检索），
#      日志写入临时目录，不会覆盖 e2e/last-run*.log
#
# 用法：
#   ./scripts/verify.sh                 # 全量
#   ./scripts/verify.sh --skip-tests    # 跳过 pytest
#   ./scripts/verify.sh --skip-build    # 跳过前端构建（复用现有 dist）
#   ./scripts/verify.sh --port 8100
#   VERIFY_KEEP_DATA=1 ./scripts/verify.sh   # 保留临时数据目录便于排查
#
# 退出码：0 = 全部通过；1 = 存在失败步骤。
# ============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"
VENV="$BACKEND_DIR/.venv"
PORT="${VERIFY_PORT:-8100}"

SKIP_TESTS=0
SKIP_BUILD=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-tests) SKIP_TESTS=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --port) PORT="${2:?--port 需要端口号}"; shift 2 ;;
    --port=*) PORT="${1#--port=}"; shift ;;
    -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1（可用：--skip-tests / --skip-build / --port N / --help）" >&2; exit 2 ;;
  esac
done

PASS=0
FAIL=0
SKIP=0
FAILED_STEPS=()
DATA_DIR="${VERIFY_DATA_DIR:-$(mktemp -d /tmp/echoic-verify-XXXXXX)}"
SERVER_PID=""

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok() { PASS=$((PASS + 1)); printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); FAILED_STEPS+=("$*"); printf '\033[1;31m[FAIL]\033[0m %s\n' "$*"; }
skip() { SKIP=$((SKIP + 1)); printf '\033[1;33m[SKIP]\033[0m %s\n' "$*"; }

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [ "${VERIFY_KEEP_DATA:-0}" != "1" ] && [ -d "$DATA_DIR" ]; then
    rm -rf "$DATA_DIR"
  elif [ -d "$DATA_DIR" ]; then
    printf '[verify] 数据目录已保留：%s\n' "$DATA_DIR"
  fi
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
step "环境检查"
# ---------------------------------------------------------------------------
if [ -x "$VENV/bin/python" ]; then
  ok "backend/.venv Python $("$VENV/bin/python" -V 2>&1 | awk '{print $2}')"
else
  skip "backend/.venv 不存在（跳过后端测试；可运行 scripts/dev.sh 自动创建）"
fi
if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg 可用（$(command -v ffmpeg)）"
else
  skip "ffmpeg 不可用（真实音视频处理用例应自动跳过）"
fi
if command -v node >/dev/null 2>&1; then
  ok "Node.js $(node -v)"
else
  skip "Node.js 不可用（跳过前端检查）"
fi

# ---------------------------------------------------------------------------
step "后端测试（pytest）"
# ---------------------------------------------------------------------------
if [ "$SKIP_TESTS" = 1 ]; then
  skip "后端测试（--skip-tests）"
elif [ ! -x "$VENV/bin/python" ]; then
  skip "后端测试（缺少 backend/.venv）"
else
  if ( cd "$BACKEND_DIR" && "$VENV/bin/python" -m pytest -q ); then
    ok "后端测试"
  else
    fail "后端测试"
  fi
fi

# ---------------------------------------------------------------------------
step "前端类型检查 + 构建"
# ---------------------------------------------------------------------------
if [ "$SKIP_BUILD" = 1 ]; then
  skip "前端构建（--skip-build）"
elif ! command -v npm >/dev/null 2>&1; then
  skip "前端构建（缺少 npm）"
elif [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  skip "前端构建（frontend/node_modules 缺失，先运行 scripts/dev.sh）"
else
  export npm_config_cache="${NPM_CACHE:-$REPO_ROOT/.cache/npm}"
  export npm_config_logs_dir="${NPM_LOGS_DIR:-$REPO_ROOT/.cache/npm-logs}"
  mkdir -p "$npm_config_cache" "$npm_config_logs_dir"
  if ( cd "$FRONTEND_DIR" && npm run typecheck ); then
    ok "前端类型检查（tsc --noEmit）"
  else
    fail "前端类型检查（tsc --noEmit）"
  fi
  if ( cd "$FRONTEND_DIR" && npm run build ); then
    ok "前端构建（vite build）"
  else
    fail "前端构建（vite build）"
  fi
fi

# ---------------------------------------------------------------------------
step "启动服务并健康检查（端口 $PORT，临时数据目录）"
# ---------------------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  skip "服务健康检查（缺少 backend/.venv）"
elif [ ! -f "$FRONTEND_DIR/dist/index.html" ]; then
  skip "服务健康检查（缺少 frontend/dist，先构建前端）"
else
  export ECHOIC_DATA_DIR="$DATA_DIR"
  export ECHOIC_STATIC_DIR="$FRONTEND_DIR/dist"
  export ECHOIC_DEMO_SEED="${ECHOIC_DEMO_SEED:-true}"
  export ECHOIC_EMBEDDED_WORKER="${ECHOIC_EMBEDDED_WORKER:-true}"
  export ECHOIC_SECRET_KEY="${ECHOIC_SECRET_KEY:-verify-secret-key}"
  # v0.2 consume 自动导入：隔离目录（与脚本同 shell，e2e 可直接投放文件）
  export ECHOIC_CONSUME_ENABLED="${ECHOIC_CONSUME_ENABLED:-true}"
  export ECHOIC_CONSUME_DIR="${ECHOIC_CONSUME_DIR:-$DATA_DIR/consume}"
  mkdir -p "$ECHOIC_CONSUME_DIR"
  # v0.3 实时转写 Beta（mock provider，无需 Key）与向量检索（hot 配置由 e2e 写入 mock embedding）
  export ECHOIC_LIVE_ENABLED="${ECHOIC_LIVE_ENABLED:-true}"
  export ECHOIC_LIVE_PROVIDER="${ECHOIC_LIVE_PROVIDER:-mock}"
  (
    cd "$BACKEND_DIR" && exec "$VENV/bin/uvicorn" app.main:app \
      --host 127.0.0.1 --port "$PORT" --proxy-headers
  ) >"$DATA_DIR/server.log" 2>&1 &
  SERVER_PID=$!

  HEALTH_URL="http://127.0.0.1:${PORT}/api/system/health"
  READY=0
  for _ in $(seq 1 60); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      break
    fi
    if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
      READY=1
      break
    fi
    sleep 1
  done
  if [ "$READY" = 1 ]; then
    ok "GET /api/system/health 返回 200"
    if curl -fsS --max-time 5 "$HEALTH_URL" | grep -q '"status"'; then
      ok "健康检查响应包含 status 字段"
    else
      fail "健康检查响应格式异常"
    fi
  else
    fail "服务启动/健康检查失败（日志：$DATA_DIR/server.log）"
    tail -n 30 "$DATA_DIR/server.log" 2>/dev/null || true
  fi

  # ---- 可选：e2e 冒烟 ----
  if [ "$READY" = 1 ] && [ -f "$REPO_ROOT/e2e/smoke.py" ]; then
    step "E2E 冒烟（e2e/smoke.py）"
    if ( cd "$REPO_ROOT" && ECHOIC_E2E_BASE_URL="http://127.0.0.1:${PORT}" \
           ECHOIC_E2E_LOG="$DATA_DIR/smoke.log" "$VENV/bin/python" e2e/smoke.py ); then
      ok "E2E v0.1 冒烟（30 项）"
    else
      fail "E2E v0.1 冒烟（详情见 $DATA_DIR/smoke.log）"
    fi
  else
    skip "E2E v0.1 冒烟（e2e/smoke.py 不存在或服务未就绪）"
  fi

  # ---- 可选：v0.2 e2e（角色/权限、分享、consume）----
  if [ "$READY" = 1 ] && [ -f "$REPO_ROOT/e2e/smoke_v02.py" ]; then
    step "E2E v0.2 冒烟（e2e/smoke_v02.py）"
    if ( cd "$REPO_ROOT" && ECHOIC_E2E_BASE_URL="http://127.0.0.1:${PORT}" \
           ECHOIC_E2E_LOG_V02="$DATA_DIR/smoke-v02.log" \
           ECHOIC_E2E_CONSUME_DIR="$ECHOIC_CONSUME_DIR" \
           "$VENV/bin/python" e2e/smoke_v02.py ); then
      ok "E2E v0.2 冒烟（34 项）"
    else
      fail "E2E v0.2 冒烟（详情见 $DATA_DIR/smoke-v02.log）"
    fi
  else
    skip "E2E v0.2 冒烟（e2e/smoke_v02.py 不存在或服务未就绪）"
  fi

  # ---- 可选：v0.3 e2e（回收站/批量、实时 mock WS、向量检索）----
  if [ "$READY" = 1 ] && [ -f "$REPO_ROOT/e2e/smoke_v03.py" ]; then
    step "E2E v0.3 冒烟（e2e/smoke_v03.py）"
    if ( cd "$REPO_ROOT" && ECHOIC_E2E_BASE_URL="http://127.0.0.1:${PORT}" \
           ECHOIC_E2E_LOG_V03="$DATA_DIR/smoke-v03.log" \
           ECHOIC_E2E_DATA_DIR="$DATA_DIR" \
           ECHOIC_E2E_CONSUME_DIR="$ECHOIC_CONSUME_DIR" \
           "$VENV/bin/python" e2e/smoke_v03.py --data-dir "$DATA_DIR" ); then
      ok "E2E v0.3 冒烟"
    else
      fail "E2E v0.3 冒烟（详情见 $DATA_DIR/smoke-v03.log）"
    fi
  else
    skip "E2E v0.3 冒烟（e2e/smoke_v03.py 不存在或服务未就绪）"
  fi
fi

# ---------------------------------------------------------------------------
printf '\n\033[1m验证结果：\033[0m%d 通过 / %d 失败 / %d 跳过\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -gt 0 ]; then
  printf '失败步骤：\n'
  for s in "${FAILED_STEPS[@]}"; do printf '  - %s\n' "$s"; done
  exit 1
fi
printf '\033[1;32m全部检查通过 ✔\033[0m\n'
printf 'e2e 冒烟日志（如已运行）：%s/smoke.log（v0.1）、%s/smoke-v02.log（v0.2）、%s/smoke-v03.log（v0.3）\n' "$DATA_DIR" "$DATA_DIR" "$DATA_DIR"
exit 0
