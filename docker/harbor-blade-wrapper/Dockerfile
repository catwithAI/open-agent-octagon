ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ARG BLADE_AGENT_KIT_VERSION=1.1.7
RUN pip install --no-cache-dir "blade-agent-kit==${BLADE_AGENT_KIT_VERSION}"
COPY docker/harbor-blade-wrapper/wrapper.py /usr/local/bin/harbor-blade-wrapper
RUN chmod 0755 /usr/local/bin/harbor-blade-wrapper && mkdir -p /app /home/agent /logs /tmp && chmod 0777 /app /home/agent /logs /tmp
ENV HOME=/home/agent PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/sbin:/bin
LABEL org.opencontainers.image.title="harbor-octagon-blade-wrapper" harbor.compat="true" harbor.agent="blade-agent"
WORKDIR /app
CMD ["sleep", "infinity"]
