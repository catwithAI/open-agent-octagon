#!/usr/bin/env bash
# 本地构建 agent 运行时镜像。
#   docker/agent-runtime/build.sh [tag] [envs_path ...]
# 缺省 tag octagon-agent-runtime:dev；envs_path 缺省 ./envs 与 OCTAGON_ENVS_PATH。
# CI 走 .github/workflows/sandbox-image.yml，同一 Dockerfile、同一 versions.env。
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"
tag="${1:-octagon-agent-runtime:dev}"
shift || true
envs=("$@")
if [ "${#envs[@]}" -eq 0 ]; then
  envs=("${root}/envs")
  [ -n "${OCTAGON_ENVS_PATH:-}" ] && envs+=("${OCTAGON_ENVS_PATH}")
fi

python3 "${here}/collect_env_requirements.py" "${envs[@]}" > "${here}/requirements-envs.generated.txt"
echo "场景依赖（generated）：" && cat "${here}/requirements-envs.generated.txt"

build_args=()
while IFS='=' read -r key value; do
  [[ -z "${key}" || "${key}" =~ ^# ]] && continue
  build_args+=("--build-arg" "${key}=${value}")
done < "${here}/versions.env"

docker build "${build_args[@]}" -f "${here}/Dockerfile" -t "${tag}" "${root}"
"${here}/smoke.sh" "${tag}"
