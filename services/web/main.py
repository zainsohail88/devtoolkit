"""web: DevToolkit's web UI. Renders the pages and calls converter-api server-side."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api_client import ApiClient, ApiUnavailableError

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def resolve_log_level(value: str | None) -> tuple[str, str | None]:
    """Return (level, warning). An unknown LOG_LEVEL falls back to INFO instead of crashing."""
    level = (value or "info").strip().upper()
    if level in LOG_LEVELS:
        return level, None
    allowed = ", ".join(name.lower() for name in LOG_LEVELS)
    return "INFO", f"Unknown LOG_LEVEL {value!r}; using 'info'. Allowed: {allowed}"


logger = logging.getLogger("web")


def configure_logging(value: str | None) -> str:
    """Set up logging from a LOG_LEVEL value and return the level used."""
    level, warning = resolve_log_level(value)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if warning:
        logger.warning(warning)
    return level


LOG_LEVEL = configure_logging(os.getenv("LOG_LEVEL"))

BASE_DIR = Path(__file__).parent
# Same limit as converter-api, checked here first so users get a clear message.
MAX_INPUT_CHARS = 100_000
# Room for the form fields and multipart framing around a maximum-size input.
MAX_BODY_BYTES = 4 * MAX_INPUT_CHARS + 16_384
NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
UNAVAILABLE = "The converter is unavailable right now. Please try again in a minute."

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

templates = Jinja2Templates(directory=BASE_DIR / "templates")


class InputError(Exception):
    """The user's input can't be sent to converter-api. The message is shown on the page."""


async def read_input(text: str, upload: UploadFile | None) -> str:
    """Return the uploaded file if one was chosen, otherwise the pasted text."""
    if upload is not None and upload.filename:
        raw = await upload.read(MAX_INPUT_CHARS * 4 + 1)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InputError("The uploaded file isn't UTF-8 text.") from exc
    if not text.strip():
        raise InputError("Paste some YAML or choose a file to upload.")
    if len(text) > MAX_INPUT_CHARS:
        raise InputError(f"The input is longer than {MAX_INPUT_CHARS:,} characters.")
    return text


def create_app(api: ApiClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.api = api or ApiClient(
            os.getenv("CONVERTER_API_URL", "http://localhost:8000"),
            auth=os.getenv("CONVERTER_API_AUTH", "none"),
        )
        yield
        await app.state.api.aclose()

    app = FastAPI(
        title="DevToolkit web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.middleware("http")
    async def limits_and_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method == "POST":
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                return PlainTextResponse("Content-Length required", status_code=411)
            if int(length) > MAX_BODY_BYTES:
                return PlainTextResponse("Request too large", status_code=413)
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    def page(
        request: Request, name: str, context: dict[str, Any], status_code: int = 200
    ) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness probe: the process is up."""
        return {"status": "healthy"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        """Readiness probe. Doesn't check converter-api, so one slow service can't take the
        other out of rotation; pages show an error instead."""
        return {"status": "ready"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return page(request, "index.html", {})

    @app.get("/compose-to-k8s", response_class=HTMLResponse)
    def compose_form(request: Request) -> HTMLResponse:
        return page(
            request,
            "compose_to_k8s.html",
            {"compose_yaml": "", "namespace": "default", "include_service": True},
        )

    @app.post("/compose-to-k8s", response_class=HTMLResponse)
    async def compose_convert(
        request: Request,
        compose_yaml: str = Form(""),
        compose_file: UploadFile | None = None,
        namespace: str = Form("default"),
        include_service: bool = Form(False),
    ) -> HTMLResponse:
        context: dict[str, Any] = {
            "compose_yaml": compose_yaml,
            "namespace": namespace,
            "include_service": include_service,
        }
        try:
            context["compose_yaml"] = await read_input(compose_yaml, compose_file)
            if not NAMESPACE_RE.match(namespace):
                raise InputError(
                    "Namespace must be lowercase letters, digits and '-', at most 63 characters, "
                    "starting and ending with a letter or digit."
                )
        except InputError as exc:
            return page(request, "compose_to_k8s.html", context | {"error": str(exc)}, 400)

        try:
            status, body = await request.app.state.api.post(
                "/convert/compose-to-k8s/yaml",
                {
                    "compose_yaml": context["compose_yaml"],
                    "namespace": namespace,
                    "include_service": include_service,
                },
            )
        except ApiUnavailableError as exc:
            logger.error("converter-api unavailable: %s", exc)
            return page(request, "compose_to_k8s.html", context | {"error": UNAVAILABLE}, 502)

        if status == 200 and body.get("success"):
            return page(
                request,
                "compose_to_k8s.html",
                context | {"result": body.get("yaml", ""), "notes": body.get("notes", [])},
            )
        error = body.get("error") or "The converter couldn't process this input."
        return page(request, "compose_to_k8s.html", context | {"error": error}, 400)

    @app.get("/validate-yaml", response_class=HTMLResponse)
    def validate_form(request: Request) -> HTMLResponse:
        return page(request, "validate_yaml.html", {"yaml_text": ""})

    @app.post("/validate-yaml", response_class=HTMLResponse)
    async def validate(
        request: Request,
        yaml_text: str = Form(""),
        yaml_file: UploadFile | None = None,
    ) -> HTMLResponse:
        context: dict[str, Any] = {"yaml_text": yaml_text}
        try:
            context["yaml_text"] = await read_input(yaml_text, yaml_file)
        except InputError as exc:
            return page(request, "validate_yaml.html", context | {"error": str(exc)}, 400)

        try:
            status, body = await request.app.state.api.post(
                "/validate/yaml", {"yaml": context["yaml_text"]}
            )
        except ApiUnavailableError as exc:
            logger.error("converter-api unavailable: %s", exc)
            return page(request, "validate_yaml.html", context | {"error": UNAVAILABLE}, 502)

        if status != 200 or "valid" not in body:
            return page(
                request,
                "validate_yaml.html",
                context | {"error": "The validator couldn't process this input."},
                400,
            )
        return page(request, "validate_yaml.html", context | {"validation": body})

    return app


app = create_app()
