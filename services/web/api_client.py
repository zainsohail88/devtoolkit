"""Calls converter-api from the server side. Browsers never talk to converter-api directly."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

# Cloud Run's metadata server issues ID tokens for the service account the container runs as.
METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)
# Google ID tokens are valid for 1 hour. Refresh well before that.
TOKEN_LIFETIME_SECONDS = 50 * 60
AUTH_MODES = ("none", "google")


class ApiUnavailableError(Exception):
    """converter-api could not be reached or did not give a usable answer."""


class GoogleIdTokens:
    """Fetches and caches ID tokens so Cloud Run lets this service call a private service."""

    def __init__(self, audience: str, client: httpx.AsyncClient) -> None:
        self._audience = audience
        self._client = client
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token is None or time.monotonic() >= self._expires_at:
                response = await self._client.get(
                    METADATA_IDENTITY_URL,
                    params={"audience": self._audience},
                    headers={"Metadata-Flavor": "Google"},
                )
                response.raise_for_status()
                self._token = response.text.strip()
                self._expires_at = time.monotonic() + TOKEN_LIFETIME_SECONDS
            return self._token


class ApiClient:
    def __init__(
        self,
        base_url: str,
        auth: str = "none",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        if auth not in AUTH_MODES:
            raise ValueError(f"CONVERTER_API_AUTH must be one of {', '.join(AUTH_MODES)}")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )
        self._tokens = (
            GoogleIdTokens(base_url.rstrip("/"), self._client) if auth == "google" else None
        )

    async def post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST JSON and return (status, body). Raises ApiUnavailableError for anything that
        isn't an answer from converter-api itself: network errors, auth failures, 5xx."""
        try:
            headers = {}
            if self._tokens is not None:
                headers["Authorization"] = f"Bearer {await self._tokens.get()}"
            response = await self._client.post(path, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 500 or response.status_code in (401, 403):
            raise ApiUnavailableError(f"converter-api returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiUnavailableError("converter-api returned a non-JSON response") from exc
        if not isinstance(body, dict):
            raise ApiUnavailableError("converter-api returned an unexpected response")
        return response.status_code, body

    async def aclose(self) -> None:
        await self._client.aclose()
