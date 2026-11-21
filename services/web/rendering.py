"""Renders the Jinja templates: whole pages, or just the result markup for the page script."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from models import Outcome
from security import THEME_SCRIPT
from settings import FRAGMENT_HEADER, FRAGMENT_VALUE, STATIC_DIR, TEMPLATES_DIR


class StaticVersions:
    """Versions the URLs of the files in static/ by their content."""

    def __init__(self, directory: Path) -> None:
        """Version the files in `directory`."""
        self.directory = directory
        self.versions: dict[str, str] = {}

    def url(self, name: str) -> str:
        """URL for a file in static/, versioned by its content. A missing file (e.g. app.css
        before `make css`) gets version 0 and the page still renders."""
        if name not in self.versions:
            try:
                digest = hashlib.sha256((self.directory / name).read_bytes()).hexdigest()
                self.versions[name] = digest[:8]
            except OSError:
                self.versions[name] = "0"
        return f"/static/{name}?v={self.versions[name]}"


static_versions = StaticVersions(STATIC_DIR)
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["theme_script"] = THEME_SCRIPT
templates.env.globals["static_url"] = static_versions.url


def page(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    """The template `name` rendered with `context`."""
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def wants_fragment(request: Request) -> bool:
    """The page script posts forms itself and asks for just the result markup."""
    return request.headers.get(FRAGMENT_HEADER) == FRAGMENT_VALUE


def wants_plain_text(request: Request) -> bool:
    """The page script asks for an example as plain text; a plain visit gets the filled form."""
    return "text/plain" in request.headers.get("accept", "")


def tool_page(request: Request, name: str, fragment: str, outcome: Outcome) -> HTMLResponse:
    """The whole page for a normal post, or just the result markup for the page script."""
    template = fragment if wants_fragment(request) else name
    return page(request, template, outcome.context, outcome.status)
