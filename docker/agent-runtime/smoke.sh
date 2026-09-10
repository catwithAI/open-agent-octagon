#!/usr/bin/env bash
# 镜像冒烟：逐个 CLI --version 与镜像标签比对；MCP 依赖与场景依赖可 import。
#   docker/agent-runtime/smoke.sh <image>
set -euo pipefail
image="${1:?image}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
label() { docker image inspect --format "{{index .Config.Labels \"$1\"}}" "${image}"; }
run() { docker run --rm --entrypoint "" "${image}" "$@"; }

fail=0
check() {
  local name="$1" expected="$2"; shift 2
  local out
  out="$(run "$@" 2>&1 || true)"
  if grep -qF "${expected}" <<<"${out}"; then
    echo "ok   ${name} ${expected}"
  else
    echo "FAIL ${name}: 期望含 ${expected}，实际：${out}" >&2; fail=1
  fi
}
check claude-code "$(label octagon.agent.claude-code.version)" claude --version
check codex       "$(label octagon.agent.codex.version)"       codex --version
check kimi-code   "$(label octagon.agent.kimi-code.version)"   kimi --version
check opencode    "$(label octagon.agent.opencode.version)"    opencode --version
check mimo-code   "$(label octagon.agent.mimo-code.version)"   mimo --version
check dsh         "dsh-runtime"                                ls -l /opt/dsh/runtime/dsh-runtime

mods="$(grep -hv '^\s*#' "${here}/requirements-base.txt" "${here}/requirements-envs.txt" \
        | sed -E 's/[<>=!~ ].*//; s/#.*//; /^\s*$/d' | tr '\n' ' ')"
for m in ${mods}; do
  py="${m//-/_}"
  check "import ${m}" "ok" python3 -c "import ${py}; print('ok')"
done
run python3 -c "import mcp.server.fastmcp; print('ok')" >/dev/null && echo "ok   mcp.server.fastmcp"
# 非 root、HOME 可写
check "home writable" "ok" sh -c 'touch /home/agent/.probe && echo ok'
[ "${fail}" -eq 0 ] && echo "smoke: 全部通过" || { echo "smoke: 有失败项" >&2; exit 1; }
