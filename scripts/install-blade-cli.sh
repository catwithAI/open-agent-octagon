#!/usr/bin/env bash
# Build the CLI matching the SDK release into this worktree's virtual environment.
set -euo pipefail

BLADE_RELEASE="v2612.0.0-beta.41"
BLADE_COMMIT="8690d275ddb1e2a760559b10aaf841ba3ca881b2"
OCTAGON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BLADE_INSTALL_PATH="${1:-${OCTAGON_ROOT}/.venv/bin/blade}"

for required in gh git go make; do
    command -v "$required" >/dev/null || {
        echo "Missing build dependency: $required" >&2
        exit 1
    }
done

BLADE_BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/octagon-blade-cli.XXXXXX")"
trap 'rm -rf "$BLADE_BUILD_DIR"' EXIT

gh repo clone blade-hq/blade-agent "$BLADE_BUILD_DIR/source" -- \
    --depth 1 --filter=blob:none --sparse --branch "$BLADE_RELEASE"
git -C "$BLADE_BUILD_DIR/source" sparse-checkout set builtin/clis/blade-cli daemonrpc
BLADE_RESOLVED_COMMIT="$(git -C "$BLADE_BUILD_DIR/source" rev-parse HEAD)"
if [[ "$BLADE_RESOLVED_COMMIT" != "$BLADE_COMMIT" ]]; then
    echo "Release commit mismatch: $BLADE_RESOLVED_COMMIT" >&2
    exit 1
fi

# make build targets the host OS/architecture; build-all produces a Linux binary.
GOWORK=off make -C "$BLADE_BUILD_DIR/source/builtin/clis/blade-cli" \
    build VERSION="${BLADE_RELEASE#v}"
mkdir -p "$(dirname "$BLADE_INSTALL_PATH")"
install -m 755 "$BLADE_BUILD_DIR/source/builtin/clis/blade-cli/dist/blade" \
    "$BLADE_INSTALL_PATH"
"$BLADE_INSTALL_PATH" version
