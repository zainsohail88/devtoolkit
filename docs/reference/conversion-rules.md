# Conversion rules

How each docker-compose (v3) field becomes Kubernetes. Each compose service becomes one Deployment, plus a ClusterIP Service if it has ports.

## Field mapping

| docker-compose | Kubernetes |
|----------------|------------|
| `services.<name>` | Deployment, plus a ClusterIP Service if it has ports |
| `image` | `containers[].image` |
| `build` (no `image`) | Image `<name>:latest`, with a note to push it to a registry |
| `ports`, `expose` | Service `ports` + `containerPort` |
| `environment` (list or dict) | `containers[].env` |
| `entrypoint` / `command` | `containers[].command` / `containers[].args` |
| `deploy.replicas` | `spec.replicas` |
| `deploy.resources.limits` | `resources.limits` |
| `deploy.resources.reservations` | `resources.requests` |

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

## Notes for manual attention

These compose features have no direct Kubernetes equivalent. They're reported in `notes`, not converted:

| Compose | Note |
|---------|------|
| `volumes` | Consider a PersistentVolumeClaim or ConfigMap |
| `depends_on` | K8s doesn't order startup; consider initContainers or readiness probes |
| `networks` | K8s uses service DNS: reach other services by name |
| `healthcheck` | Convert to liveness/readiness probes |
| `env_file` | Move the values into a ConfigMap or Secret |
| Env var with no value (`- API_KEY`) | Compose reads it from the host; set it or use a Secret |
| No CPU/memory limits | `'<name>' has no resource limits` |

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
