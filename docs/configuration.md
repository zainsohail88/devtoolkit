# Configuration

`converter-api` is configured with environment variables.

| Variable | Default | Allowed values | What it does |
|----------|---------|----------------|--------------|
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`, `critical` (any case) | How much the service logs. `info` logs rejected conversions; `debug` adds more detail. An unknown value doesn't stop the service: it logs a warning and uses `info`. |

The service always listens on port **8000**.

## Where to set it

| How you run it | Where |
|----------------|-------|
| Kubernetes | `LOG_LEVEL` in `k8s/converter-api/configmap.yaml`. After changing it, run `kubectl -n devtoolkit rollout restart deployment/converter-api` so the pods pick it up. |
| Locally with Python | `LOG_LEVEL=debug make run` |
| Plain Docker | `docker run -e LOG_LEVEL=debug ...` |

## Fixed limits

Input limits (100,000 characters, nesting depth, value count) are fixed in code, not configurable. See [API reference: input limits](reference/api.md#input-limits).
