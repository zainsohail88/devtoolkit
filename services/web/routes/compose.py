"""The Compose to Kubernetes page."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse

from archives import zip_download
from conversions import converter_service
from examples import EXAMPLE_COMPOSE
from models import ComposeForm
from rendering import page, tool_page, wants_plain_text
from settings import COMPOSE_PAGE, COMPOSE_RESULT, HELM_ZIP_NAME, MANIFESTS_ZIP_NAME

router = APIRouter()


@router.get("/compose-to-k8s", response_class=HTMLResponse)
def compose_form(request: Request, example: str | None = None) -> Response:
    """The empty form, or the form filled with the example (as plain text for the page script)."""
    if not example:
        return page(request, COMPOSE_PAGE, ComposeForm("").context())
    if wants_plain_text(request):
        return PlainTextResponse(EXAMPLE_COMPOSE)
    return page(request, COMPOSE_PAGE, ComposeForm(EXAMPLE_COMPOSE, "shop").context())


@router.post("/compose-to-k8s", response_class=HTMLResponse)
async def compose_convert(
    request: Request,
    compose_yaml: str = Form(""),
    compose_file: UploadFile | None = None,
    namespace: str = Form("default"),
    include_service: bool = Form(False),
    output: str = Form("manifests"),
) -> HTMLResponse:
    """Convert the posted Compose file and show the result."""
    form = ComposeForm(compose_yaml, namespace, include_service, output)
    outcome = await converter_service(request).compose(form, compose_file)
    return tool_page(request, COMPOSE_PAGE, COMPOSE_RESULT, outcome)


@router.post("/compose-to-k8s/zip")
async def compose_zip(
    request: Request,
    compose_yaml: str = Form(""),
    compose_file: UploadFile | None = None,
    namespace: str = Form("default"),
    include_service: bool = Form(False),
    output: str = Form("manifests"),
) -> Response:
    """The same conversion, downloaded as a zip of the files."""
    form = ComposeForm(compose_yaml, namespace, include_service, output)
    outcome = await converter_service(request).compose(form, compose_file)
    if not outcome.ok:
        return page(request, COMPOSE_PAGE, outcome.context, outcome.status)
    name = HELM_ZIP_NAME if output == "helm" else MANIFESTS_ZIP_NAME
    return zip_download(outcome.context["files"], name)
