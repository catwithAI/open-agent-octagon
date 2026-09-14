#!/usr/bin/env bash
# Build only the Codex runtime needed by the Harbor pilot.
# No Harbor task image is pulled by this script; use --pull=false.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"
tag="${1:-harbor-octagon-codex-runtime:dev}"
base="${HARBOR_CODEX_BASE_IMAGE:-}"
version="${HARBOR_CODEX_VERSION:-0.149.1}"
if [[ -z "${base}" ]]; then
  echo "HARBOR_CODEX_BASE_IMAGE must name an already-local image (use the selected Harbor task image)." >&2
  exit 2
fi
docker build --pull=false --build-arg "BASE_IMAGE=${base}" --build-arg "CODEX_VERSION=${version}" -f "${here}/Dockerfile" -t "${tag}" "${root}"
docker run --rm --pull=never "${tag}" codex --version
printf 'Set HARBOR_OCTAGON_RUNTIME_IMAGE=%s before running the pilot.\n' "${tag}"
