# --- Stage 1: install dependencies into a virtualenv ---------------------------
FROM python:3.15-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt

# --- Stage 2: compile the CSS with Tailwind's standalone CLI (no Node.js) -------
FROM debian:13-slim AS css

ARG TAILWIND_VERSION=v4.3.3
# From sha256sums.txt on the Tailwind release. Update both together.
ARG TAILWIND_SHA256_AMD64=dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a
ARG TAILWIND_SHA256_ARM64=55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in \
      amd64) asset=tailwindcss-linux-x64;   sum="$TAILWIND_SHA256_AMD64" ;; \
      arm64) asset=tailwindcss-linux-arm64; sum="$TAILWIND_SHA256_ARM64" ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -sSfL --retry 5 --retry-delay 5 -o /usr/local/bin/tailwindcss \
         "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}" \
    && echo "${sum}  /usr/local/bin/tailwindcss" | sha256sum -c - \
    && chmod +x /usr/local/bin/tailwindcss

WORKDIR /src
COPY styles/ styles/
COPY templates/ templates/
COPY static/app.js static/app.js
RUN tailwindcss -i styles/input.css -o /out/app.css --minify

# --- Stage 3: bundle the code editor (CodeMirror 6) with esbuild --------------------
FROM node:26-alpine AS editor

WORKDIR /src
COPY editor/package.json editor/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY editor/src/ src/
RUN npx esbuild src/editor.js --bundle --minify --format=iife --target=es2020 --outfile=/out/editor.js

# --- Stage 4: small runtime image, no build tools, non-root user ----------------
FROM python:3.15-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_LEVEL=info

# Fixed UID/GID so the K8s securityContext (runAsUser: 10001) matches.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app main.py api_client.py ./
COPY --chown=app:app templates/ templates/
COPY --chown=app:app static/ static/
COPY --from=css --chown=app:app /out/app.css static/app.css
COPY --from=editor --chown=app:app /out/editor.js static/vendor/editor.js

USER 10001:10001
EXPOSE 8080

# Used by plain `docker run`. Kubernetes ignores this and uses its own probes.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
