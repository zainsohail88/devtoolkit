"""Names, limits and defaults of the compose to Kubernetes conversion and its renderers."""

from __future__ import annotations

import re

# K8s Service names must be DNS-1035 labels: lowercase alphanumerics and '-', starting with a
# letter, max 63 chars. Deployments get the same name so labels and selectors line up.
DNS_LABEL_MAX = 63
INVALID_NAME_CHARS = re.compile(r"[^a-z0-9-]+")
NAME_PREFIX = "svc-"

# Deployment spec.replicas is an int32.
MAX_REPLICAS = 2**31 - 1
DIGITS = re.compile(r"[0-9]+")

# Compose memory units -> K8s binary suffixes. Docker treats these as powers of 1024.
MEMORY_UNITS = {
    "b": "",
    "k": "Ki",
    "kb": "Ki",
    "m": "Mi",
    "mb": "Mi",
    "g": "Gi",
    "gb": "Gi",
}
# Applied to a stripped value. No two adjacent repetitions can match the same character, so
# matching stays linear however long the input is.
MEMORY_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)[ \t]*([a-zA-Z]*)$")

# Placeholder resources, added when a service declares none. Kubernetes schedules an
# unbounded pod otherwise; the note tells the user these are starting points.
DEFAULT_REQUESTS = {"cpu": "100m", "memory": "128Mi"}
DEFAULT_LIMITS = {"cpu": "500m", "memory": "512Mi"}
# Named volumes become PersistentVolumeClaims of this size; the note says to adjust it.
DEFAULT_VOLUME_SIZE = "1Gi"
# Init containers that wait for the port of a dependency. Pinned, tiny, has `nc`.
WAIT_IMAGE = "busybox:1.37"

DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|us|µs|ns|h|m|s)")
DURATION_SECONDS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}
BIND_PREFIXES = ("/", "./", "../", "~")
# Manifest kinds in apply order: a namespace before what goes in it, storage before the
# workloads that mount it, workloads before their Services.
MANIFEST_ORDER = ("namespaces", "persistent_volume_claims", "deployments", "services")

# Renderers (render.py).
OUTPUTS = ("manifests", "helm")
FILE_SUFFIX = {
    "namespaces": "namespace",
    "persistent_volume_claims": "pvc",
    "deployments": "deployment",
    "services": "service",
}
CHART_NAME_INVALID = re.compile(r"[^a-z0-9-]+")
# Sentinels: dumped by PyYAML as plain text, then swapped for Helm template expressions.
SENTINEL = "__HELM_{kind}_{name}__"
