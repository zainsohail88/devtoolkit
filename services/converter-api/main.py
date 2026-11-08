"""converter-api: HTTP endpoints for DevToolkit conversion tools."""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from converters.compose_to_k8s import (
    ComposeError,
    check_yaml_limits,
    convert_compose_to_k8s,
    load_yaml_documents,
    manifests_to_yaml,
    parse_compose,
)
from converters.render import chart_name_from, render_files

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def resolve_log_level(value: str | None) -> tuple[str, str | None]:
    """Return (level, warning). An unknown LOG_LEVEL falls back to INFO instead of crashing,
    so a typo in the ConfigMap doesn't put every pod into CrashLoopBackOff."""
    level = (value or "info").strip().upper()
    if level in LOG_LEVELS:
        return level, None
    allowed = ", ".join(name.lower() for name in LOG_LEVELS)
    return "INFO", f"Unknown LOG_LEVEL {value!r}; using 'info'. Allowed: {allowed}"


logger = logging.getLogger("converter-api")


def configure_logging(value: str | None) -> str:
    """Set up logging from a LOG_LEVEL value and return the level used."""
    level, warning = resolve_log_level(value)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if warning:
        logger.warning(warning)
    return level


LOG_LEVEL = configure_logging(os.getenv("LOG_LEVEL"))

# Large enough for any real compose file, small enough to stop someone posting megabytes of YAML.
MAX_INPUT_CHARS = 100_000
# K8s namespace names are DNS-1123 labels.
NAMESPACE_PATTERN = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"

app = FastAPI(
    title="DevToolkit converter-api",
    description="Convert docker-compose files into Kubernetes manifests.",
    version="1.0.0",
)


class ConvertRequest(BaseModel):
    compose_yaml: str = Field(..., max_length=MAX_INPUT_CHARS, description="docker-compose YAML")
    namespace: str = Field("default", pattern=NAMESPACE_PATTERN)
    include_service: bool = True


class ConvertFilesRequest(ConvertRequest):
    output: Literal["manifests", "helm"] = Field(
        "manifests", description="'manifests': one YAML file per resource. 'helm': a Helm chart."
    )


class ValidateRequest(BaseModel):
    yaml: str = Field(..., max_length=MAX_INPUT_CHARS)


def _error(exc: ComposeError, status_code: int = 400) -> JSONResponse:
    content: dict[str, Any] = {"success": False, "error": exc.message}
    if exc.line is not None:
        content["line"] = exc.line
        content["column"] = exc.column
    return JSONResponse(status_code=status_code, content=content)


def _convert(request: ConvertRequest) -> dict[str, Any]:
    compose = parse_compose(request.compose_yaml)
    return convert_compose_to_k8s(compose, request.namespace, request.include_service)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe: the process is up."""
    return {"status": "healthy"}


@app.get("/ready")
def ready() -> dict[str, str]:
    """Readiness probe: the service can take traffic. No external dependencies to check yet."""
    return {"status": "ready"}


@app.post("/convert/compose-to-k8s", response_model=None)
def convert_compose(request: ConvertRequest) -> dict[str, Any] | JSONResponse:
    """Convert docker-compose YAML to K8s manifests, returned as JSON objects."""
    try:
        result = _convert(request)
    except ComposeError as exc:
        logger.info("conversion rejected: %s", exc)
        return _error(exc)
    return {"success": True, "manifests": result["manifests"], "notes": result["notes"]}


@app.post("/convert/compose-to-k8s/yaml", response_model=None)
def convert_compose_yaml(request: ConvertRequest) -> dict[str, Any] | JSONResponse:
    """Convert docker-compose YAML to K8s manifests, returned as one multi-document YAML string."""
    try:
        result = _convert(request)
    except ComposeError as exc:
        logger.info("conversion rejected: %s", exc)
        return _error(exc)
    return {
        "success": True,
        "yaml": manifests_to_yaml(result["manifests"]),
        "notes": result["notes"],
    }


@app.post("/convert/compose-to-k8s/files", response_model=None)
def convert_compose_files(request: ConvertFilesRequest) -> dict[str, Any] | JSONResponse:
    """Convert docker-compose YAML and return the result as files: plain manifests (one per
    resource, in apply order) or a Helm chart."""
    try:
        compose = parse_compose(request.compose_yaml)
        result = convert_compose_to_k8s(compose, request.namespace, request.include_service)
        files = render_files(result, request.output, chart_name_from(compose))
    except ComposeError as exc:
        logger.info("conversion rejected: %s", exc)
        return _error(exc)
    return {"success": True, "output": request.output, "files": files, "notes": result["notes"]}


@app.post("/validate/yaml")
def validate_yaml(request: ValidateRequest) -> dict[str, Any]:
    """Check YAML syntax. Supports multi-document input (separated by ---)."""
    try:
        check_yaml_limits(request.yaml)
        # Safe loader with linear-time `<<` merges (plain safe_load_all is exponential).
        documents = load_yaml_documents(request.yaml)
    except ComposeError as exc:
        return {"valid": False, "error": exc.message}
    except yaml.YAMLError as exc:
        # Only the parser's description of the input is returned, never the exception text.
        problem = getattr(exc, "problem", None) or "syntax error"
        mark = getattr(exc, "problem_mark", None)
        response: dict[str, Any] = {"valid": False, "error": f"Invalid YAML: {problem}"}
        if mark is not None:
            response["line"] = mark.line + 1
            response["column"] = mark.column + 1
        return response
    return {"valid": True, "documents": len(documents)}
