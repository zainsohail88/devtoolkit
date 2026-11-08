# Architecture

## Project layout

```
services/
  converter-api/          FastAPI service
    main.py               HTTP endpoints
    converters/           Conversion logic (pure functions, no FastAPI)
    tests/                pytest tests
    Dockerfile            Multi-stage build, non-root user
k8s/
  namespace.yaml          devtoolkit namespace
  converter-api/          Deployment, Service, ConfigMap
.github/
  workflows/              CI, image publishing, GKE deploy, CodeQL
  CODEOWNERS, dependabot.yml, issue and PR templates
scripts/                  build.sh, deploy-local.sh, image-name.sh, kind-cluster.yaml
docs/                     This documentation
Makefile                  Common commands (`make help`)
.tool-versions            kind version (used by CI and checked by `make cluster-up`)
```

Conversion logic lives in `converters/` as plain functions that take strings or dicts and return dicts. `main.py` only handles HTTP. This keeps the logic easy to test, and lets a future CLI or frontend reuse it.

## How it runs on Kubernetes

The manifests in `k8s/` are plain YAML, no Helm.

| Aspect | Setting |
|--------|---------|
| Replicas | 2, rolling updates with zero unavailable pods |
| Probes | liveness on `/health`, readiness on `/ready` |
| Resources | requests 100m CPU / 64Mi, limits 250m CPU / 128Mi |
| User | UID/GID 10001, `runAsNonRoot` |
| Filesystem | read-only root filesystem |
| Privileges | all Linux capabilities dropped, no privilege escalation, `RuntimeDefault` seccomp, no service account token mounted |
| Config | `LOG_LEVEL` from the `converter-api-config` ConfigMap |
| Network | ClusterIP Service on port 8000, not reachable from the internet |

The `devtoolkit` namespace enforces the `restricted` [Pod Security Standard](https://kubernetes.io/docs/concepts/security/pod-security-standards/), so the cluster rejects any pod that tries to run as root.

## Image

`services/converter-api/Dockerfile` has two stages:

1. **builder** installs dependencies into a virtualenv.
2. **runtime** copies only the virtualenv and the app code onto the official slim Python image (the exact version is set in the Dockerfile and kept current by Dependabot) and runs as UID 10001. It has a Docker `HEALTHCHECK` for plain `docker run`; Kubernetes uses its own probes instead.

Images are published to GitHub Container Registry, tagged with the commit SHA and `latest`. The image name is set once, in `k8s/converter-api/deployment.yaml`; scripts and CI read it from there. See [CI/CD](operations/ci-cd.md).
