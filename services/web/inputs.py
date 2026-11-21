"""Reads and checks what users submit, before anything is sent to converter-api."""

from __future__ import annotations

from fastapi import UploadFile

from errors import InputError
from messages import (
    BAD_LAYOUT,
    BAD_NAMESPACE,
    BAD_OUTPUT,
    BAD_PROVIDER,
    BAD_STACK_NAME,
    EMPTY_YAML,
    NOT_UTF8,
    TOO_LONG,
)
from models import CfnForm, ComposeForm
from settings import (
    BYTES_PER_CHAR,
    LAYOUTS,
    MAX_INPUT_CHARS,
    NAMESPACE_RE,
    OUTPUTS,
    PROVIDERS,
    STACK_NAME_RE,
)


async def read_input(
    text: str,
    upload: UploadFile | None,
    limit: int = MAX_INPUT_CHARS,
    empty: str = EMPTY_YAML,
) -> str:
    """Return the uploaded file if one was chosen, otherwise the pasted text."""
    if upload is not None and upload.filename:
        raw = await upload.read(limit * BYTES_PER_CHAR + 1)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InputError(NOT_UTF8) from exc
    if not text.strip():
        raise InputError(empty)
    if len(text) > limit:
        raise InputError(TOO_LONG.format(limit=limit))
    return text


def check_compose(form: ComposeForm) -> None:
    """Raise InputError when a Compose form option has a value converter-api rejects."""
    if not NAMESPACE_RE.match(form.namespace):
        raise InputError(BAD_NAMESPACE)
    if form.output not in OUTPUTS:
        raise InputError(BAD_OUTPUT)


def check_cfn(form: CfnForm) -> None:
    """Raise InputError when a CloudFormation form option has a value converter-api rejects."""
    if form.stack_name and not STACK_NAME_RE.match(form.stack_name):
        raise InputError(BAD_STACK_NAME)
    if form.provider not in PROVIDERS:
        raise InputError(BAD_PROVIDER)
    if form.layout not in LAYOUTS:
        raise InputError(BAD_LAYOUT)
