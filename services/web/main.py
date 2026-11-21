"""web: the DevToolkit web UI. Renders the pages and calls converter-api on the server side.

This module wires the app together; the routes live in routes/, the middleware in
middleware.py and the error pages in error_pages.py."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from api_client import ApiClient
from conversions import ConverterService
from error_pages import form_error, http_error
from log_config import configure_logging
from middleware import limits_and_headers
from routes import cfn, compose, home, yaml_check
from settings import (
    API_AUTH_DEFAULT,
    API_AUTH_VARIABLE,
    API_URL_DEFAULT,
    API_URL_VARIABLE,
    LOG_LEVEL_VARIABLE,
    STATIC_DIR,
)

LOG_LEVEL = configure_logging(os.getenv(LOG_LEVEL_VARIABLE))


def api_from_environment() -> ApiClient:
    """A converter-api client configured by CONVERTER_API_URL and CONVERTER_API_AUTH."""
    return ApiClient(
        os.getenv(API_URL_VARIABLE, API_URL_DEFAULT),
        auth=os.getenv(API_AUTH_VARIABLE, API_AUTH_DEFAULT),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hold a converter-api client while the app runs: the one given to create_app (kept in
    app.state.given_api), or else one configured from the environment."""
    api = app.state.given_api
    if api is None:
        api = api_from_environment()
    app.state.api = api
    app.state.converter = ConverterService(api)
    yield
    await api.aclose()


def create_app(api: ApiClient | None = None) -> FastAPI:
    """The web app. Tests pass `api` to reach a fake converter-api."""
    app = FastAPI(
        title="DevToolkit web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.given_api = api
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.middleware("http")(limits_and_headers)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, form_error)
    app.include_router(home.router)
    app.include_router(compose.router)
    app.include_router(cfn.router)
    app.include_router(yaml_check.router)
    return app


app = create_app()
