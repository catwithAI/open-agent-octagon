# OpenAgentOctagon 后端镜像。
#
# **边界：只装后端 API，不装 agent CLI，不含前端。**
#
# 各 agent CLI（claude / codex 等）是**被测对象**，不是后端的运行时依赖，
# 所以不进**本**镜像：本镜像解决的是后端进程的资源隔离与自动重启。
# 被测环境本身由另一个镜像定义——docker/agent-runtime/Dockerfile 把全部可测
# agent CLI 钉版本打进「agent 运行时镜像」，每个版本就是一个可锁定、可复现的
# 被测环境（spec: docs/specs/260909-agent-sandbox）。沙盒关闭时 CLI 仍走
# bind mount 从宿主机注入（见 docker-compose.yaml）。
# 注意：沙盒模式要求后端跑在宿主机（同路径挂载 + docker CLI），容器化后端
# 与沙盒模式当前不兼容，启动检查会报 sandbox_unavailable。
#
# 前端（web/）不在本镜像内：本镜像只提供 API（默认发行形态）。需要前端时在源码
# 侧 `npm run dev` 或单独部署静态资源。
#
# 场景目录（envs）也不打进镜像：通过 volume 挂载 + 配置 envs_path 注入（见 compose）。

FROM python:3.12-slim AS base

# uv：与本地开发同一套依赖解析，避免「本地能跑、镜像里版本不同」。
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# 运行时系统依赖，保持最小：
# - git：部分 env / agent 工作区需要；
# - curl：容器健康检查用。
RUN apt-get update && apt-get install --no-install-recommends -y \
        git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- 依赖层：先只拷依赖清单，改代码不必重装依赖 ----------------------------
COPY pyproject.toml uv.lock ./
# 只装依赖、不装项目本身（项目代码在下一层拷进来），最大化缓存命中。
RUN uv sync --frozen --no-install-project --no-dev

# ---- 代码层 ---------------------------------------------------------------
COPY backend/ ./backend/
COPY octagon/ ./octagon/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# 非 root 运行。UID/GID 1000 与常见宿主机首个用户对齐，使 bind mount 进来的
# data/ 与 agent CLI 目录权限可用；按需在 compose 里覆盖。
RUN groupadd -g 1000 octagon 2>/dev/null || true \
    && useradd -u 1000 -g 1000 -m -s /bin/bash octagon 2>/dev/null || true \
    && mkdir -p /app/data && chown -R 1000:1000 /app
USER 1000:1000

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8100/api/healthz || exit 1

CMD ["python", "-m", "uvicorn", "backend.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8100"]
