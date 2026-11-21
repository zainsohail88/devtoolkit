# DevToolkit documentation

The [main README](../README.md) has the quick start. The pages below cover the rest.

## Using it

- [API reference](reference/api.md): endpoints, request and response formats, errors and input limits
- [Conversion rules](reference/conversion-rules.md): how each Docker Compose field maps to Kubernetes
- [CloudFormation to Terraform](reference/cloudformation-to-terraform.md): how CloudFormation, AWS SAM and AWS CDK templates convert to Terraform

## How it is built

- [Architecture](architecture.md): the two services, the project layout and how they run on Kubernetes

## Running it

- [CI/CD](operations/ci-cd.md): what runs on each PR and merge, image publishing and dependency and security scanning
- [Deploying to your cluster](operations/deploy.md): how to run DevToolkit on your own Kubernetes cluster
- [Configuration](configuration.md): environment variables

## Where it is going

- [Roadmap](roadmap.md): planned features in priority order, the timeline and what is out of scope

## Contributing

- [CONTRIBUTING.md](../CONTRIBUTING.md): local setup, how to add a converter or a service, code style and the PR process
- [SECURITY.md](../SECURITY.md): how to report a vulnerability
