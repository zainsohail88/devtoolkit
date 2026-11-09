# Contributing to DevToolkit

Thanks for helping. This guide covers local setup, adding a converter or service, and the PR process.

## Local setup

```bash
make install   # .venv with app + dev dependencies for every service
make lint      # ruff check + format check, every service
make test      # pytest with coverage, every service
make run       # live-reloading API on http://localhost:8000
make run-web   # live-reloading web UI on http://localhost:8080 (needs `make run` in another terminal)
```

Install [kubeconform](https://github.com/yannh/kubeconform) if you change anything in `k8s/`, then run `make validate-k8s`.

## Adding a new converter

Example: adding a CloudFormation to Terraform converter.

1. **Logic.** Create `services/converter-api/converters/cfn_to_terraform.py`. Keep it pure: functions take strings or dicts and return dicts. No FastAPI imports in here, which keeps the logic easy to test.
   - A `parse_...()` function that validates input and raises a clear error.
   - One or more `convert_...()` functions.
2. **Endpoint.** Add a route in `services/converter-api/main.py`. Use a Pydantic request model with a `max_length` on text fields. Return `{"success": true, ...}` on success and HTTP 400 with `{"success": false, "error": "..."}` for bad input.
3. **Tests.** Add `tests/test_cfn_to_terraform.py`, covering the happy path, each supported input format, and every error branch. CI fails under 95% coverage, and the current code is at 100%. Please keep it there.
4. **Page.** Add a template in `services/web/templates/` and a GET/POST route pair in `services/web/main.py`, following `compose_to_k8s.html`. Link it from `index.html` and `base.html`. Test it in `services/web/tests/` with the fake API from `conftest.py`.
5. **Docs.** Add the endpoint to `docs/reference/api.md` and its rules to `docs/reference/conversion-rules.md`. Keep `README.md` to the overview and quick start.

## Adding a new service

1. `services/<name>/` with a `Dockerfile` that follows the existing ones: multi-stage, non-root UID 10001, `HEALTHCHECK`, `/health` and `/ready` endpoints.
2. `k8s/<name>/` manifests following the `web` pattern: probes, resource limits, non-root security context, a ConfigMap for its settings. The image name goes in `deployment.yaml`; scripts read it from there.
3. Add the name to the `SERVICES` list in `Makefile`, `.github/workflows/ci.yaml` and `scripts/deploy-local.sh`, add a job for it in `.github/workflows/build-push.yaml`, and its directory to `.github/dependabot.yml`.

## Code style

- **ruff** for linting and formatting (`make format` fixes most things).
- **Type hints** on all functions.
- **Docstrings** on public functions. Say what it does and give an example if the input format is non-obvious.
- **Comments explain why**, not what. Plain words, no buzzwords.

## Web UI rules

The design system is one file: `services/web/styles/input.css`. Every colour, size, radius and font is a token there, named for its job (`surface`, `ink`, `accent`), with light and dark values side by side.

- **No raw values in templates.** Use token classes (`bg-surface`, `text-ink-muted`, `border-hairline`, `rounded-sm`, `text-step-1`) and the component classes (`btn`, `card`, `field`, `alert`, `code`, `nav-link`). Tailwind's own palette, radii and text sizes are removed, so `bg-slate-50` or `rounded-lg` produce no CSS at all. If you need a value the tokens lack, add a token.
- **One component per concept.** A variant is a modifier class (`btn-primary`), not a new component.
- **Both themes.** New colours get a light and a dark value, checked for contrast: 4.5:1 for text, 3:1 for interface borders. The two dark blocks in `input.css` must stay identical.
- **Keyboard and motion.** Anything clickable is at least 44px tall (`min-h-11`), focus stays visible, and animation respects `prefers-reduced-motion` (already handled globally).
- **Scripts.** `app.js` and the editor bundle are the only JavaScript, and every page must work without them: forms post and the server renders the result. The one inline script (theme before first paint) is allowed by hash in the Content-Security-Policy; don't add others.
- **Icons.** Inline SVGs from `templates/icons/` (`{% include "icons/copy.svg" %}`), sized by the `.icon` class. Add icons from the pinned Lucide release named in the docs, strip `width`/`height`, and add `aria-hidden="true"`.
- **Editors.** YAML goes through the CodeMirror editor (`services/web/editor/`), which reads its colours from the tokens. A new tool page needs only a `[data-editor]` container around its textarea and a result fragment; `app.js` does the rest.
- **Copy.** Lead with the result, no filler, no em dashes, and prefer the smaller true claim. The product is called DevToolkit; pages name what the tool does, not what to paste.

`make css` rebuilds `static/app.css` and `make editor` rebuilds `static/vendor/editor.js` (Node.js 22); the Dockerfile does both during the image build.

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
4. Open a PR into `main`. CI must pass: lint, tests, Docker builds, manifest validation, end-to-end on kind.
5. A maintainer reviews and merges. Merging to `main` builds and pushes the images of the services that changed.

## License

By contributing, you agree your contributions are licensed under the [MIT License](LICENSE).
