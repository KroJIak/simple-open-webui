#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

# ---------------------------------------------------------------------------
# Tear down the compose project and remove all volumes (including data).
# ---------------------------------------------------------------------------

echo "WARNING: This will stop all containers and delete all volumes (including persistent data)."
read -rp "Are you sure you want to continue? [y/N]: " answer

if [[ "${answer,,}" =~ ^y(es)?$ ]]; then
  docker compose -f "$ROOT_DIR/docker-compose.yml" down -v
  echo "All containers and volumes have been removed."
else
  echo "Operation cancelled."
fi
