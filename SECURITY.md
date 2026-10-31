# Security Policy

## Supported versions

Only the latest code on `main` (and the image tagged `latest`) gets security fixes.

## Reporting a vulnerability

**Please don't open a public issue for security problems.**

Report it privately through GitHub instead: go to the **Security** tab of this repository and click **Report a vulnerability**. Only the maintainers can see the report.

Please include:

- what the problem is and where it is (file, endpoint or workflow)
- steps or an input that reproduces it
- what an attacker could do with it

You'll get an acknowledgement within 7 days. Once a fix is released we'll credit you in the advisory, unless you'd rather stay anonymous.

## Scope

In scope:

- the `converter-api` service, e.g. input that crashes it, hangs it or makes it use excessive memory
- the Dockerfile and Kubernetes manifests in `k8s/`
- the GitHub Actions workflows in `.github/workflows/`

Out of scope:

- deployments run by other people with their own configuration
- denial of service by sending a very large number of requests (rate limiting is not part of this project)
