# API reference

While the service is running, interactive docs (Swagger UI) are served at `/docs`, e.g. http://localhost:8000/docs.

The `web` service calls these endpoints from the server side. In the cluster, a NetworkPolicy allows only `web` pods to reach the API; in the hosted version, the API requires authentication and only `web` is allowed to call it. To use the API directly, run it yourself (`make run`).

## Endpoints

| Method | Path | What it does |
|--------|------|--------------|
| GET | `/health` | Liveness probe: `{"status": "healthy"}` |
| GET | `/ready` | Readiness probe: `{"status": "ready"}` |
| POST | `/convert/compose-to-k8s` | Convert compose YAML, return manifests as JSON |
| POST | `/convert/compose-to-k8s/yaml` | Same, return one multi-document YAML string |
| POST | `/convert/compose-to-k8s/files` | Same, return files: one per resource, or a Helm chart |
| POST | `/validate/yaml` | Check YAML syntax, report line/column of errors |

## Convert

Both convert endpoints take the same request:

```json
{
  "compose_yaml": "version: '3'\nservices:\n  web:\n    image: nginx\n    ports:\n      - '80:80'",
  "namespace": "default",
  "include_service": true
}
```

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `compose_yaml` | yes | | The docker-compose file as a string |
| `namespace` | no | `default` | Must be a valid Kubernetes namespace name |
| `include_service` | no | `true` | `false` skips generating Services |

**Success** (HTTP 200), from `/convert/compose-to-k8s`:

```json
{
  "success": true,
  "manifests": { "deployments": [...], "services": [...] },
  "notes": ["'web' has no resource limits"]
}
```

`manifests` has four lists, each possibly empty: `namespaces`, `persistent_volume_claims`, `deployments`, `services`.

`/convert/compose-to-k8s/yaml` returns `"yaml": "apiVersion: v1\n..."` in place of `manifests`: every resource in apply order (Namespace, PersistentVolumeClaims, Deployments, Services), separated by `---`.

`/convert/compose-to-k8s/files` takes one more field, `output`: `manifests` (default) or `helm`, and returns the result as files:

```json
{
  "success": true,
  "output": "manifests",
  "files": [
    {"path": "web-deployment.yaml", "content": "apiVersion: apps/v1\n..."},
    {"path": "web-service.yaml", "content": "apiVersion: v1\n..."}
  ],
  "notes": []
}
```

With `output: helm` the files are `Chart.yaml`, `values.yaml` and `templates/*.yaml`. An unknown `output` is a 422.

`notes` lists anything that needs a human decision. See [conversion rules](conversion-rules.md).

## Validate

`POST /validate/yaml` takes `{"yaml": "..."}`. Multi-document input (separated by `---`) is supported.

```json
{"valid": true, "documents": 2}
```

```json
{"valid": false, "error": "Invalid YAML: ...", "line": 2, "column": 2}
```

## Errors

| Status | When | Body |
|--------|------|------|
| 400 | The compose input can't be converted: invalid YAML, no services, a wrong field type, a bad value | `{"success": false, "error": "..."}` |
| 422 | The request itself is malformed: missing field, bad namespace name, input too long | FastAPI's standard validation error |

## Input limits

Real compose files are far below these limits. They exist so a hostile input can't tie up the server.

| Limit | Value | Response |
|-------|-------|----------|
| Input length | 100,000 characters | 422 |
| Nesting depth | 100 levels | 400 (`valid: false` from `/validate/yaml`) |
| Values in the document | 10,000, counted both as written and after expanding YAML anchors/aliases | 400 (`valid: false` from `/validate/yaml`) |
