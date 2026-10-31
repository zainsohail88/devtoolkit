#!/usr/bin/env bash
# Print the converter-api image repository (no tag), e.g. ghcr.io/<owner>/devtoolkit/converter-api.
# k8s/converter-api/deployment.yaml is the single source of truth: scripts and CI read the
# name from there, so the local build always matches what the manifest deploys.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(sed -n -E 's#^[[:space:]]*image:[[:space:]]*([^:@[:space:]]+).*#\1#p' \
  "${ROOT_DIR}/k8s/converter-api/deployment.yaml" | head -n 1)"

if [[ -z "$repo" ]]; then
  echo "error: no image found in k8s/converter-api/deployment.yaml" >&2
  exit 1
fi
echo "$repo"
