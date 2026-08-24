#!/usr/bin/env bash
# 一键启动 Octagon 后端 + 前端 dev server。
#
# 用法:
#     ./start.sh                # 默认端口:后端 8100,前端 5172
#     ./start.sh --no-frontend  # 只起后端
#     ./start.sh --no-backend   # 只起前端
#
# 退出:Ctrl-C 一次,脚本会回收两个子进程。
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

if [[ -f "${ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ROOT}/.env"
    set +a
fi

BACKEND_PORT="${BACKEND_PORT:-8100}"
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${FRONTEND_PORT:-5172}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
START_BACKEND=1
START_FRONTEND=1

for arg in "$@"; do
    case "$arg" in
        --no-frontend) START_FRONTEND=0 ;;
        --no-backend)  START_BACKEND=0 ;;
        -h|--help)
            sed -n '2,11p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

# ---------- 前置检查 -------------------------------------------------------

if (( START_BACKEND )); then
    command -v uv >/dev/null 2>&1 || {
        echo "[start.sh] 找不到 uv,先装一下:https://docs.astral.sh/uv/" >&2
        exit 1
    }
fi

if (( START_FRONTEND )); then
    command -v npm >/dev/null 2>&1 || {
        echo "[start.sh] 找不到 npm,先装 Node.js" >&2
        exit 1
    }
    if [[ ! -d "${ROOT}/web/node_modules" ]]; then
        echo "[start.sh] web/node_modules 缺失,跑一次 npm install"
        (cd "${ROOT}/web" && npm install --silent)
    fi
fi

mkdir -p logs
BACKEND_LOG="${ROOT}/logs/backend.log"
FRONTEND_LOG="${ROOT}/logs/frontend.log"

# ---------- 子进程管理 ----------------------------------------------------
#
# `uv run uvicorn` 和 `npm run dev` 都是 wrapper,真正的 server 是 wrapper 的
# 子进程。在终端里 Ctrl-C 时整个前台进程组都会收到 SIGINT,wrapper 会传给
# server。**但**如果脚本被外部 `kill -INT <pid>` 显式杀,wrapper 是脚本的子,
# 不会自动传给孙子。所以 cleanup 用 `pkill -TERM -P $pid` 递归杀子树兜底。

PIDS=()

cleanup() {
    echo
    echo "[start.sh] 收到退出信号,回收子进程…"
    # 第一轮:wrapper 发 TERM(让 wrapper 自己优雅传给孙子),
    # 同时 -P 递归杀子树兜底
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            pkill -TERM -P "$pid" 2>/dev/null || true
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    sleep 1
    # 第二轮:仍存活的强杀
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            pkill -KILL -P "$pid" 2>/dev/null || true
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
}
trap cleanup INT TERM EXIT

# ---------- 端口清理 ------------------------------------------------------

kill_port() {
    local port=$1
    local pids
    pids=$(lsof -ti :"$port" 2>/dev/null) || true
    if [[ -n "$pids" ]]; then
        echo "[start.sh] 端口 ${port} 被占用 (pid ${pids// /, }),正在清理…"
        echo "$pids" | xargs kill 2>/dev/null || true
        sleep 1
        # 还活着就强杀
        pids=$(lsof -ti :"$port" 2>/dev/null) || true
        if [[ -n "$pids" ]]; then
            echo "$pids" | xargs kill -9 2>/dev/null || true
            sleep 0.5
        fi
    fi
}

(( START_BACKEND ))  && kill_port "$BACKEND_PORT"
(( START_FRONTEND )) && kill_port "$FRONTEND_PORT"

# ---------- 启动后端 ------------------------------------------------------

if (( START_BACKEND )); then
    # 先同步 uv 配置的项目环境，再以 --no-sync 启动。两步均由 uv 解析
    # UV_PROJECT_ENVIRONMENT，避免检查一个环境却从另一个 .venv 启动。
    if ! uv sync --locked --inexact --check >/dev/null 2>&1; then
        echo "[start.sh] Python 环境与 uv.lock 不一致，正在同步…"
        uv sync --locked --inexact
    fi
    UVICORN_CMD=(uv run --locked --no-sync uvicorn)
    echo "[start.sh] backend  → http://${BACKEND_HOST}:${BACKEND_PORT}  (logs/backend.log)"
    "${UVICORN_CMD[@]}" backend.main:create_app --factory \
        --host "${BACKEND_HOST}" --port "${BACKEND_PORT}" \
        > "${BACKEND_LOG}" 2>&1 &
    PIDS+=("$!")
fi

# ---------- 启动前端 ------------------------------------------------------

if (( START_FRONTEND )); then
    echo "[start.sh] frontend → http://${FRONTEND_HOST}:${FRONTEND_PORT}  (logs/frontend.log)"
    (cd "${ROOT}/web" && npm run dev -- --host "${FRONTEND_HOST}" --port "${FRONTEND_PORT}") \
        > "${FRONTEND_LOG}" 2>&1 &
    PIDS+=("$!")
fi

if (( ${#PIDS[@]} == 0 )); then
    echo "[start.sh] 没有要启动的服务" >&2
    exit 0
fi

echo "[start.sh] 已启动 ${#PIDS[@]} 个进程,Ctrl-C 退出。"

# 等待任一子进程退出;它一退,trap cleanup 会带走另一个
wait -n "${PIDS[@]}"
