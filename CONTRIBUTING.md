# Contributing to DevToolkit

Thanks for helping. This guide covers local setup, the code rules, how to add a converter or a service and the pull request process.

## Local setup

```bash
make install   # .venv with app + dev dependencies for every service
make lint      # ruff check + format check on every service, then the house code rules
make test      # pytest with coverage, every service
make run       # live-reloading API on http://localhost:8000
make run-web   # live-reloading web UI on http://localhost:8080 (needs `make run` in another terminal)
```

Install [kubeconform](https://github.com/yannh/kubeconform) if you change anything in `k8s/`, then run `make validate-k8s`. Install [Terraform](https://developer.hashicorp.com/terraform/install) if you change the CloudFormation converter, then run `make validate-terraform`.

## Code rules

`make lint` and the `lint` CI job enforce these with ruff and `scripts/check_code_rules.py`.

- **Define functions at module or class level.** A function or method never defines another one. Write a named function where you would use a lambda. To pass a callback, pass a bound method.
- **Write arguments out in full.** The checker rejects star imports, `*args`, `**kwargs`, keyword-only markers and unpacking. Write `a + [b]`, `dict(a, key=value)` and explicit arguments instead.
- **Put imports at the top of the module**, before any other statement. Keep them out of functions, classes, `if` blocks and tests.
- **Fix problems at the cause.** The checker rejects comments that switch a checker off, such as `noqa`, `type: ignore`, `pragma: no cover` and `nosec`.
- **Give each class one job.** Put dataclasses in a `models.py` and constants in a `constants.py`. An `__init__.py` holds only a docstring or plain imports.
- **Add docstrings and type hints** to every module, class, function and method outside `tests/`. The docstring says what the code does and gives an example when the input format needs one.
- **Use comments to explain why**, in plain words.
- **Let ruff format the code.** `make format` fixes most formatting problems.

## Writing rules

These rules apply to the docs, the page copy and every message a user reads, such as errors and conversion notes:

- Lead with the result, skip filler and prefer the smaller true claim.
- Say what something does or is. Leave out what goes without saying.
- Write without apostrophes: "does not" and "the stack name".
- Put no comma before "and" or "or".
- Use a full stop or a colon where you would use an em dash.

`scripts/check_code_rules.py` checks the last three rules in `README.md`, `CONTRIBUTING.md`, `SECURITY.md` and `docs/`.

## Adding a new converter

Example: a Kubernetes to Docker Compose converter.

1. **Logic.** Create a package `services/converter-api/converters/k8s_to_compose/`. Its classes and functions take strings or dicts and return dicts. Keep FastAPI imports out of it, so the logic stays easy to test. Give it a `models.py`, a `constants.py` and an `errors.py` with an error class whose message is safe to show the user.
2. **Endpoint.** Add a route in `services/converter-api/main.py` with a Pydantic request model. Set a `max_length` on its text fields. Return `{"success": true, ...}` on success and HTTP 400 with `{"success": false, "error": "..."}` for bad input.
3. **Tests.** Cover the happy path, each supported input format and every error branch. CI fails below 95% coverage. The current code is at 100%, so please keep it there.
4. **Page.** Add a template in `services/web/templates/` and a router in `services/web/routes/`, following `routes/compose.py`. Put the form dataclass in `models.py`, its checks in `inputs.py`, the converter-api call in `conversions.py` and every text the user reads in `messages.py`. Include the router in `main.py` and link the page from `index.html` and `base.html`. Test it in `services/web/tests/` with the fake API from `conftest.py`.
5. **Docs.** Add the endpoint to `docs/reference/api.md` and its rules to a page in `docs/reference/`. Keep `README.md` to the overview and quick start.

### CloudFormation to Terraform mappings

Each CloudFormation type with a hand-written `aws` provider mapping is a subclass of `ResourceConverter` (`converters/cfn/resources/base.py`). The subclass lives in one of the modules in `converters/cfn/resources/` and is listed in the `CONVERTERS` tuple of that module.

- The class names the CloudFormation type, the Terraform type and the attributes that `Ref` and `Fn::GetAtt` read.
- Its `convert(b)` method takes properties one at a time from `b.props` (`attr`, `number`, `json`, `tags`, `block`...). Any property it leaves in `b.props` is kept as a comment and reported in the notes.
- Check attribute names against the provider schema (`terraform providers schema -json`). Then add the type to a fixture in `tests/fixtures/cfn/` and run `make validate-terraform`.

Types without a mapping use the `awscc` provider through `converters/cfn/awscc_index.json`. When you bump the awscc version, rebuild that file with `python -m tools.build_awscc_index` from `services/converter-api`.

## Adding a new service

1. Create `services/<name>/` with a `Dockerfile` that follows the existing ones: multi-stage, non-root UID 10001, a `HEALTHCHECK` and `/health` and `/ready` endpoints.
2. Add manifests in `k8s/<name>/` that follow the `web` pattern: probes, resource limits, a non-root security context and a ConfigMap for its settings. Put the image name in `deployment.yaml`. The scripts read it from there.
3. Add the name to the `SERVICES` list in `Makefile`, `.github/workflows/ci.yaml` and `scripts/deploy-local.sh`, add a job for it in `.github/workflows/build-push.yaml` and add its directory to `.github/dependabot.yml`.

## Web UI rules

The design system is one file: `services/web/styles/input.css`. Every colour, size, radius and font is a token there. Each token is named for its job (`surface`, `ink`, `accent`) and has its light and dark values side by side.

- **Use token classes in templates.** Use the token classes (`bg-surface`, `text-ink-muted`, `border-hairline`, `rounded-sm`, `text-step-1`) and the component classes (`btn`, `card`, `field`, `alert`, `code`, `nav-link`). `input.css` removes the Tailwind palette, radii and text sizes, so classes such as `bg-slate-50` or `rounded-lg` produce no CSS. When you need a value the tokens lack, add a token.
- **Build one component per concept.** A variant is a modifier class (`btn-primary`). A part that two pages share, such as the file list with tabs, is one template that both include (`_files.html`).
- **Support both themes.** Give each new colour a light and a dark value and check the contrast: 4.5:1 for text and 3:1 for interface borders. Keep the two dark blocks in `input.css` identical.
- **Respect keyboard and motion settings.** Anything clickable is at least 44px tall (`min-h-11`) and focus stays visible. Animation follows `prefers-reduced-motion`, which `input.css` handles for every page.
- **Keep scripts to two files.** `app.js` and the editor bundle are the only JavaScript. Every page works without them: forms post and the server renders the result. The one inline script sets the theme before first paint. The Content-Security-Policy allows it by hash, so keep it the only inline script.
- **Inline icons.** Icons are inline SVGs from `templates/icons/` (`{% include "icons/copy.svg" %}`), sized by the `.icon` class. Take new icons from the pinned Lucide release named in the docs, strip `width`/`height` and add `aria-hidden="true"`.
- **Use the shared editor.** YAML and Terraform go through the CodeMirror editor in `services/web/editor/`, which reads its colours from the tokens. A new tool page needs a `[data-editor]` container around its textarea and a result fragment. `app.js` does the rest.
- **Follow the writing rules.** See the writing rules above. The product is called DevToolkit and each page names what its tool does.

`make css` rebuilds `static/app.css` and `make editor` rebuilds `static/vendor/editor.js` (Node.js 22). The Dockerfile does both during the image build.

## Kubernetes manifest rules

Every container must have:

- `resources.requests` **and** `resources.limits`
- `livenessProbe` and `readinessProbe`
- `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`, `readOnlyRootFilesystem: true`

The `devtoolkit` namespace enforces the `restricted` Pod Security Standard, so the cluster rejects pods that break these rules.

**Keep secrets out of code and manifests.** Use GitHub repo variables, Workload Identity or Kubernetes Secrets that you create outside the repo.

## Pull request process

1. Branch from `main`.
2. Keep each PR to one change, such as one converter or one fix.
3. Before you push, run `make lint test`. If you changed `k8s/`, run `make validate-k8s` too.
4. Open a PR into `main`. CI must pass: lint, tests, Docker builds, manifest validation, Terraform validation and the end-to-end test on kind.
5. A maintainer reviews and merges it. A merge to `main` builds and pushes the images of the services that changed.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
