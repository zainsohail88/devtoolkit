# DevToolkit

Practical DevOps conversion tools, built as microservices on Kubernetes.

DevToolkit has two jobs:

1. **Be useful.** Tools DevOps engineers reach for every day, starting with a docker-compose to Kubernetes converter.
2. **Be a clean example.** Raw Kubernetes manifests (no Helm yet), health probes, resource limits, non-root containers and a full CI/CD pipeline, all small enough to read in one sitting.

> **Status:** Phase 1 is done: `converter-api` is built, tested and ready to deploy. Next up are `utility-api` and a web frontend. See the [roadmap](docs/roadmap.md).

## What it does

Send a `docker-compose.yaml` and get back Kubernetes Deployments and Services, plus notes on anything that needs a human decision.

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
```

## Quick start

**Requirements:** Python 3.13 and Docker. For Kubernetes, also [kind](https://kind.sigs.k8s.io/) (version in [`.tool-versions`](.tool-versions)) and `kubectl`.

### Develop: run the app directly (fastest)

```bash
make install   # creates .venv and installs dependencies
make test      # runs tests with coverage
make run       # http://localhost:8000/docs for interactive API docs
```

### Run it on Kubernetes (kind)

```bash
make deploy-local   # creates the kind cluster if needed, builds the image, deploys k8s/
make port-forward   # then in another terminal:
curl localhost:8000/health
make cluster-down   # delete the cluster when you're done
```

`make help` lists every command.

## Documentation

| Topic | |
|-------|-|
| [API reference](docs/reference/api.md) | Endpoints, requests, responses, errors, input limits |
| [Conversion rules](docs/reference/conversion-rules.md) | How each compose field maps to Kubernetes |
| [Architecture](docs/architecture.md) | Project layout, how it runs on Kubernetes |
| [CI/CD](docs/operations/ci-cd.md) | Workflows, image publishing, security scanning |
| [Deploying to your cluster](docs/operations/deploy.md) | Run it on any Kubernetes cluster |
| [Configuration](docs/configuration.md) | Environment variables |
| [Roadmap](docs/roadmap.md) | Phases, future plans, out of scope |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Found a security problem? See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
