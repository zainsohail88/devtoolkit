# Roadmap

## Phase 1: converter and platform (done)

- [x] `converter-api` (FastAPI): docker-compose to Kubernetes, YAML validation
- [x] Raw Kubernetes manifests: probes, resource limits, non-root, restricted Pod Security
- [x] CI: lint, tests with a coverage gate, Docker build, manifest validation
- [x] Image publishing to GitHub Container Registry
- [x] Manual, keyless production deploy workflow (planned for Phase 2, done early)
- [x] Dependabot, CodeQL, SHA-pinned actions

## Phase 2: more tools and a web frontend

- [ ] `utility-api` service: `/base64/encode`, `/base64/decode`, `/jwt/decode`, `/cron/explain`, `/hash`, following the same Kubernetes manifest pattern as `converter-api`
- [ ] Web frontend (Flask + Jinja2, Tailwind CSS) for people who don't use the CLI. It calls the APIs server-side, and is server-rendered so search engines can index it.
- [ ] Production Kubernetes config: Ingress and TLS, so the service is reachable from the internet

## Later

- [ ] More converters, starting with CloudFormation to Terraform
- [ ] Public hosted version, ad-supported

## Out of scope

These are deliberately not planned:

- User accounts and authentication
- Database or persistent storage
- Rate limiting
- Paid features
- Mobile app
