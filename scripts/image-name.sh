#!/usr/bin/env bash
# Print a service's image repository (no tag), e.g. ghcr.io/<owner>/devtoolkit/converter-api.
# Usage: scripts/image-name.sh <service>        (a directory under services/ and k8s/)
# k8s/<service>/deployment.yaml is the single source of truth: scripts and CI read the name
# from there, so the local build always matches what the manifest deploys.
set -euo pipefail

SERVICE="${1:?Usage: scripts/image-name.sh <service>}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="${ROOT_DIR}/k8s/${SERVICE}/deployment.yaml"

[[ -f "$MANIFEST" ]] || { echo "error: no such service: ${MANIFEST} not found" >&2; exit 1; }
repo="$(sed -n -E 's#^[[:space:]]*image:[[:space:]]*([^:@[:space:]]+).*#\1#p' "$MANIFEST" | head -n 1)"

if [[ -z "$repo" ]]; then
  echo "error: no image found in ${MANIFEST}" >&2
  exit 1
fi
echo "$repo"
