# Configuration

Both services are configured with environment variables.

## converter-api

| Variable | Default | Allowed values | What it does |
|----------|---------|----------------|--------------|
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`, `critical` (any case) | How much the service logs. `info` logs rejected conversions; `debug` adds more detail. An unknown value doesn't stop the service: it logs a warning and uses `info`. |

The service always listens on port **8000**.

## web

| Variable | Default | Allowed values | What it does |
|----------|---------|----------------|--------------|
| `LOG_LEVEL` | `info` | as above | As above. `error` logs each time `converter-api` could not be reached. |
| `CONVERTER_API_URL` | `http://localhost:8000` | A URL | Where `converter-api` is. In the cluster: `http://converter-api:8000` (the Service name). |
| `CONVERTER_API_AUTH` | `none` | `none`, `google` | `google` sends a Google identity token with each API call, for a `converter-api` that requires authentication (Google Cloud Run). Needs the Google metadata server, so only works on Google Cloud. |

The service always listens on port **8080**.

## Where to set them

| How you run it | Where |
|----------------|-------|
| Kubernetes | `k8s/converter-api/configmap.yaml` and `k8s/web/configmap.yaml`. After changing one, run `kubectl -n devtoolkit rollout restart deployment/<service>` so the pods pick it up. |
| Locally with Python | `LOG_LEVEL=debug make run`, `CONVERTER_API_URL=http://localhost:8000 make run-web` |
| Plain Docker | `docker run -e LOG_LEVEL=debug ...` |

## Fixed limits

Input limits (100,000 characters, nesting depth, value count) are fixed in code, not configurable. See [API reference: input limits](reference/api.md#input-limits). `web` checks the length first, so you get a readable message instead of an API error, and refuses request bodies larger than about 400 KB.
