"""The HTTP middleware: request size limits, security headers and cache rules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import PlainTextResponse

from messages import LENGTH_REQUIRED, TOO_LARGE
from security import (
    HTML_CACHE,
    HTML_CONTENT_TYPE,
    SECURITY_HEADERS,
    STATIC_CACHE,
    STATIC_PREFIX,
)
from settings import MAX_BODY_BYTES


async def limits_and_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Refuse posts without a usable size, then add the headers to the response."""
    refusal = size_refusal(request)
    if refusal is not None:
        return refusal
    response = await call_next(request)
    add_headers(request, response)
    return response


def size_refusal(request: Request) -> Response | None:
    """The answer to a post without a Content-Length or with too large a body, else None."""
    if request.method != "POST":
        return None
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        return PlainTextResponse(LENGTH_REQUIRED, status_code=411)
    if int(length) > MAX_BODY_BYTES:
        return PlainTextResponse(TOO_LARGE, status_code=413)
    return None


def add_headers(request: Request, response: Response) -> None:
    """The security headers on every response, and the cache rule for static files and HTML."""
    response.headers.update(SECURITY_HEADERS)
    if request.url.path.startswith(STATIC_PREFIX):
        response.headers["Cache-Control"] = STATIC_CACHE
    elif response.headers.get("content-type", "").startswith(HTML_CONTENT_TYPE):
        response.headers["Cache-Control"] = HTML_CACHE
