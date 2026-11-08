# Architecture

## Two services

| Service | What it does | Port | Reachable from |
|---------|--------------|------|----------------|
| `web` | Serves the pages (Docker Compose → Kubernetes, YAML validator). Calls `converter-api` from the server side. | 8080 | Users |
| `converter-api` | The conversions, as a JSON API | 8000 | `web` only |

Browsers never talk to `converter-api`. In the cluster a [NetworkPolicy](../k8s/converter-api/networkpolicy.yaml) allows traffic to it only from `web` pods. This keeps one place (`web`) responsible for input handling and output pages, and lets the API change without breaking anyone who might have scripted against it.

## Project layout

```
services/
  converter-api/          FastAPI service: the conversions
    main.py               HTTP endpoints
    converters/           Conversion logic (pure functions, no FastAPI)
    tests/                pytest tests
    Dockerfile            Multi-stage build, non-root user
  web/                    FastAPI + Jinja2 service: the pages
    main.py               Routes, form handling, security headers
    api_client.py         Calls converter-api (with a Google identity token on Cloud Run)
    templates/            Jinja2 templates: index, the two tools, error page
    static/               app.js (theme toggle, copy/download); app.css is built from styles/
    styles/input.css      The design system: every colour, size and radius as a token, light
                          and dark values, and the component classes. Compiled by Tailwind.
    tests/                pytest tests; converter-api is replaced by a fake
    Dockerfile            Multi-stage build: Python deps, Tailwind CLI, runtime
k8s/
  namespace.yaml          devtoolkit namespace
  converter-api/          Deployment, Service, ConfigMap, NetworkPolicy
  web/                    Deployment, Service, ConfigMap
.github/
  workflows/              CI, image publishing, CodeQL
  CODEOWNERS, dependabot.yml, issue and PR templates
scripts/                  build.sh, deploy-local.sh, image-name.sh, kind-cluster.yaml
docs/                     This documentation
Makefile                  Common commands (`make help`)
.tool-versions            kind version (used by CI and checked by `make cluster-up`)
```

Conversion logic lives in `converter-api/converters/` as plain functions that take strings or dicts and return dicts. `converter-api/main.py` only handles HTTP, and `web` only handles pages. This keeps the logic easy to test, and lets a future CLI reuse it.

## How it runs on Kubernetes

The manifests in `k8s/` are plain YAML, no Helm. Both services follow the same pattern:

| Aspect | Setting |
|--------|---------|
| Replicas | 2, rolling updates with zero unavailable pods |
| Probes | liveness on `/health`, readiness on `/ready` |
| Resources | `converter-api`: requests 100m CPU / 64Mi, limits 250m CPU / 128Mi. `web`: requests 100m CPU / 96Mi, limits 250m CPU / 192Mi |
| User | UID/GID 10001, `runAsNonRoot` |
| Filesystem | read-only root filesystem |
| Privileges | all Linux capabilities dropped, no privilege escalation, `RuntimeDefault` seccomp, no service account token mounted |
| Config | a ConfigMap per service, see [Configuration](configuration.md) |
| Network | ClusterIP Services (`web` on 8080, `converter-api` on 8000), not reachable from the internet. `converter-api` accepts traffic from `web` pods only |

The `devtoolkit` namespace enforces the `restricted` [Pod Security Standard](https://kubernetes.io/docs/concepts/security/pod-security-standards/), so the cluster rejects any pod that tries to run as root.

`web`'s readiness probe does not check `converter-api`. If the API is down, the pages stay up and show an error instead, so one slow service can't take the other out of rotation.

## Images

Both Dockerfiles use the official slim Python image (the exact version is set in each Dockerfile and kept current by Dependabot) and run as UID 10001. Each has a Docker `HEALTHCHECK` for plain `docker run`; Kubernetes uses its own probes instead.

- `services/converter-api/Dockerfile`: **builder** installs dependencies into a virtualenv; **runtime** copies only the virtualenv and the code.
- `services/web/Dockerfile`: the same two stages, plus a **css** stage that downloads Tailwind's standalone CLI (version and SHA-256 pinned in the Dockerfile) and compiles `styles/input.css` into one small stylesheet. No Node.js anywhere.

Images are published to GitHub Container Registry, one per service, tagged with the commit SHA and `latest`. Each image name is set once, in `k8s/<service>/deployment.yaml`; scripts and CI read it from there. See [CI/CD](operations/ci-cd.md).
