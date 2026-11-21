# Contributing to DevToolkit

Thanks for helping. This guide covers local setup, the code rules, adding a converter or service and the PR process.

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

- **Functions live at module or class level.** A function or method never defines another one. Lambdas are replaced by named functions; pass a bound method as a callback.
- **No `*` or `**` syntax.** No star imports, `*args`, `**kwargs`, keyword-only markers or unpacking. Write `a + [b]`, `dict(a, key=value)` and explicit arguments.
- **Imports at the top of the module**, before any other statement. Never inside a function, a class, an `if` block or a test.
- **Problems get fixed at the cause.** Comments that switch a checker off (`noqa`, `type: ignore`, `pragma: no cover`, `nosec`) are not accepted.
- **Classes with one job each.** Dataclasses go in a `models.py` and constants in a `constants.py`. An `__init__.py` holds only a docstring or plain imports.
- **Docstrings and type hints** on every module, class, function and method outside `tests/`: what it does, plus an example when the input format needs one.
- **Comments explain why**, in plain words.
- **ruff** formats the code (`make format` fixes most things).

## Writing rules

For the docs, page copy and every message a user reads (errors, conversion notes):

- Lead with the result, skip filler and prefer the smaller true claim.
- Say what something does or is.
- No apostrophes: write "does not" and "the stack name".
- No comma before "and" or "or".
- No em dashes.

The Markdown checks run with the code rules.

## Adding a new converter

Example: a Kubernetes to Docker Compose converter.

1. **Logic.** Create a package `services/converter-api/converters/k8s_to_compose/`. Keep it pure: classes and functions take strings or dicts and return dicts, with no FastAPI imports, which keeps the logic easy to test. Give it a `models.py`, a `constants.py` and an `errors.py` with an error class that carries a message safe to show the user.
2. **Endpoint.** Add a route in `services/converter-api/main.py` with a Pydantic request model that sets a `max_length` on text fields. Return `{"success": true, ...}` on success and HTTP 400 with `{"success": false, "error": "..."}` for bad input.
3. **Tests.** Add tests covering the happy path, each supported input format and every error branch. CI fails under 95% coverage and the current code is at 100%. Please keep it there.
4. **Page.** Add a template in `services/web/templates/` and a router in `services/web/routes/`, following `routes/compose.py`: the form dataclass goes in `models.py`, its checks in `inputs.py`, the converter-api call in `conversions.py` and every user-facing text in `messages.py`. Include the router in `main.py`. Link it from `index.html` and `base.html`. Test it in `services/web/tests/` with the fake API from `conftest.py`.
5. **Docs.** Add the endpoint to `docs/reference/api.md` and its rules to a page in `docs/reference/`. Keep `README.md` to the overview and quick start.

### CloudFormation to Terraform mappings

Each CloudFormation type with a hand-written `aws` provider mapping is a `ResourceConverter` subclass (`converters/cfn/resources/base.py`) in one of the modules in `converters/cfn/resources/`, listed in the `CONVERTERS` tuple of its module. The class names the two types and what `Ref` and `Fn::GetAtt` read. Its `convert(b)` method takes properties one at a time from `b.props` (`attr`, `number`, `json`, `tags`, `block`...). Properties it leaves are kept as comments and reported in the notes. Check attribute names against the provider schema (`terraform providers schema -json`), add the type to a fixture in `tests/fixtures/cfn/` and run `make validate-terraform`. Types without a mapping use the `awscc` provider through `converters/cfn/awscc_index.json`; rebuild that file with `python -m tools.build_awscc_index` (from `services/converter-api`) when you bump the awscc version.

## Adding a new service

1. `services/<name>/` with a `Dockerfile` that follows the existing ones: multi-stage, non-root UID 10001, `HEALTHCHECK`, `/health` and `/ready` endpoints.
2. `k8s/<name>/` manifests following the `web` pattern: probes, resource limits, non-root security context, a ConfigMap for its settings. The image name goes in `deployment.yaml`; scripts read it from there.
3. Add the name to the `SERVICES` list in `Makefile`, `.github/workflows/ci.yaml` and `scripts/deploy-local.sh`, add a job for it in `.github/workflows/build-push.yaml` and add its directory to `.github/dependabot.yml`.

## Web UI rules

The design system is one file: `services/web/styles/input.css`. Every colour, size, radius and font is a token there, named for its job (`surface`, `ink`, `accent`), with light and dark values side by side.

- **Token classes in templates.** Use token classes (`bg-surface`, `text-ink-muted`, `border-hairline`, `rounded-sm`, `text-step-1`) and the component classes (`btn`, `card`, `field`, `alert`, `code`, `nav-link`). The Tailwind palette, radii and text sizes are removed, so `bg-slate-50` or `rounded-lg` produce no CSS at all. When you need a value the tokens lack, add a token.
- **One component per concept.** A variant is a modifier class (`btn-primary`). A part two pages share, such as the file list with tabs, is one template both include (`_files.html`).
- **Both themes.** New colours get a light and a dark value, checked for contrast: 4.5:1 for text, 3:1 for interface borders. The two dark blocks in `input.css` must stay identical.
- **Keyboard and motion.** Anything clickable is at least 44px tall (`min-h-11`), focus stays visible and animation respects `prefers-reduced-motion` (handled globally).
- **Scripts.** `app.js` and the editor bundle are the only JavaScript. Every page works without them: forms post and the server renders the result. The one inline script (theme before first paint) is allowed by hash in the Content-Security-Policy; keep it the only one.
- **Icons.** Inline SVGs from `templates/icons/` (`{% include "icons/copy.svg" %}`), sized by the `.icon` class. Add icons from the pinned Lucide release named in the docs, strip `width`/`height` and add `aria-hidden="true"`.
- **Editors.** YAML and Terraform go through the CodeMirror editor (`services/web/editor/`), which reads its colours from the tokens. A new tool page needs only a `[data-editor]` container around its textarea and a result fragment; `app.js` does the rest.
- **Copy.** Follow the writing rules above. The product is called DevToolkit; pages name what the tool does.

`make css` rebuilds `static/app.css` and `make editor` rebuilds `static/vendor/editor.js` (Node.js 22); the Dockerfile does both during the image build.

## Kubernetes manifest rules

Every container must have:

- `resources.requests` **and** `resources.limits`
- `livenessProbe` and `readinessProbe`
- `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`, `readOnlyRootFilesystem: true`

The `devtoolkit` namespace enforces the `restricted` Pod Security Standard, so the cluster rejects pods that break these rules.

**Secrets stay outside code and manifests.** Use GitHub repo variables, Workload Identity or Kubernetes Secrets created outside the repo.

## Pull request process

1. Branch from `main`.
2. Keep each PR to one change: one converter, one fix and so on.
3. Run `make lint test` (and `make validate-k8s` if you touched `k8s/`) before pushing.
4. Open a PR into `main`. CI must pass: lint, tests, Docker builds, manifest validation, Terraform validation, end-to-end on kind.
5. A maintainer reviews and merges. Merging to `main` builds and pushes the images of the services that changed.

## License

By contributing, you agree your contributions are licensed under the [MIT License](LICENSE).
