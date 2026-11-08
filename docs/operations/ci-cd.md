# CI/CD

All automation runs in GitHub Actions, from `.github/workflows/`.

## Workflows

| Workflow | Runs when | What it does |
|----------|-----------|--------------|
| `ci.yaml` | PR to `main`, push to `main` | Lint, tests, Docker builds, manifest validation, deploy to a real cluster (jobs below) |
| `build-push.yaml` | Called by CI on `main` after every check passes. Can also be run by hand. | Builds and pushes each service's image to GitHub Container Registry, if that service changed |
| `codeql.yaml` | PR to `main`, push to `main`, weekly | Security scan of the Python code and the workflow files |

### CI jobs

Every job covers both services. The list of services is one line at the top of `ci.yaml`.

| Job | Checks |
|-----|--------|
| `lint` | `ruff check` and `ruff format --check` on each service |
| `test` | pytest on each service; fails if either is under **95%** coverage |
| `build` | Builds both Docker images, starts them on one Docker network, and runs a conversion through the web UI, so `web` → `converter-api` is proven to work |
| `validate-k8s` | [kubeconform](https://github.com/yannh/kubeconform) `-strict` on `k8s/` (the maintained successor to kubeval) |
| `e2e` | Creates a [kind](https://kind.sigs.k8s.io/) cluster from `scripts/kind-cluster.yaml` (the same one as `make cluster-up`), deploys `k8s/`, waits for both rollouts, then uses the UI through the `web` Service, including a conversion. Finally it checks the NetworkPolicy: a `web` pod can reach `converter-api`, and a pod without the `web` label cannot |

PR runs that get superseded by a newer push are cancelled. Runs on `main` always finish, so every passing commit gets its images.

## Image publishing

- **One image per service:** `ghcr.io/zainsohail88/devtoolkit/converter-api` and `.../web`.
- **Tags:** each image is tagged with its full commit SHA. `:latest` only moves if that commit is still the newest on `main`, so a slow CI run for an older commit can't overwrite a newer image.
- **Never broken:** a commit that fails any CI job never gets an image.
- **Only what changed:** a service's image is built only when its `services/<name>/` directory changed in that push. Commits that only touch docs or `k8s/` build nothing.

## Supply-chain protection

| What | How |
|------|-----|
| Pinned actions | Every third-party action is pinned to a full commit SHA (with the version in a comment), so a moved or hijacked tag can't change what runs |
| Pinned Tailwind CLI | The `web` Dockerfile downloads a fixed version and checks its SHA-256 before using it |
| Dependabot | Weekly update PRs for both services' Python packages and Docker base images, and for GitHub Actions. Minor and patch bumps are grouped; major bumps get their own PR. |
| CodeQL | `security-extended` queries on Python and on the workflows, e.g. to catch script injection |
| CODEOWNERS | `.github/` changes need the repo owner's review |
| Least privilege | Workflows get read-only tokens by default and request `packages: write` or `id-token: write` only in the job that needs it |
