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
| POST | `/convert/cfn-to-terraform` | Convert a CloudFormation, AWS SAM or AWS CDK template to Terraform files |
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

## CloudFormation to Terraform

`POST /convert/cfn-to-terraform` converts a CloudFormation template (JSON or YAML), an AWS SAM template, or the template `cdk synth` writes. See [CloudFormation to Terraform](cloudformation-to-terraform.md) for what becomes what.

```json
{
  "template": "Resources:\n  Bucket:\n    Type: AWS::S3::Bucket\n",
  "provider": "aws",
  "layout": "files",
  "stack_name": "shop",
  "include_imports": false
}
```

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `template` | yes | | The template as a string, at most 500,000 characters |
| `provider` | no | `aws` | `aws`: hand-written `aws` provider mappings, `awscc` for other types. `awscc`: every resource with `awscc` |
| `layout` | no | `files` | `files`: `versions.tf`, `variables.tf`, `locals.tf`, `main.tf`, `outputs.tf`. `single`: everything in `main.tf` |
| `stack_name` | no | | Default for `var.stack_name`, which replaces `AWS::StackName`. A letter, then letters, digits and `-` |
| `include_imports` | no | `false` | Also write `imports.tf`, to adopt the resources an existing stack created |

**Success** (HTTP 200):

```json
{
  "success": true,
  "source": "cloudformation",
  "files": [
    {"path": "versions.tf", "content": "terraform {\n..."},
    {"path": "main.tf", "content": "resource \"aws_s3_bucket\" \"bucket\" {}\n"}
  ],
  "notes": [],
  "resources": [
    {"logical_id": "Bucket", "type": "AWS::S3::Bucket", "address": "aws_s3_bucket.bucket", "status": "aws"}
  ],
  "summary": {"resources": 1, "aws": 1, "awscc": 0, "unsupported": 0}
}
```

`source` is `cloudformation`, `sam` or `cdk`. `resources` lists each resource of the template (after SAM expansion) with its Terraform address, or `null` and status `unsupported` when it could not be converted. Errors use the same shape as the other endpoints, with `line` and `column` for syntax errors. A template with a structure the converter doesn't handle gets a 400 asking for an issue, never a 500.

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
| 400 | The input can't be converted: invalid YAML or JSON, no services or resources, a wrong field type, a bad value | `{"success": false, "error": "..."}` |
| 422 | The request itself is malformed: missing field, bad namespace or stack name, unknown option, input too long | FastAPI's standard validation error |

## Input limits

Real files are far below these limits. They exist so a hostile input can't tie up the server.

| Limit | Compose and YAML check | CloudFormation | Response |
|-------|------------------------|----------------|----------|
| Input length | 100,000 characters | 500,000 characters | 422 |
| Nesting depth | 100 levels | 100 levels | 400 (`valid: false` from `/validate/yaml`) |
| Values in the document | 10,000 | 100,000 | 400 (`valid: false` from `/validate/yaml`) |

Values are counted both as written and after expanding YAML anchors and aliases.
