"""Test fixtures. converter-api is replaced by an in-memory fake, so these tests check the
web service alone: what it sends, and how it renders every kind of answer."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from api_client import ApiClient
from main import create_app


class FakeApi:
    """Records requests and returns scripted responses."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.status = 200
        self.body: Any = {}
        self.text: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.url.path, json.loads(request.content)))
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def fake_api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def client(fake_api: FakeApi) -> Iterator[TestClient]:
    api = ApiClient("http://converter-api", transport=httpx.MockTransport(fake_api.handler))
    with TestClient(create_app(api)) as test_client:
        yield test_client
