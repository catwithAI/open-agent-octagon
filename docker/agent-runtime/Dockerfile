# syntax=docker/dockerfile:1.7
# OpenAgentOctagon agent 运行时镜像 —— 「镜像即被测环境」
# （spec: docs/specs/260909-agent-sandbox，决策 D-03）。
#
# 六个本机 agent 的 CLI / runtime 全部打进来并钉版本：改任何一个版本就是一个
# 新的被测环境，必须发新 tag。这与后端 Dockerfile 的「CLI 不进镜像」相反：
# 后端镜像解决后端进程的资源隔离，本镜像解决被测环境本身要可锁定、可复现。
#
# 构建：docker/agent-runtime/build.sh（读 versions.env、合并场景依赖）。
# 运行：由 backend/process/launcher.py 的 DockerLauncher 以 sleep infinity 起容器，
# 每轮 docker exec；--user 取宿主机后端进程 uid/gid，所以 /home/agent 是 777。

ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

# buildx 注入 TARGETARCH；传统 builder 为空，RUN 里回落到 uname -m。
ARG TARGETARCH
ARG NODE_MAJOR=22
ARG CLAUDE_VERSION
ARG CODEX_VERSION
ARG KIMI_VERSION
ARG OPENCODE_VERSION
ARG MIMO_VERSION
ARG DSH_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---- 系统层 ---------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        git curl ca-certificates procps \
        build-essential g++ ripgrep unzip xz-utils jq less \
    && rm -rf /var/lib/apt/lists/*

# node（npm 安装的 CLI 需要；claude / codex / dsh runtime 是原生二进制）
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

# ---- MCP 依赖 + 场景依赖 --------------------------------------------------
COPY docker/agent-runtime/requirements-base.txt \
     docker/agent-runtime/requirements-envs.txt \
     docker/agent-runtime/requirements-envs.generated.txt \
     /tmp/req/
RUN pip3 install -r /tmp/req/requirements-base.txt \
                 -r /tmp/req/requirements-envs.txt \
                 -r /tmp/req/requirements-envs.generated.txt \
    && python3 -c "import mcp.server.fastmcp, httpx"

# ---- agent CLI ------------------------------------------------------------
# 每个 CLI 装完立刻 --version 并与 ARG 比对：镜像标签写的是 ARG，比对保证标签可信。

# claude-code：官方安装器装到 $HOME/.local，这里把 HOME 定向到 /opt/claude。
RUN test -n "${CLAUDE_VERSION}" \
    && mkdir -p /opt/claude \
    && HOME=/opt/claude bash -c 'curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_VERSION}"' \
    && ln -s /opt/claude/.local/bin/claude /usr/local/bin/claude \
    && chmod -R a+rX /opt/claude \
    && claude --version | grep -F "${CLAUDE_VERSION}"

# codex：GitHub release 的 musl 静态二进制。
RUN test -n "${CODEX_VERSION}" \
    && arch="${TARGETARCH:-$(uname -m)}" \
    && case "${arch}" in \
         amd64|x86_64) triple=x86_64-unknown-linux-musl ;; \
         arm64|aarch64) triple=aarch64-unknown-linux-musl ;; \
         *) echo "unsupported arch=${arch}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/codex.tgz \
         "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${triple}.tar.gz" \
    && tar -xzf /tmp/codex.tgz -C /tmp \
    && install -m 0755 "/tmp/codex-${triple}" /usr/local/bin/codex \
    && rm -f /tmp/codex.tgz "/tmp/codex-${triple}" \
    && codex --version | grep -F "${CODEX_VERSION}"

# kimi-code：pip 包 kimi-cli（bin: kimi）。
RUN test -n "${KIMI_VERSION}" \
    && pip3 install "kimi-cli==${KIMI_VERSION}" \
    && kimi --version | grep -F "${KIMI_VERSION}"

# opencode：npm 全局包。
RUN test -n "${OPENCODE_VERSION}" \
    && npm install -g "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force \
    && opencode --version | grep -F "${OPENCODE_VERSION}"

# mimo-code：小米官方安装器（与 Harbor 对齐）。npm 包 @mimo-ai/cli 无
# versions.env 钉的 0.1.9（ETARGET），且小米官方渠道就是 `curl
# https://mimo.xiaomi.com/install | bash`。该渠道**不可精确钉版本**——
# 装完用 `mimo --version` 记录实际版本（smoke.sh 对 mimo 只验可运行、不比对
# 钉点）。HOME 定向 /opt/mimo（仿 claude），确保非 root 运行时能读到二进制。
RUN test -n "${MIMO_VERSION}" \
    && mkdir -p /opt/mimo \
    && HOME=/opt/mimo bash -c 'curl -fsSL https://mimo.xiaomi.com/install | bash' \
    && ln -s /opt/mimo/.mimocode/bin/mimo /usr/local/bin/mimo \
    && chmod -R a+rX /opt/mimo \
    && mimo --version

# dsh runtime：从 wheel 抽出 stdio JSON-RPC runtime 二进制到 /opt/dsh/runtime。
RUN test -n "${DSH_VERSION}" \
    && pip3 install "deepseek-harness-runtime-bin==${DSH_VERSION}" \
    && src="$(python3 -c 'import deepseek_harness_runtime, os; print(os.path.join(os.path.dirname(deepseek_harness_runtime.__file__), "runtime"))')" \
    && mkdir -p /opt/dsh \
    && cp -r "${src}" /opt/dsh/runtime \
    && bin="$(ls /opt/dsh/runtime/dsh-jsonrpc-agent-pkg-linux-* | head -1)" \
    && chmod 0755 "${bin}" \
    && ln -s "${bin}" /opt/dsh/runtime/dsh-runtime \
    && chmod -R a+rX /opt/dsh \
    && test -x /opt/dsh/runtime/dsh-runtime

# ---- 运行时环境 -----------------------------------------------------------
# HOME 由 launcher 以 rw 挂载 sandbox_home；--user 是宿主机 uid，所以这里不 chown 而是 777。
#
# `lo`/`loroot`/`sysroot`/`fonts` 由 launcher 盖成匿名卷（可重建的运行时，
# 不该堆进 attempt 目录）。**匿名卷的属主与权限继承镜像里该路径的目录**：
# 镜像里不存在就是 root:root 0755，而容器以宿主机 uid 跑，agent 写不进去，
# LibreOffice 直接起不来。所以这里必须预建且 0777——与 /home/agent 同一个
# 理由：构建期不知道宿主机 uid，只能放权限。
# 这一串必须与 `SandboxSection.ephemeral_home_dirs` 的默认值保持一致。
# 名单来自 2026-09-21 对 330 个 attempt / 32.8G sandbox_home 的实测：
# .npm 5.77G、.tmp 3.95G、.cache 2.43G 是可安全回收的运行期缓存；
# lo/loroot/sysroot/apt/fonts 合计仅 1.1G（镜像里并没有 LibreOffice），留作兜底。
# 漏掉任何一项，该目录的匿名卷就会是 root:root 0755，agent 写不进去。
RUN mkdir -p /home/agent /attempt \
    && mkdir -p /home/agent/.npm /home/agent/.cache /home/agent/.tmp \
                /home/agent/lo /home/agent/loroot /home/agent/sysroot \
                /home/agent/fonts /home/agent/.fonts /home/agent/apt \
    && chmod 0777 /home/agent /home/agent/.npm /home/agent/.cache \
                  /home/agent/.tmp /home/agent/lo /home/agent/loroot \
                  /home/agent/sysroot /home/agent/fonts /home/agent/.fonts \
                  /home/agent/apt
ENV HOME=/home/agent \
    PATH=/usr/local/bin:/usr/bin:/bin \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DSH_TELEMETRY_DISABLED=1

# 镜像标签：后端启动检查 `docker image inspect` 读 octagon.agents 判断启用的
# agent 是否都在，运行时把版本写进 security_meta。
LABEL org.opencontainers.image.title="octagon-agent-runtime" \
      octagon.agents="claude-code,codex,kimi-code,opencode,mimo-code,dsh" \
      octagon.agent.claude-code.version="${CLAUDE_VERSION}" \
      octagon.agent.codex.version="${CODEX_VERSION}" \
      octagon.agent.kimi-code.version="${KIMI_VERSION}" \
      octagon.agent.opencode.version="${OPENCODE_VERSION}" \
      octagon.agent.mimo-code.version="${MIMO_VERSION}" \
      octagon.agent.dsh.version="${DSH_VERSION}"

WORKDIR /home/agent
CMD ["sleep", "infinity"]
