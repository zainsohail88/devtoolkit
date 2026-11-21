"""The YAML check page."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from conversions import converter_service
from rendering import page, tool_page
from settings import YAML_PAGE, YAML_RESULT

router = APIRouter()


@router.get("/validate-yaml", response_class=HTMLResponse)
def validate_form(request: Request) -> HTMLResponse:
    """The empty form."""
    return page(request, YAML_PAGE, {"yaml_text": ""})


@router.post("/validate-yaml", response_class=HTMLResponse)
async def validate(
    request: Request,
    yaml_text: str = Form(""),
    yaml_file: UploadFile | None = None,
) -> HTMLResponse:
    """Check the posted YAML and show the result."""
    outcome = await converter_service(request).validate_yaml(yaml_text, yaml_file)
    return tool_page(request, YAML_PAGE, YAML_RESULT, outcome)
