# Minimal Harbor pilot runtime: Codex only.
# This is deliberately separate from docker/agent-runtime, which installs all
# six Octagon CLIs and all environment dependencies. Building this image avoids
# spending pilot disk on unrelated agents/environments.
ARG BASE_IMAGE=python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3
FROM ${BASE_IMAGE}

ARG CODEX_VERSION=0.149.1
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH:-amd64}" in \
         amd64) triple=x86_64-unknown-linux-musl ;; \
         arm64) triple=aarch64-unknown-linux-musl ;; \
         *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/codex.tgz \
         "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${triple}.tar.gz" \
    && tar -xzf /tmp/codex.tgz -C /tmp \
    && install -m 0755 "/tmp/codex-${triple}" /usr/local/bin/codex \
    && rm -f /tmp/codex.tgz "/tmp/codex-${triple}" \
    && codex --version | grep -F "${CODEX_VERSION}"

RUN mkdir -p /home/agent /logs /tmp && chmod 0777 /home/agent /logs /tmp
ENV HOME=/home/agent PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/sbin:/bin DISABLE_AUTOUPDATER=1
LABEL org.opencontainers.image.title="harbor-octagon-codex-runtime" \
      octagon.agents="codex" \
      octagon.agent.codex.version="${CODEX_VERSION}"
WORKDIR /home/agent
CMD ["sleep", "infinity"]
