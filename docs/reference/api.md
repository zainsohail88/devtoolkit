# API reference

While the service runs, it serves interactive docs (Swagger UI) at `/docs`, for example http://localhost:8000/docs.

The `web` service calls these endpoints from its server. In the cluster, a NetworkPolicy lets only `web` pods reach the API. In the hosted version, the API requires authentication and only `web` may call it. To call the API yourself, run it locally with `make run`.

## Endpoints

| Method | Path | What it does |
|--------|------|--------------|
| GET | `/health` | Liveness probe: `{"status": "healthy"}` |
| GET | `/ready` | Readiness probe: `{"status": "ready"}` |
| POST | `/convert/compose-to-k8s` | Converts Compose YAML and returns the manifests as JSON |
| POST | `/convert/compose-to-k8s/yaml` | The same conversion, returned as one multi-document YAML string |
| POST | `/convert/compose-to-k8s/files` | The same conversion, returned as files: one per resource or a Helm chart |
| POST | `/convert/cfn-to-terraform` | Converts a CloudFormation, AWS SAM or AWS CDK template to Terraform files |
| POST | `/validate/yaml` | Checks YAML syntax and reports the line and column of an error |

## Convert

The three `compose-to-k8s` endpoints take the same request:

```json
{
  "compose_yaml": "version: '3'\nservices:\n  web:\n    image: nginx\n    ports:\n      - '80:80'",
  "namespace": "default",
  "include_service": true
}
```

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `compose_yaml` | yes | | The Docker Compose file as a string |
| `namespace` | no | `default` | Must be a valid Kubernetes namespace name |
| `include_service` | no | `true` | `false` leaves out the Services |

**Success** (HTTP 200), from `/convert/compose-to-k8s`:

```json
{
  "success": true,
  "manifests": { "deployments": [...], "services": [...] },
  "notes": ["'web' has no resource limits"]
}
```

`manifests` has four lists, any of which can be empty: `namespaces`, `persistent_volume_claims`, `deployments` and `services`.

`/convert/compose-to-k8s/yaml` returns `"yaml": "apiVersion: v1\n..."` instead of `manifests`. The string holds every resource in apply order (Namespace, PersistentVolumeClaims, Deployments, Services), separated by `---`.

`/convert/compose-to-k8s/files` takes one more field, `output`, set to `manifests` (the default) or `helm`. It returns the result as files:

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

With `output: helm`, the files are `Chart.yaml`, `values.yaml` and `templates/*.yaml`. An unknown `output` value returns a 422.

`notes` lists anything you need to decide or fix yourself. See [conversion rules](conversion-rules.md).

## CloudFormation to Terraform

`POST /convert/cfn-to-terraform` converts a CloudFormation template (JSON or YAML), an AWS SAM template or the template that `cdk synth` writes. [CloudFormation to Terraform](cloudformation-to-terraform.md) explains how each part converts.

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
| `provider` | no | `aws` | `aws`: hand-written `aws` provider mappings, with `awscc` for the other types. `awscc`: `awscc` for every resource |
| `layout` | no | `files` | `files`: `versions.tf`, `variables.tf`, `locals.tf`, `main.tf`, `outputs.tf`. `single`: everything in `main.tf` |
| `stack_name` | no | | `AWS::StackName` converts to `var.stack_name` and this field sets its default. It starts with a letter, followed by letters, digits and `-`, up to 128 characters |
| `include_imports` | no | `false` | Also writes `imports.tf`, so Terraform can adopt the resources that an existing stack created |

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

`source` is `cloudformation`, `sam` or `cdk`. `resources` lists each resource of the template after SAM expansion, with its Terraform address. A resource kept as a comment has the address `null` and the status `unsupported`.

Errors have the same shape as for the other endpoints, with `line` and `column` for syntax errors. If the converter cannot handle the structure of a template, it returns a 400 that asks you to open an issue.

## Validate

`POST /validate/yaml` takes `{"yaml": "..."}`. It accepts several documents separated by `---`.

```json
{"valid": true, "documents": 2}
```

```json
{"valid": false, "error": "Invalid YAML: ...", "line": 2, "column": 2}
```

## Errors

| Status | When | Body |
|--------|------|------|
| 400 | The input cannot be converted: invalid YAML or JSON, a file without services or resources, a wrong field type or a bad value | `{"success": false, "error": "..."}` |
| 422 | The request itself is malformed: a missing field, a bad namespace or stack name, an unknown option or input that is too long | The standard FastAPI validation error |

## Input limits

These limits cap the CPU time and memory that one request can use. Real files stay far below them.

| Limit | Compose and YAML check | CloudFormation | Response |
|-------|------------------------|----------------|----------|
| Input length | 100,000 characters | 500,000 characters | 422 |
| Nesting depth | 100 levels | 100 levels | 400 (`valid: false` from `/validate/yaml`) |
| Values in the document | 10,000 | 100,000 | 400 (`valid: false` from `/validate/yaml`) |

Values are counted both as written and after expanding YAML anchors and aliases.
