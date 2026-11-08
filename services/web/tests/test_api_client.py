from __future__ import annotations

import httpx
import pytest

import api_client
from api_client import METADATA_IDENTITY_URL, ApiClient


def test_rejects_unknown_auth_mode():
    with pytest.raises(ValueError, match="CONVERTER_API_AUTH"):
        ApiClient("http://api", auth="basic")


@pytest.mark.anyio
async def test_google_auth_fetches_id_token_once_and_sends_it(monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if str(request.url).startswith(METADATA_IDENTITY_URL):
            assert request.headers["Metadata-Flavor"] == "Google"
            assert request.url.params["audience"] == "https://converter-api.example"
            return httpx.Response(200, text="id-token-1\n")
        assert request.headers["Authorization"] == "Bearer id-token-1"
        return httpx.Response(200, json={"ok": True})

    client = ApiClient(
        "https://converter-api.example/", auth="google", transport=httpx.MockTransport(handler)
    )
    assert await client.post("/validate/yaml", {"yaml": "a: 1"}) == (200, {"ok": True})
    assert await client.post("/validate/yaml", {"yaml": "b: 2"}) == (200, {"ok": True})
    await client.aclose()
    # One token fetch, two API calls.
    assert [str(r.url).startswith(METADATA_IDENTITY_URL) for r in calls] == [True, False, False]


@pytest.mark.anyio
async def test_google_auth_refreshes_expired_token(monkeypatch):
    tokens = iter(["first", "second"])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(METADATA_IDENTITY_URL):
            return httpx.Response(200, text=next(tokens))
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    client = ApiClient("http://api", auth="google", transport=httpx.MockTransport(handler))
    await client.post("/x", {})
    client._tokens._expires_at = 0  # pretend the hour has passed
    await client.post("/x", {})
    await client.aclose()
    assert seen == ["Bearer first", "Bearer second"]


@pytest.mark.anyio
async def test_google_auth_metadata_failure_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not on Cloud Run")

    client = ApiClient("http://api", auth="google", transport=httpx.MockTransport(handler))
    with pytest.raises(api_client.ApiUnavailableError, match="404"):
        await client.post("/x", {})
    await client.aclose()


@pytest.mark.anyio
async def test_no_auth_sends_no_authorization_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(400, json={"success": False, "error": "nope"})

    client = ApiClient("http://api", transport=httpx.MockTransport(handler))
    assert await client.post("/x", {}) == (400, {"success": False, "error": "nope"})
    await client.aclose()
