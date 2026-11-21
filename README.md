# DevToolkit

DevToolkit converts infrastructure code from one tool to another. It runs as two small services on Kubernetes.

**Hosted version:** [devtoolkit.cloud.run](https://devtoolkit.cloud.run)

DevToolkit has two converters. Each one has a web page and an API endpoint:

- Docker Compose to Kubernetes
- CloudFormation, AWS SAM and AWS CDK to Terraform

The project also shows how to run a small service on Kubernetes. It uses plain manifests, health probes, resource limits, non-root containers, a NetworkPolicy and a full CI/CD pipeline. All of it is small enough to read in one sitting.

The **web** service serves the pages and **converter-api** does the conversions. Only web can call the API.

## Quick start

#### Requirements

| Tool | Minimum version | Needed for |
|------|-----------------|------------|
| [Python](https://www.python.org/) | 3.13 | Dev Mode |
| [GNU Make](https://www.gnu.org/software/make/) | 3.81 | Dev Mode, Kubernetes Mode |
| [curl](https://curl.se/) | Any current release | Dev Mode: downloads the Tailwind CLI once to build the web UI stylesheet |
| [Node.js](https://nodejs.org/) | 22 | Dev Mode: `make editor` bundles the code editor of the web UI once |
| [Docker](https://docs.docker.com/get-docker/) | Any current release | Kubernetes Mode: builds the images and runs kind |
| [kind](https://kind.sigs.k8s.io/) | v0.33.0 (pinned in [`.tool-versions`](.tool-versions)) | Kubernetes Mode |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | 1.36 (the cluster runs Kubernetes 1.37) | Kubernetes Mode |
| [jq](https://jqlang.org/) | 1.5 | The [example](#example) below |
| [Terraform](https://developer.hashicorp.com/terraform/install) | 1.5 | `make validate-terraform`, which checks the Terraform that the CloudFormation converter writes |

#### Dev Mode

Dev Mode runs both services with Python and reloads them when you change the code.

```bash
make install                 # create .venv and install dependencies for every service
make test                    # run the tests of every service with coverage
make run                     # start the API on http://localhost:8000 (docs at /docs)
make run-web                 # in a second terminal: start the web UI on http://localhost:8080
```

#### Kubernetes Mode

Kubernetes Mode builds both images and runs them on a local kind cluster with the same manifests as production.

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

Post a Docker Compose file to the API and you get back Kubernetes Deployments and Services. The response also lists notes on anything you need to decide yourself. The web UI does the same through a form. Start the API with `make run`, then run:

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

To convert a CloudFormation template, post it to `/convert/cfn-to-terraform`:

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
| [API reference](docs/reference/api.md) | Endpoints, requests, responses, errors and input limits |
| [Conversion rules](docs/reference/conversion-rules.md) | How each Compose field maps to Kubernetes |
| [CloudFormation to Terraform](docs/reference/cloudformation-to-terraform.md) | How CloudFormation, SAM and CDK templates convert to Terraform |
| [Architecture](docs/architecture.md) | The project layout, the two services and how they run on Kubernetes |
| [CI/CD](docs/operations/ci-cd.md) | Workflows, image publishing and security scanning |
| [Deploying to your cluster](docs/operations/deploy.md) | How to run DevToolkit on your own Kubernetes cluster |
| [Configuration](docs/configuration.md) | Environment variables |
| [Roadmap](docs/roadmap.md) | Planned features in priority order, the timeline and what is out of scope |
