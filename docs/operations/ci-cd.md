# CI/CD

All automation runs in GitHub Actions, from `.github/workflows/`.

## Workflows

| Workflow | Runs when | What it does |
|----------|-----------|--------------|
| `ci.yaml` | PR to `main`, push to `main` | Lint, tests, Docker builds, manifest validation, deploy to a real cluster (jobs below) |
| `build-push.yaml` | Called by CI on `main` after every check passes. Can also be run by hand. | Builds and pushes the image of each changed service to GitHub Container Registry |
| `codeql.yaml` | PR to `main`, push to `main`, weekly | Security scan of the Python code and the workflow files |

### CI jobs

Every job covers both services. The list of services is one line at the top of `ci.yaml`.

| Job | Checks |
|-----|--------|
| `lint` | `ruff check` and `ruff format --check` on each service, then `scripts/check_code_rules.py` on the whole repo: the code rules and writing rules in [CONTRIBUTING](../../CONTRIBUTING.md) |
| `test` | pytest on each service; fails when either is under **95%** coverage |
| `build` | Builds both Docker images, starts them on one Docker network and runs a conversion through the web UI, so `web` → `converter-api` is proven to work |
| `validate-k8s` | [kubeconform](https://github.com/yannh/kubeconform) `-strict` on `k8s/` (the maintained successor to kubeval) |
| `validate-terraform` | Converts each CloudFormation, SAM and CDK test template in `services/converter-api/tests/fixtures/cfn/` to Terraform, once with the `aws` provider and once with `awscc`, then runs `terraform fmt -check` and `terraform validate` on every result. Warnings fail it too. The Terraform version and its SHA-256 are pinned in `ci.yaml` |
| `e2e` | Creates a [kind](https://kind.sigs.k8s.io/) cluster from `scripts/kind-cluster.yaml` (the same one as `make cluster-up`), deploys `k8s/`, waits for both rollouts, then uses the UI through the `web` Service, including a conversion. Last it checks the NetworkPolicy: a `web` pod reaches `converter-api` and a pod without the `web` label does not |

A newer push to a PR cancels the runs of the older one. Runs on `main` always finish, so every passing commit gets its images.

## Image publishing

- **One image per service:** `ghcr.io/zainsohail88/devtoolkit/converter-api` and `.../web`.
- **Tags:** each image is tagged with its full commit SHA. `:latest` moves only when that commit is still the newest on `main`, so a slow CI run for an older commit leaves a newer image in place.
- **Only passing commits:** a commit that fails any CI job gets no image.
- **Only what changed:** the image of a service is built only when `services/<name>/` changed in that push. Commits that touch only docs or `k8s/` build nothing.

## Supply-chain protection

| What | How |
|------|-----|
| Pinned actions | Every third-party action is pinned to a full commit SHA (with the version in a comment), so a moved or hijacked tag has no effect on what runs |
| Pinned Tailwind CLI | The `web` Dockerfile downloads a fixed version and checks its SHA-256 before using it |
| Pinned Terraform | CI downloads a fixed Terraform release and checks its SHA-256 before using it |
| Locked editor bundle | The npm dependencies of the code editor are pinned exactly in `services/web/editor/package-lock.json` and installed with `npm ci`; the image builds the bundle |
| Dependabot | Weekly update PRs for the Python packages and Docker base images of both services, the npm packages of the editor and GitHub Actions. Minor and patch bumps are grouped; each major bump gets its own PR. |
| CodeQL | `security-extended` queries on Python and on the workflows, for example to catch script injection |
| CODEOWNERS | Changes to `.github/` need a review from the repo owner |
| Least privilege | Workflows get read-only tokens by default and request `packages: write` or `id-token: write` only in the job that needs it |
