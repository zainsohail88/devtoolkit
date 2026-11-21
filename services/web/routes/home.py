"""The home page and the health probes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from rendering import page
from settings import INDEX_PAGE

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe: the process is up."""
    return {"status": "healthy"}


@router.get("/ready")
def ready() -> dict[str, str]:
    """Readiness probe. It leaves converter-api unchecked, so one slow service keeps the other
    in rotation; pages show an error instead."""
    return {"status": "ready"}


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """The home page, with a card for each tool."""
    return page(request, INDEX_PAGE, {})
