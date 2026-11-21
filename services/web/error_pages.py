"""Error pages with a way back, in place of the JSON errors of FastAPI."""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from messages import BAD_FORM, HTTP_ERROR_MESSAGES
from rendering import page
from settings import ERROR_PAGE


async def http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
    """The error page for an HTTP error, such as an unknown address."""
    # Starlette always sets the detail, at least to the status phrase.
    message = HTTP_ERROR_MESSAGES.get(exc.status_code, str(exc.detail))
    return page(
        request, ERROR_PAGE, {"status": exc.status_code, "message": message}, exc.status_code
    )


async def form_error(request: Request, exc: RequestValidationError) -> HTMLResponse:
    """The error page for a form with a field FastAPI cannot read, such as a bad checkbox."""
    return page(request, ERROR_PAGE, {"status": 400, "message": BAD_FORM}, 400)
