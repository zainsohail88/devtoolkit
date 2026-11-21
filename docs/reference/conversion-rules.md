# Conversion rules

This page shows how each Docker Compose (v3) field converts to Kubernetes. The converter writes only the resources that the compose file needs.

## What is generated

| Compose | Kubernetes | When |
|---------|------------|------|
| `services.<name>` | Deployment | Always |
| `services.<name>.ports` / `expose` | ClusterIP Service | The service has ports and Services are enabled |
| Named volume (`dbdata:/var/lib/postgresql/data`) | PersistentVolumeClaim, one per volume name | The volume is internal (`external` unset) |
| `namespace` other than `default` | Namespace | You set `namespace` to another name |

Files are written in apply order: Namespace, PersistentVolumeClaims, Deployments, Services.

## Field mapping

| Docker Compose | Kubernetes |
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
| `depends_on` | `initContainers` that wait for the port of the dependency |
| `volumes` | `volumes` + `volumeMounts`, PersistentVolumeClaims for named volumes |

### Ports

| Compose | Service port | Container port |
|---------|--------------|----------------|
| `"80"` | 80 | 80 |
| `"8080:80"` | 8080 | 80 |
| `"127.0.0.1:8080:80"` | 8080 | 80 (host IP dropped) |
| `"53:53/udp"` | 53/UDP | 53/UDP |
| `{target: 80, published: 8080}` | 8080 | 80 |

Port ranges (`"3000-3005"`) return an error.

### Resources

| Compose | Kubernetes |
|---------|------------|
| `cpus: '0.5'` | `500m` |
| `cpus: '2'` | `2` |
| `memory: 512M` | `512Mi` |
| `memory: 1g` | `1Gi` |
| `memory: 268435456` (bytes) | `256Mi` |

`cpus` must be at least `0.001`, because a smaller value would round to zero CPU.

Every container gets requests and limits. Without them, Kubernetes still schedules the pod but may evict it when the node runs low on resources. The converter sets them like this:

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

The same probe serves for liveness and readiness. With `test: ["NONE"]`, `disable: true` or no `test`, the container gets no probes. Durations use compose syntax (`1m30s`, `500ms`) and are rounded up to whole seconds.

### Startup order

Kubernetes starts pods in any order. For each entry in `depends_on`, the service gets an init container that waits until the dependency answers on its first Service port:

```yaml
initContainers:
  - name: wait-for-db
    image: busybox:1.37
    command: [sh, -c, "until nc -z db 5432; do sleep 2; done"]
```

When a dependency has no ports or Services are turned off, there is no port to wait on. The converter then adds a note in place of the init container. A `depends_on` entry that names a service missing from the file is an error.

### Volumes

| Compose entry | Kubernetes |
|---------------|------------|
| `dbdata:/var/lib/postgresql/data` (named) | PersistentVolumeClaim `dbdata` (1Gi, ReadWriteOnce) + mount |
| `data:/x:ro` | Same, mounted `readOnly` |
| `/tmp` (anonymous) | `emptyDir` + mount |
| `{type: tmpfs, target: /run}` | `emptyDir` with `medium: Memory` |
| `./src:/app` or `/host/path:/x` (bind mount) | Left out and reported in the notes |
| Named volume declared `external: true` | Mounted from a claim with that name, which must already exist |

The PVC size is a placeholder that a note asks you to adjust. A named volume that several services mount becomes one PVC. A note then warns that with ReadWriteOnce, those pods must run on the same node.

## Notes for manual attention

`notes` lists what you need to check or change:

| Compose | Note |
|---------|------|
| Bind mount | The mount is left out. Use a ConfigMap, bake the files into the image or use a PVC |
| `depends_on` a service without ports | The converter adds no init container. Make the dependent service retry its connection |
| `networks` | Left out, because services reach each other by name (for example `http://db:5432`) |
| `env_file` | Move the values into a ConfigMap or Secret |
| Env var with no value (`- API_KEY`) | Compose reads the value from the host. Set a value or use a Secret |
| `build` without `image` | Push the image to a registry your cluster can pull from |
| No limits / only reservations | The converter adds placeholder values or copies the reservations. Adjust them |
| Named volume | The PVC size is a placeholder. A shared volume is ReadWriteOnce, so its pods must run on the same node |
| External volume | A PVC with that name must already exist |

## Helm chart

With `output: helm`, the converter writes the same resources as a chart. The chart has a `Chart.yaml`, one template per resource and a `values.yaml` with what changes between environments: the image, replicas and resources of each service plus the size of each volume. The templates leave out the namespace, so Helm uses the release namespace. The chart takes its name from the top-level `name` of the compose file. Without a `name`, the chart is called `app`.

## Names

Kubernetes names must be lowercase letters, digits and `-`, start with a letter and be at most 63 characters. The converter fixes invalid names and adds a note:

- `my_app` becomes `my-app`
- `1api` becomes `svc-1api`

If two services end up with the same name, the conversion fails with an error.

## YAML parsing

The converter reads values the way Docker Compose does, with YAML 1.2 rules:

| Compose | YAML 1.1 would give | DevToolkit gives |
|---------|---------------------|------------------|
| `MODE: 0755` | `493` | `"0755"` |
| `FLAG: on` | `true` | `"on"` |
| `ports: [22:22]` | port 1342 | port 22 |

Anchors, aliases and `<<` merge keys work as usual.
