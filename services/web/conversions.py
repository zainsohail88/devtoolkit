"""Sends checked input to converter-api and turns each answer into what the page shows."""

from __future__ import annotations

from typing import Any

from fastapi import Request, UploadFile

from api_client import ApiClient, ApiUnavailableError
from errors import InputError
from inputs import check_cfn, check_compose, read_input
from log_config import logger
from messages import CONVERTER_FAILED, EMPTY_TEMPLATE, UNAVAILABLE, VALIDATOR_FAILED
from models import CfnForm, ComposeForm, Outcome
from settings import (
    CFN_API_PATH,
    COMPOSE_API_PATH,
    DEFAULT_SOURCE,
    MAX_TEMPLATE_CHARS,
    VALIDATE_API_PATH,
)


class ConverterService:
    """The three tools of the site, each one call to converter-api."""

    def __init__(self, api: ApiClient) -> None:
        """Use `api` to reach converter-api."""
        self.api = api

    async def compose(self, form: ComposeForm, upload: UploadFile | None) -> Outcome:
        """Convert a Compose file to Kubernetes manifests or a Helm chart."""
        context = form.context()
        try:
            context["compose_yaml"] = await read_input(form.compose_yaml, upload)
            check_compose(form)
        except InputError as exc:
            return Outcome(context | {"error": str(exc)}, 400)
        payload = {
            "compose_yaml": context["compose_yaml"],
            "namespace": form.namespace,
            "include_service": form.include_service,
            "output": form.output,
        }
        answer = await self.post(COMPOSE_API_PATH, payload)
        if answer is None:
            return Outcome(context | {"error": UNAVAILABLE}, 502)
        status, body = answer
        if not self.converted(status, body):
            return self.rejected(context, body)
        return Outcome(context | {"files": body["files"], "notes": body.get("notes", [])})

    async def cfn(self, form: CfnForm, upload: UploadFile | None) -> Outcome:
        """Convert a CloudFormation, AWS SAM or synthesized AWS CDK template to Terraform."""
        context = form.context()
        try:
            context["template"] = await read_input(
                form.template, upload, MAX_TEMPLATE_CHARS, EMPTY_TEMPLATE
            )
            check_cfn(form)
        except InputError as exc:
            return Outcome(context | {"error": str(exc)}, 400)
        payload: dict[str, Any] = {
            "template": context["template"],
            "provider": form.provider,
            "layout": form.layout,
            "include_imports": form.include_imports,
        }
        if form.stack_name:
            payload["stack_name"] = form.stack_name
        answer = await self.post(CFN_API_PATH, payload)
        if answer is None:
            return Outcome(context | {"error": UNAVAILABLE}, 502)
        status, body = answer
        if not self.converted(status, body):
            return self.rejected(context, body)
        return Outcome(
            context
            | {
                "files": body["files"],
                "notes": body.get("notes", []),
                "resources": body.get("resources", []),
                "summary": body.get("summary", {}),
                "source": body.get("source", DEFAULT_SOURCE),
            }
        )

    async def validate_yaml(self, text: str, upload: UploadFile | None) -> Outcome:
        """Check that the text is valid YAML."""
        context: dict[str, Any] = {"yaml_text": text}
        try:
            context["yaml_text"] = await read_input(text, upload)
        except InputError as exc:
            return Outcome(context | {"error": str(exc)}, 400)
        answer = await self.post(VALIDATE_API_PATH, {"yaml": context["yaml_text"]})
        if answer is None:
            return Outcome(context | {"error": UNAVAILABLE}, 502)
        status, body = answer
        if status != 200 or "valid" not in body:
            return Outcome(context | {"error": VALIDATOR_FAILED}, 400)
        return Outcome(context | {"validation": body})

    async def post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        """The (status, body) answer of converter-api, or None when it is unavailable."""
        try:
            return await self.api.post(path, payload)
        except ApiUnavailableError as exc:
            logger.error("converter-api unavailable: %s", exc)
            return None

    @staticmethod
    def converted(status: int, body: dict[str, Any]) -> bool:
        """Whether converter-api answered with a list of converted files."""
        return status == 200 and bool(body.get("success")) and isinstance(body.get("files"), list)

    @staticmethod
    def rejected(context: dict[str, Any], body: dict[str, Any]) -> Outcome:
        """The error converter-api gave, with its position in the input when it has one."""
        return Outcome(
            context
            | {
                "error": body.get("error") or CONVERTER_FAILED,
                "error_line": body.get("line"),
                "error_column": body.get("column"),
            },
            400,
        )


def converter_service(request: Request) -> ConverterService:
    """The ConverterService of the running app, set up by its lifespan."""
    return request.app.state.converter
