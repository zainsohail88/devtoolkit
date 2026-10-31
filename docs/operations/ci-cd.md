# CI/CD

All automation runs in GitHub Actions, from `.github/workflows/`.

## Workflows

| Workflow | Runs when | What it does |
|----------|-----------|--------------|
| `ci.yaml` | PR to `main`, push to `main` | Lint, tests, Docker build, manifest validation, deploy to a real cluster (jobs below) |
| `build-push.yaml` | Called by CI on `main` after every check passes, if `services/**` changed. Can also be run by hand. | Builds and pushes the image to GitHub Container Registry |
| `deploy-gke.yaml` | Manual only, from `main` | The maintainers' production deploy. You don't need it to run DevToolkit: see [Deploying to your cluster](deploy.md). |
| `codeql.yaml` | PR to `main`, push to `main`, weekly | Security scan of the Python code and the workflow files |

### CI jobs

| Job | Checks |
|-----|--------|
| `lint` | `ruff check` and `ruff format --check` |
| `test` | pytest; fails if coverage is under **95%** |
| `build` | Builds the Docker image, starts it, and checks `/health` responds |
| `validate-k8s` | [kubeconform](https://github.com/yannh/kubeconform) `-strict` on `k8s/` (the maintained successor to kubeval) |
| `e2e` | Creates a [kind](https://kind.sigs.k8s.io/) cluster from `scripts/kind-cluster.yaml` (the same one as `make cluster-up`), deploys `k8s/`, waits for the rollout, then calls `/health`, `/ready` and a conversion through the Service |

PR runs that get superseded by a newer push are cancelled. Runs on `main` always finish, so every passing commit gets its image.

## Image publishing

- **Tags:** each image is tagged with its full commit SHA. `:latest` only moves if that commit is still the newest on `main`, so a slow CI run for an older commit can't overwrite a newer image.
- **Never broken:** a commit that fails any CI job never gets an image.
- **Skipped when nothing changed:** commits that don't touch `services/` (docs, `k8s/` only) don't build an image. The deploy workflow defaults to the newest commit that did.

## Supply-chain protection

| What | How |
|------|-----|
| Pinned actions | Every third-party action is pinned to a full commit SHA (with the version in a comment), so a moved or hijacked tag can't change what runs |
| Dependabot | Weekly update PRs for Python packages, GitHub Actions and the Docker base image. Minor and patch bumps are grouped; major bumps get their own PR. |
| CodeQL | `security-extended` queries on Python and on the workflows, e.g. to catch script injection |
| CODEOWNERS | `.github/` changes need the repo owner's review |
| Least privilege | Workflows get read-only tokens by default and request `packages: write` or `id-token: write` only in the job that needs it |
| No stored cloud keys | GKE deploys use Workload Identity Federation |
