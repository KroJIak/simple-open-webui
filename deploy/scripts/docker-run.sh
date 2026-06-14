#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

# ---------------------------------------------------------------------------
# Build and run the Open WebUI Docker container locally.
# ---------------------------------------------------------------------------

readonly IMAGE="open-webui"
readonly CONTAINER="open-webui"
readonly HOST_PORT="${OPEN_WEBUI_PORT:-3000}"
readonly CONTAINER_PORT=8080

echo "Building ${IMAGE} image..."
docker build -t "$IMAGE" "$ROOT_DIR"

echo "Stopping any existing ${CONTAINER} container..."
docker stop "$CONTAINER" 2>/dev/null || true
docker rm "$CONTAINER" 2>/dev/null || true

echo "Starting ${CONTAINER}..."
docker run -d \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  --add-host=host.docker.internal:host-gateway \
  -v "${IMAGE}:/app/backend/data" \
  --name "$CONTAINER" \
  --restart always \
  "$IMAGE"

echo "Cleaning up dangling images..."
docker image prune -f

echo "Open WebUI is running at http://localhost:${HOST_PORT}"
