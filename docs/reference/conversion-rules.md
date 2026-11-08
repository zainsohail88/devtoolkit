# Conversion rules

How each docker-compose (v3) field becomes Kubernetes. The output is proportional to the input: only the resources the compose file needs are generated, nothing else.

## What is generated

| Compose | Kubernetes | When |
|---------|------------|------|
| `services.<name>` | Deployment | Always |
| `services.<name>.ports` / `expose` | ClusterIP Service | The service has ports and Services are enabled |
| Named volume (`dbdata:/var/lib/postgresql/data`) | PersistentVolumeClaim, one per volume name | The volume is not `external` |
| `namespace` other than `default` | Namespace | Requested namespace is not `default` |

Files are written in apply order: Namespace, PersistentVolumeClaims, Deployments, Services.

## Field mapping

| docker-compose | Kubernetes |
|----------------|------------|
| `image` | `containers[].image` |
| `build` (no `image`) | Image `<name>:latest`, with a note to push it to a registry |
| `ports`, `expose` | Service `ports` + `containerPort` |
| `environment` (list or dict) | `containers[].env` |
| `entrypoint` / `command` | `containers[].command` / `containers[].args` |
| `deploy.replicas` | `spec.replicas` |
| `deploy.resources.limits` | `resources.limits` |
| `deploy.resources.reservations` | `resources.requests` |
| `healthcheck` | `livenessProbe` and `readinessProbe` |
| `depends_on` | `initContainers` that wait for the dependency's port |
| `volumes` | `volumes` + `volumeMounts`, PersistentVolumeClaims for named volumes |

### Ports

| Compose | Service port | Container port |
|---------|--------------|----------------|
| `"80"` | 80 | 80 |
| `"8080:80"` | 8080 | 80 |
| `"127.0.0.1:8080:80"` | 8080 | 80 (host IP dropped) |
| `"53:53/udp"` | 53/UDP | 53/UDP |
| `{target: 80, published: 8080}` | 8080 | 80 |

Port ranges (`"3000-3005"`) aren't supported and return an error.

### Resources

| Compose | Kubernetes |
|---------|------------|
| `cpus: '0.5'` | `500m` |
| `cpus: '2'` | `2` |
| `memory: 512M` | `512Mi` |
| `memory: 1g` | `1Gi` |
| `memory: 268435456` (bytes) | `256Mi` |

`cpus` must be at least `0.001`, because anything smaller would round to zero CPU.

Every container gets requests and limits, because an unbounded pod is the one thing Kubernetes will happily schedule and then evict:

| Compose declares | Output | Note |
|------------------|--------|------|
| `limits` (with or without `reservations`) | As given | none |
| `reservations` only | `requests` = `limits` = the reservations | "limits set equal to the reservations" |
| Neither | requests `100m` / `128Mi`, limits `500m` / `512Mi` | "placeholder requests and limits were added; adjust them" |

### Health checks

| Compose `healthcheck` | Probe field |
|-----------------------|-------------|
| `test: ["CMD", "curl", "-f", "http://localhost"]` | `exec.command: [curl, -f, http://localhost]` |
| `test: ["CMD-SHELL", "pg_isready"]` or `test: "pg_isready"` | `exec.command: [sh, -c, pg_isready]` |
| `interval` | `periodSeconds` |
| `timeout` | `timeoutSeconds` |
| `retries` | `failureThreshold` |
| `start_period` | `initialDelaySeconds` |

The same probe is used for liveness and readiness. `test: ["NONE"]`, `disable: true` or a missing `test` produce no probes. Durations accept compose syntax (`1m30s`, `500ms`) and are rounded up to whole seconds.

### Startup order

Kubernetes starts pods in any order. For each entry in `depends_on`, the service gets an init container that waits until the dependency answers on its first Service port:

```yaml
initContainers:
  - name: wait-for-db
    image: busybox:1.37
    command: [sh, -c, "until nc -z db 5432; do sleep 2; done"]
```

A dependency without ports (or with Services disabled) can't be waited on, so it is reported as a note instead. A dependency that isn't a service in the file is an error.

### Volumes

| Compose entry | Kubernetes |
|---------------|------------|
| `dbdata:/var/lib/postgresql/data` (named) | PersistentVolumeClaim `dbdata` (1Gi, ReadWriteOnce) + mount |
| `data:/x:ro` | Same, mounted `readOnly` |
| `/tmp` (anonymous) | `emptyDir` + mount |
| `{type: tmpfs, target: /run}` | `emptyDir` with `medium: Memory` |
| `./src:/app` or `/host/path:/x` (bind mount) | Not converted; reported as a note |
| Named volume declared `external: true` | Mounted as a claim that must already exist; no PVC generated |

The PVC size is a placeholder and is reported as a note. A named volume mounted by more than one service becomes one PVC, with a note that ReadWriteOnce keeps those pods on the same node.

## Notes for manual attention

Only what the conversion could not do for you is reported in `notes`:

| Compose | Note |
|---------|------|
| Bind mount | Kubernetes has no host paths; use a ConfigMap, bake it into the image, or a PVC |
| `depends_on` a service without ports | Nothing to wait for; make the dependent retry its connection |
| `networks` | Not needed in Kubernetes; services reach each other by name |
| `env_file` | Move the values into a ConfigMap or Secret |
| Env var with no value (`- API_KEY`) | Compose reads it from the host; set it or use a Secret |
| `build` without `image` | Push the image to a registry your cluster can pull from |
| No limits / only reservations | Placeholder or copied values were added; adjust them |
| Named volume | PVC size is a placeholder; shared volumes are ReadWriteOnce |
| External volume | A PVC with that name must already exist |

## Helm chart

With `output: helm`, the same resources are written as a chart: `Chart.yaml`, a `values.yaml` holding what changes between environments (image, replicas, resources per service; size per volume), and one template per resource. Templates carry no namespace; Helm supplies the release namespace. The chart name comes from the compose file's top-level `name`, or `app`.

## Names

Kubernetes names must be lowercase letters, digits and `-`, start with a letter, and be at most 63 characters. Invalid names are fixed and noted:

- `my_app` becomes `my-app`
- `1api` becomes `svc-1api`

If two services end up with the same name, conversion fails with an error.

## YAML parsing

Values are read the way Docker Compose reads them (YAML 1.2), not the older YAML 1.1 rules:

| Compose | YAML 1.1 would give | DevToolkit gives |
|---------|---------------------|------------------|
| `MODE: 0755` | `493` | `"0755"` |
| `FLAG: on` | `true` | `"on"` |
| `ports: [22:22]` | port 1342 | port 22 |

Anchors, aliases and `<<` merge keys work as usual.
