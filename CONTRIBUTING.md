# Contributing to DevToolkit

Thanks for helping. This guide covers local setup, adding a converter or tool, and the PR process.

## Local setup

```bash
make install   # .venv with app + dev dependencies
make lint      # ruff check + format check
make test      # pytest with coverage
make run       # live-reloading server on http://localhost:8000
```

Install [kubeconform](https://github.com/yannh/kubeconform) if you change anything in `k8s/`, then run `make validate-k8s`.

## Adding a new converter

Example: adding a CloudFormation to Terraform converter.

1. **Logic.** Create `services/converter-api/converters/cfn_to_terraform.py`. Keep it pure: functions take strings or dicts and return dicts. No FastAPI imports in here, which keeps the logic easy to test.
   - A `parse_...()` function that validates input and raises a clear error.
   - One or more `convert_...()` functions.
2. **Endpoint.** Add a route in `services/converter-api/main.py`. Use a Pydantic request model with a `max_length` on text fields. Return `{"success": true, ...}` on success and HTTP 400 with `{"success": false, "error": "..."}` for bad input.
3. **Tests.** Add `tests/test_cfn_to_terraform.py`, covering the happy path, each supported input format, and every error branch. CI fails under 95% coverage, and the current code is at 100%. Please keep it there.
4. **Docs.** Add the endpoint to `docs/reference/api.md` and its rules to `docs/reference/conversion-rules.md`. Keep `README.md` to the overview and quick start.

## Adding a new service

A new service (for example the planned web frontend) needs its own `k8s/<service>/` manifests that follow the `converter-api` pattern: probes, resource limits, non-root security context.

## Code style

- **ruff** for linting and formatting (`make format` fixes most things).
- **Type hints** on all functions.
- **Docstrings** on public functions. Say what it does and give an example if the input format is non-obvious.
- **Comments explain why**, not what. Plain words, no buzzwords.

## Kubernetes manifest rules

Every container must have:

- `resources.requests` **and** `resources.limits`
- `livenessProbe` and `readinessProbe`
- `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`, `readOnlyRootFilesystem: true`

The `devtoolkit` namespace enforces the `restricted` Pod Security Standard, so pods that break these rules are rejected by the cluster.

**No secrets in code or manifests.** Use GitHub repo variables, Workload Identity, or Kubernetes Secrets created outside the repo.

## Pull request process

1. Branch from `main`.
2. Keep each PR to one change: one converter, one fix, and so on.
3. Run `make lint test` (and `make validate-k8s` if you touched `k8s/`) before pushing.
4. Open a PR into `main`. CI must pass: lint, tests, Docker build, manifest validation.
5. A maintainer reviews and merges. Merging to `main` builds and pushes the image. Production deploys are a separate manual step.

## License

By contributing, you agree your contributions are licensed under the [MIT License](LICENSE).
