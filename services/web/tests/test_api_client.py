from __future__ import annotations

import httpx
import pytest

import api_client
from api_client import METADATA_IDENTITY_URL, ApiClient


def is_metadata(request: httpx.Request) -> bool:
    return str(request.url).startswith(METADATA_IDENTITY_URL)


class CheckedTokenServer:
    """The metadata server and converter-api: checks the token request and its use."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if is_metadata(request):
            assert request.headers["Metadata-Flavor"] == "Google"
            assert request.url.params["audience"] == "https://converter-api.example"
            return httpx.Response(200, text="id-token-1\n")
        assert request.headers["Authorization"] == "Bearer id-token-1"
        return httpx.Response(200, json={"ok": True})


class RotatingTokenServer:
    """A metadata server that hands out a new token each time, and records which one is used."""

    def __init__(self) -> None:
        self.tokens = iter(["first", "second"])
        self.seen: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if is_metadata(request):
            return httpx.Response(200, text=next(self.tokens))
        self.seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={})


def outside_cloud_run(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, text="not on Cloud Run")


def without_authorization(request: httpx.Request) -> httpx.Response:
    assert "Authorization" not in request.headers
    return httpx.Response(400, json={"success": False, "error": "nope"})


def test_rejects_unknown_auth_mode():
    with pytest.raises(ValueError, match="CONVERTER_API_AUTH"):
        ApiClient("http://api", auth="basic")


@pytest.mark.anyio
async def test_google_auth_fetches_id_token_once_and_sends_it():
    server = CheckedTokenServer()
    client = ApiClient(
        "https://converter-api.example/",
        auth="google",
        transport=httpx.MockTransport(server.handler),
    )
    assert await client.post("/validate/yaml", {"yaml": "a: 1"}) == (200, {"ok": True})
    assert await client.post("/validate/yaml", {"yaml": "b: 2"}) == (200, {"ok": True})
    await client.aclose()
    # One token fetch, two API calls.
    assert [is_metadata(request) for request in server.calls] == [True, False, False]


@pytest.mark.anyio
async def test_google_auth_refreshes_expired_token():
    server = RotatingTokenServer()
    client = ApiClient("http://api", auth="google", transport=httpx.MockTransport(server.handler))
    await client.post("/x", {})
    client._tokens._expires_at = 0  # pretend the hour has passed
    await client.post("/x", {})
    await client.aclose()
    assert server.seen == ["Bearer first", "Bearer second"]


@pytest.mark.anyio
async def test_google_auth_metadata_failure_is_unavailable():
    client = ApiClient(
        "http://api", auth="google", transport=httpx.MockTransport(outside_cloud_run)
    )
    with pytest.raises(api_client.ApiUnavailableError, match="404"):
        await client.post("/x", {})
    await client.aclose()


@pytest.mark.anyio
async def test_no_auth_sends_no_authorization_header():
    client = ApiClient("http://api", transport=httpx.MockTransport(without_authorization))
    assert await client.post("/x", {}) == (400, {"success": False, "error": "nope"})
    await client.aclose()
