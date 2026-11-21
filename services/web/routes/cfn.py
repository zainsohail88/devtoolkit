"""The CloudFormation to Terraform page."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse

from archives import notes_file, zip_download
from conversions import converter_service
from examples import EXAMPLE_CLOUDFORMATION, EXAMPLES
from models import CfnForm
from rendering import page, tool_page, wants_plain_text
from settings import CFN_PAGE, CFN_RESULT, TERRAFORM_ZIP_NAME

router = APIRouter()


@router.get("/cfn-to-terraform", response_class=HTMLResponse)
def cfn_form(request: Request, example: str | None = None) -> Response:
    """The empty form, or the form filled with an example (as plain text for the page script).
    An unknown example name gets the CloudFormation example."""
    if not example:
        return page(request, CFN_PAGE, CfnForm("").context())
    text = EXAMPLES.get(example, EXAMPLE_CLOUDFORMATION)
    if wants_plain_text(request):
        return PlainTextResponse(text)
    return page(request, CFN_PAGE, CfnForm(text).context())


@router.post("/cfn-to-terraform", response_class=HTMLResponse)
async def cfn_convert(
    request: Request,
    template: str = Form(""),
    template_file: UploadFile | None = None,
    provider: str = Form("aws"),
    layout: str = Form("files"),
    stack_name: str = Form(""),
    include_imports: bool = Form(False),
) -> HTMLResponse:
    """Convert the posted template and show the result."""
    form = CfnForm(template, provider, layout, stack_name.strip(), include_imports)
    outcome = await converter_service(request).cfn(form, template_file)
    return tool_page(request, CFN_PAGE, CFN_RESULT, outcome)


@router.post("/cfn-to-terraform/zip")
async def cfn_zip(
    request: Request,
    template: str = Form(""),
    template_file: UploadFile | None = None,
    provider: str = Form("aws"),
    layout: str = Form("files"),
    stack_name: str = Form(""),
    include_imports: bool = Form(False),
) -> Response:
    """The same conversion, downloaded as a zip of the .tf files and the notes."""
    form = CfnForm(template, provider, layout, stack_name.strip(), include_imports)
    outcome = await converter_service(request).cfn(form, template_file)
    if not outcome.ok:
        return page(request, CFN_PAGE, outcome.context, outcome.status)
    context = outcome.context
    files = context["files"] + [notes_file(context["notes"], context["source"])]
    return zip_download(files, TERRAFORM_ZIP_NAME)
