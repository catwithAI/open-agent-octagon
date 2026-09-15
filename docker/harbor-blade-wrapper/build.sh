#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"
tag="${1:-harbor-octagon-blade-wrapper:dev}"
version="${BLADE_AGENT_KIT_VERSION:-1.1.7}"
# Do not use a remote digest-pinned base by default: Docker Desktop may have
# the required Python image only under a local tag, and BuildKit then tries to
# contact the registry for metadata before executing the build. --pull=false
# plus this local preflight makes the failure explicit instead of invoking a
# broken credential helper.
base="${HARBOR_BLADE_WRAPPER_BASE_IMAGE:-python:3.12-slim}"
if ! docker image inspect "${base}" >/dev/null 2>&1; then
  echo "local base image is missing: ${base}" >&2
  echo "set HARBOR_BLADE_WRAPPER_BASE_IMAGE to an already-local Python image" >&2
  exit 2
fi
docker build --pull=false --build-arg "BASE_IMAGE=${base}" --build-arg "BLADE_AGENT_KIT_VERSION=${version}" -f "${here}/Dockerfile" -t "${tag}" "${root}"
docker run --rm --pull=never "${tag}" python3 -c 'import blade_agent_kit; print(blade_agent_kit.__file__)'
