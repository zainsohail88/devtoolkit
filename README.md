# DevToolkit

Practical DevOps conversion tools, built as microservices on Kubernetes.

**Try it:** [devtoolkit.cloud.run](https://devtoolkit.cloud.run)

DevToolkit has two jobs:

1. **Be useful.** Two converters, each with a web page and an API: Docker Compose to Kubernetes, CloudFormation (with AWS SAM and AWS CDK) to Terraform.
2. **Be a clean example.** Plain Kubernetes manifests, health probes, resource limits, non-root containers, a NetworkPolicy and a full CI/CD pipeline, all small enough to read in one sitting.

It is two services: **web** serves the pages you use in a browser and **converter-api** does the conversions. Only web can reach the API.

## Quick start

#### Requirements

| Tool | Minimum version | Needed for |
|------|-----------------|------------|
| [Python](https://www.python.org/) | 3.13 | Dev Mode |
| [GNU Make](https://www.gnu.org/software/make/) | 3.81 | Dev Mode, Kubernetes Mode |
| [curl](https://curl.se/) | Any current release | Dev Mode (downloads the Tailwind CLI once, for the stylesheet of the web UI) |
| [Node.js](https://nodejs.org/) | 22 | Dev Mode (bundles the code editor of the web UI once, `make editor`) |
| [Docker](https://docs.docker.com/get-docker/) | Any current release | Kubernetes Mode (builds the images; kind runs on it) |
| [kind](https://kind.sigs.k8s.io/) | v0.33.0 (pinned in [`.tool-versions`](.tool-versions)) | Kubernetes Mode |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | 1.36 (cluster runs Kubernetes 1.37) | Kubernetes Mode |
| [jq](https://jqlang.org/) | 1.5 | Only the [example](#example) below |
| [Terraform](https://developer.hashicorp.com/terraform/install) | 1.5 | Only `make validate-terraform`, which checks the output of the CloudFormation converter |

#### Dev Mode

Runs both services directly with Python and reloads on every code change.

```bash
make install                 # create .venv and install dependencies for every service
make test                    # run the tests of every service with coverage
make run                     # start the API on http://localhost:8000 (docs at /docs)
make run-web                 # in a second terminal: start the web UI on http://localhost:8080
```

#### Kubernetes Mode

Builds both images and runs them on a local kind cluster, using the same manifests as production.

```bash
make deploy-local            # create the kind cluster if needed, build the images, deploy k8s/
make port-forward            # forward localhost:8080 to the web UI (keep it running)
                             # then open http://localhost:8080 in your browser
make cluster-down            # delete the cluster when you are done
```

#### All Commands

```bash
make help                    # list every make target with a short description
```

## Example

Send a Docker Compose file and get back Kubernetes Deployments and Services, plus notes on anything that needs a human decision. The web UI does the same from a form. With the API running (`make run`):

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

A CloudFormation template works the same way, with `/convert/cfn-to-terraform`:

```bash
curl -s localhost:8000/convert/cfn-to-terraform \
  -H 'Content-Type: application/json' \
  -d '{"template": "Resources:\n  Logs:\n    Type: AWS::S3::Bucket\n    Properties:\n      VersioningConfiguration:\n        Status: Enabled"}' \
  | jq -r '.files[] | select(.path == "main.tf") | .content'
```

```hcl
resource "aws_s3_bucket" "logs" {}

resource "aws_s3_bucket_versioning" "logs_versioning" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration {
    status = "Enabled"
  }
}
```

## Documentation

| Topic | |
|-------|-|
| [API reference](docs/reference/api.md) | Endpoints, requests, responses, errors, input limits |
| [Conversion rules](docs/reference/conversion-rules.md) | How each compose field maps to Kubernetes |
| [CloudFormation to Terraform](docs/reference/cloudformation-to-terraform.md) | How CloudFormation, SAM and CDK templates become Terraform |
| [Architecture](docs/architecture.md) | Project layout, the two services, how they run on Kubernetes |
| [CI/CD](docs/operations/ci-cd.md) | Workflows, image publishing, security scanning |
| [Deploying to your cluster](docs/operations/deploy.md) | Run it on any Kubernetes cluster |
| [Configuration](docs/configuration.md) | Environment variables |
| [Roadmap](docs/roadmap.md) | Planned features in priority order, timeline, out of scope |
