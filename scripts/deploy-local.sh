#!/usr/bin/env bash
# Build every service and deploy them all to the local kind cluster.
#
# Usage:  make deploy-local     (creates the cluster first if needed)
#    or:  scripts/deploy-local.sh   with kubectl pointed at a kind cluster
#
# Then:   make port-forward  and open http://localhost:8080
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES=(converter-api web)

for tool in docker kubectl kind; do
  command -v "$tool" > /dev/null || { echo "error: '$tool' is not installed" >&2; exit 1; }
done

CONTEXT="$(kubectl config current-context)"
echo "kubectl context: ${CONTEXT}"
if [[ "$CONTEXT" != kind-* ]]; then
  # Only kind gets the locally built images loaded. Anything else would silently pull the
  # published :latest from the registry instead of your local build.
  echo "error: '${CONTEXT}' is not a kind cluster. Run 'make cluster-up' first." >&2
  exit 1
fi

for service in "${SERVICES[@]}"; do
  "${ROOT_DIR}/scripts/build.sh" "$service" latest
  # kind nodes can't see your local Docker images, so copy the image in.
  # Same name as the manifest, so the pods use this image instead of pulling.
  kind load docker-image "$("${ROOT_DIR}/scripts/image-name.sh" "$service"):latest" \
    --name "${CONTEXT#kind-}"
done

kubectl apply -f "${ROOT_DIR}/k8s/namespace.yaml"
for service in "${SERVICES[@]}"; do
  kubectl apply -f "${ROOT_DIR}/k8s/${service}/"
done
for service in "${SERVICES[@]}"; do
  # Same ':latest' tag as last time, so force new pods to pick up the rebuilt image.
  kubectl -n devtoolkit rollout restart "deployment/${service}"
done
for service in "${SERVICES[@]}"; do
  kubectl -n devtoolkit rollout status "deployment/${service}" --timeout=120s
done

echo
echo "Deployed. Try it:"
echo "  make port-forward"
echo "  open http://localhost:8080"
