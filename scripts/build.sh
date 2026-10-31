#!/usr/bin/env bash
# Build the converter-api image locally.
# Usage: scripts/build.sh [tag]     (default tag: latest)
# The image name comes from k8s/converter-api/deployment.yaml (see image-name.sh).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_REPO="$("${ROOT_DIR}/scripts/image-name.sh")"
TAG="${1:-latest}"

echo "Building ${IMAGE_REPO}:${TAG}"
docker build -t "${IMAGE_REPO}:${TAG}" "${ROOT_DIR}/services/converter-api"
