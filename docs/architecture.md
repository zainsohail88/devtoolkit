# Architecture

## Two services

| Service | What it does | Port | Reachable from |
|---------|--------------|------|----------------|
| `web` | Serves the pages (Docker Compose → Kubernetes, CloudFormation → Terraform, YAML validator). Calls `converter-api` from the server side. | 8080 | Users |
| `converter-api` | The conversions, as a JSON API | 8000 | `web` only |

Only `web` calls `converter-api`. In the cluster a [NetworkPolicy](../k8s/converter-api/networkpolicy.yaml) admits traffic to it from `web` pods alone. That keeps input handling and output pages in one place (`web`) and leaves the API free to change.

## Project layout

```
services/
  converter-api/          FastAPI service: the conversions
    main.py               HTTP endpoints
    schemas.py            Request bodies of the endpoints
    converters/           Conversion logic, free of FastAPI
      yaml_safety.py      YAML loader with size, depth and alias limits
      compose_to_k8s.py   Docker Compose to Kubernetes
      compose_constants.py  Name limits, resource defaults, output kinds
      render.py           Manifests as files or as a Helm chart
      cfn/                CloudFormation, SAM and CDK to Terraform
        constants.py      Limits, provider versions, accepted options
        models.py         HCL nodes and the facts about parameters and resources
        errors.py         CfnError
        parse.py          Template text to a checked dict
        sam.py            SAM expansion, one expander class per SAM resource type
        sam_policies.py   SAM policy templates as IAM statements
        sam_constants.py  SAM tables: globals, inherited attributes, function properties
        cdk.py            CDK clean-up: metadata, asset parameters, names
        cdk_constants.py  The CDK custom resources, metadata and bootstrap names it handles
        expressions.py    Intrinsic functions as HCL expressions
        context.py        Conversion state: names, variables, locals, notes
        builder.py        Props and Builder, the toolkit of a resource converter
        resources/        Hand-written aws provider mappings: base.py (ResourceConverter),
                          registry.py and one module per AWS service group
        awscc.py          Any other type with the awscc provider, from awscc_index.json
        convert.py        Converter: the files of one template
        pipeline.py       TemplateConverter: text in, files and notes out
        hcl.py            HCL rendering that matches terraform fmt
    tools/                Developer scripts, run as python -m tools.<name>: build_awscc_index,
                          convert_fixtures and validate_report (for validate-terraform.sh)
    tests/                pytest tests; fixtures/cfn/ holds the templates checked with Terraform
    Dockerfile            Multi-stage build, non-root user
  web/                    FastAPI and Jinja2 service: the pages
    main.py               create_app: wires routers, middleware and error handlers
    routes/               One router per page: home, compose, cfn, yaml_check
    conversions.py        ConverterService: form to converter-api call to page context
    inputs.py, models.py  Form reading and checks, form and outcome dataclasses
    settings.py           Paths, limits, accepted options, environment variable names
    messages.py           Every user-facing text
    examples.py           The example inputs
    security.py           Security headers and cache rules
    middleware.py         Body size limit and headers on every response
    rendering.py          Jinja2 setup and page helpers
    archives.py           Zip downloads and the notes file
    error_pages.py        HTTP and form error pages
    api_client.py         Calls converter-api (with a Google identity token on Cloud Run)
    templates/            Jinja2 templates: index, one per tool, result fragments, error page.
                          _files.html is the file list both result fragments share
      icons/              Lucide icons (ISC) and the GitHub mark (Simple Icons, CC0), inlined
    static/               app.js (editors, in-place results, theme, copy and download);
                          the image build writes app.css and vendor/editor.js here
    styles/input.css      The design system: every colour, size and radius as a token, light
                          and dark values and the component classes. Compiled by Tailwind.
    editor/               CodeMirror 6 editor (YAML plus HCL for Terraform output):
                          src/editor.js plus pinned npm dependencies, bundled by esbuild
    tests/                pytest tests; a fake stands in for converter-api
    Dockerfile            Multi-stage build: Python deps, Tailwind CLI, Node (editor), runtime
k8s/
  namespace.yaml          devtoolkit namespace
  converter-api/          Deployment, Service, ConfigMap, NetworkPolicy
  web/                    Deployment, Service, ConfigMap
.github/
  workflows/              CI, image publishing, CodeQL
  CODEOWNERS, dependabot.yml, issue and PR templates
scripts/                  build.sh, deploy-local.sh, image-name.sh, kind-cluster.yaml,
                          validate-terraform.sh, check_code_rules.py (the house rules)
docs/                     This documentation
Makefile                  Common commands (`make help`)
.tool-versions            kind version (used by CI and checked by `make cluster-up`)
```

Conversion logic lives in `converter-api/converters/` as classes and functions that take strings or dicts and return dicts. `converter-api/main.py` handles HTTP alone and `web` handles pages alone. That keeps the logic easy to test and ready for a future CLI.

## How it runs on Kubernetes

The manifests in `k8s/` are plain YAML. Both services follow the same pattern:

| Aspect | Setting |
|--------|---------|
| Replicas | 2, rolling updates with zero unavailable pods |
| Probes | liveness on `/health`, readiness on `/ready` |
| Resources | `converter-api`: requests 100m CPU / 64Mi, limits 250m CPU / 128Mi. `web`: requests 100m CPU / 96Mi, limits 250m CPU / 192Mi |
| User | UID/GID 10001, `runAsNonRoot` |
| Filesystem | read-only root filesystem |
| Privileges | all Linux capabilities dropped, privilege escalation off, `RuntimeDefault` seccomp, service account token unmounted |
| Config | a ConfigMap per service, see [Configuration](configuration.md) |
| Network | ClusterIP Services (`web` on 8080, `converter-api` on 8000), internal to the cluster. `converter-api` accepts traffic from `web` pods only |

The `devtoolkit` namespace enforces the `restricted` [Pod Security Standard](https://kubernetes.io/docs/concepts/security/pod-security-standards/), so the cluster rejects any pod that tries to run as root.

The readiness probe of `web` checks `web` alone. When the API is down, the pages stay up and show an error, so each service stays in rotation on its own health.

## Images

Both Dockerfiles use the official slim Python image (the exact version is set in each Dockerfile and kept current by Dependabot) and run as UID 10001. Each has a Docker `HEALTHCHECK` for plain `docker run`; Kubernetes uses its own probes instead.

- `services/converter-api/Dockerfile`: **builder** installs dependencies into a virtualenv; **runtime** copies only the virtualenv and the code.
- `services/web/Dockerfile`: the same two stages, plus a **css** stage that downloads the standalone Tailwind CLI (version and SHA-256 pinned in the Dockerfile) and compiles `styles/input.css` into one small stylesheet plus an **editor** stage that bundles the CodeMirror editor from `editor/` with esbuild (`npm ci` against the committed lockfile). The runtime image gets their output alone.

## How a page works

Every page works without JavaScript: the form posts and the server renders the whole page again with the result. With JavaScript, `app.js` posts the same form with an `X-Requested-With: fetch` header. The server then returns the result fragment alone (`_result.html`, `_terraform_result.html` or `_validation.html`) and the script swaps it in place, turns the file list into tabs, mounts a read-only editor per file and marks the error line in the input editor from the `data-error-line` attribute of the fragment. The zip downloads are plain form posts to `/compose-to-k8s/zip` and `/cfn-to-terraform/zip`.

Images are published to GitHub Container Registry, one per service, tagged with the commit SHA and `latest`. Each image name is set once, in `k8s/<service>/deployment.yaml`; scripts and CI read it from there. See [CI/CD](operations/ci-cd.md).
