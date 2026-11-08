#!/usr/bin/env bash
# Build a service's image locally.
# Usage: scripts/build.sh <service> [tag]     (default tag: latest)
# The image name comes from k8s/<service>/deployment.yaml (see image-name.sh).
set -euo pipefail

SERVICE="${1:?Usage: scripts/build.sh <service> [tag]}"
TAG="${2:-latest}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_REPO="$("${ROOT_DIR}/scripts/image-name.sh" "$SERVICE")"

echo "Building ${IMAGE_REPO}:${TAG}"
docker build -t "${IMAGE_REPO}:${TAG}" "${ROOT_DIR}/services/${SERVICE}"
