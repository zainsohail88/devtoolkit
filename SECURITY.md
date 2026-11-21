# Security Policy

## Supported versions

Security fixes go to the latest code on `main` and to the image tagged `latest`.

## Reporting a vulnerability

**Please report security problems privately.**

Go to the **Security** tab of this repository on GitHub and click **Report a vulnerability**. Only the maintainers can see the report.

Please include:

- what the problem is and where it is (file, endpoint or workflow)
- steps or an input that reproduces it
- what an attacker could do with it

We acknowledge each report within 7 days. Once a fix is released, we credit you in the advisory. Tell us if you would rather stay anonymous.

## Scope

In scope:

- the `converter-api` service, for example input that crashes it, hangs it or makes it use too much memory
- the Dockerfiles and the Kubernetes manifests in `k8s/`
- the GitHub Actions workflows in `.github/workflows/`

Out of scope:

- deployments that other people run with their own configuration
- denial of service through a very large number of requests: rate limiting is the job of the platform that runs the service
