# Configuration

You configure both services with environment variables.

## converter-api

| Variable | Default | Allowed values | What it does |
|----------|---------|----------------|--------------|
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`, `critical` (any case) | How much the service logs. `info` logs rejected conversions and `debug` adds more detail. With an unknown value, the service logs a warning and uses `info`. |

The service always listens on port **8000**.

## web

| Variable | Default | Allowed values | What it does |
|----------|---------|----------------|--------------|
| `LOG_LEVEL` | `info` | As for `converter-api` | As for `converter-api`. Even at `error`, the service logs each time it cannot reach `converter-api`. |
| `CONVERTER_API_URL` | `http://localhost:8000` | A URL | The address of `converter-api`. In the cluster this is `http://converter-api:8000`, after the Service name. |
| `CONVERTER_API_AUTH` | `none` | `none`, `google` | `google` sends a Google identity token with each API call. Use it when `converter-api` requires authentication, as on Google Cloud Run. The token comes from the Google metadata server, so this works on Google Cloud only. |

The service always listens on port **8080**.

## Where to set them

| How you run it | Where |
|----------------|-------|
| Kubernetes | `k8s/converter-api/configmap.yaml` and `k8s/web/configmap.yaml`. After you change one, run `kubectl -n devtoolkit rollout restart deployment/<service>` so the pods pick up the change. |
| Locally with Python | `LOG_LEVEL=debug make run`, `CONVERTER_API_URL=http://localhost:8000 make run-web` |
| Plain Docker | `docker run -e LOG_LEVEL=debug ...` |

## Fixed limits

The input limits (characters, nesting depth and value count) are set in code. See [API reference: input limits](reference/api.md#input-limits). `web` checks the input length before it calls the API and shows a clear message on the page. It refuses request bodies larger than about 2 MB.
