#!/usr/bin/env bash
# Build converter-api and deploy it to the local kind cluster.
#
# Usage:  make deploy-local     (creates the cluster first if needed)
#    or:  scripts/deploy-local.sh   with kubectl pointed at a kind cluster
#
# Then:   make port-forward  and  curl http://localhost:8000/health
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Same name as the manifest, so the pods use the image loaded below.
IMAGE="$("${ROOT_DIR}/scripts/image-name.sh"):latest"

for tool in docker kubectl kind; do
  command -v "$tool" > /dev/null || { echo "error: '$tool' is not installed" >&2; exit 1; }
done

CONTEXT="$(kubectl config current-context)"
echo "kubectl context: ${CONTEXT}"
if [[ "$CONTEXT" != kind-* ]]; then
  # Only kind gets the locally built image loaded. Anything else would silently pull the
  # published :latest from the registry instead of your local build.
  echo "error: '${CONTEXT}' is not a kind cluster. Run 'make cluster-up' first." >&2
  exit 1
fi

"${ROOT_DIR}/scripts/build.sh" latest
# kind nodes can't see your local Docker images, so copy the image in.
kind load docker-image "$IMAGE" --name "${CONTEXT#kind-}"

kubectl apply -f "${ROOT_DIR}/k8s/namespace.yaml"
kubectl apply -f "${ROOT_DIR}/k8s/converter-api/"
# Same ':latest' tag as last time, so force new pods to pick up the rebuilt image.
kubectl -n devtoolkit rollout restart deployment/converter-api
kubectl -n devtoolkit rollout status deployment/converter-api --timeout=120s

echo
echo "Deployed. Try it:"
echo "  make port-forward"
echo "  curl http://localhost:8000/health"
