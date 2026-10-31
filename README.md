# DevToolkit

Practical DevOps conversion tools, built as microservices on Kubernetes.

DevToolkit has two jobs:

1. **Be useful.** Tools DevOps engineers reach for every day, starting with a Docker Compose to Kubernetes converter.
2. **Be a clean example.** Raw Kubernetes manifests (no Helm yet), health probes, resource limits, non-root containers and a full CI/CD pipeline, all small enough to read in one sitting.

## Quick start

#### Requirements

| Tool | Minimum version | Needed for |
|------|-----------------|------------|
| [Python](https://www.python.org/) | 3.13 | Dev Mode |
| [GNU Make](https://www.gnu.org/software/make/) | 3.81 | Dev Mode, Kubernetes Mode |
| [Docker](https://docs.docker.com/get-docker/) | Any current release | Kubernetes Mode (builds the image; kind runs on it) |
| [kind](https://kind.sigs.k8s.io/) | v0.33.0 (pinned in [`.tool-versions`](.tool-versions)) | Kubernetes Mode |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | 1.36 (cluster runs Kubernetes 1.37) | Kubernetes Mode |
| [jq](https://jqlang.org/) | 1.5 | Only the [example](#example) below |

#### Dev Mode

Runs the API directly with Python and reloads on every code change.

```bash
make install                 # create .venv and install dependencies
make test                    # run the tests with coverage
make run                     # start the API on http://localhost:8000 (docs at /docs)
```

#### Kubernetes Mode

Builds the image and runs it on a local kind cluster, using the same manifests as production.

```bash
make deploy-local            # create the kind cluster if needed, build the image, deploy k8s/
make port-forward            # forward localhost:8000 to the service (keep it running)
curl localhost:8000/health   # in a second terminal: check the API responds
make cluster-down            # delete the cluster when you're done
```

#### All Commands

```bash
make help                    # list every make target with a short description
```

## Example

Send a Docker Compose file and get back Kubernetes Deployments and Services, plus notes on anything that needs a human decision. With the API running in either mode:

```bash
curl -s localhost:8000/convert/compose-to-k8s/yaml \
  -H 'Content-Type: application/json' \
  -d '{"compose_yaml": "services:\n  web:\n    image: nginx\n    ports:\n      - \"8080:80\""}' \
  | jq -r .yaml
```

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
...
---
apiVersion: v1
kind: Service
...
  ports:
  - name: tcp-8080
    port: 8080
    targetPort: 80
    protocol: TCP
```

## Documentation

| Topic | |
|-------|-|
| [API reference](docs/reference/api.md) | Endpoints, requests, responses, errors, input limits |
| [Conversion rules](docs/reference/conversion-rules.md) | How each compose field maps to Kubernetes |
| [Architecture](docs/architecture.md) | Project layout, how it runs on Kubernetes |
| [CI/CD](docs/operations/ci-cd.md) | Workflows, image publishing, security scanning |
| [Deploying to your cluster](docs/operations/deploy.md) | Run it on any Kubernetes cluster |
| [Configuration](docs/configuration.md) | Environment variables |
| [Roadmap](docs/roadmap.md) | Planned features in priority order, timeline, out of scope |
