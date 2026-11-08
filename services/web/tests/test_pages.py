from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from main import MAX_BODY_BYTES, MAX_INPUT_CHARS, SECURITY_HEADERS, create_app

COMPOSE = 'services:\n  web:\n    image: nginx\n    ports:\n      - "8080:80"\n'
K8S_YAML = "apiVersion: apps/v1\nkind: Deployment\n"


def test_probes(client):
    assert client.get("/health").json() == {"status": "healthy"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_index_links_to_tools(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/compose-to-k8s"' in response.text
    assert 'href="/validate-yaml"' in response.text


def test_no_api_docs_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_security_headers_on_every_response(client):
    for path in ("/", "/compose-to-k8s", "/health", "/static/app.js"):
        headers = client.get(path).headers
        for name, value in SECURITY_HEADERS.items():
            assert headers[name] == value, (path, name)


def test_compose_form_renders(client):
    response = client.get("/compose-to-k8s")
    assert response.status_code == 200
    assert 'name="compose_yaml"' in response.text
    assert 'value="default"' in response.text
    assert "checked" in response.text


def test_compose_convert_success(client, fake_api):
    fake_api.body = {"success": True, "yaml": K8S_YAML, "notes": ["'web' has no resource limits"]}
    response = client.post(
        "/compose-to-k8s",
        data={"compose_yaml": COMPOSE, "namespace": "shop", "include_service": "true"},
    )
    assert response.status_code == 200
    assert fake_api.requests == [
        (
            "/convert/compose-to-k8s/yaml",
            {"compose_yaml": COMPOSE, "namespace": "shop", "include_service": True},
        )
    ]
    assert "kind: Deployment" in response.text
    assert "has no resource limits" in response.text
    # The form keeps what the user typed.
    assert 'value="shop"' in response.text
    assert "image: nginx" in response.text


def test_compose_convert_without_services(client, fake_api):
    fake_api.body = {"success": True, "yaml": K8S_YAML, "notes": []}
    response = client.post("/compose-to-k8s", data={"compose_yaml": COMPOSE})
    assert response.status_code == 200
    assert fake_api.requests[0][1]["include_service"] is False
    assert "<h3" not in response.text  # no Notes heading when there are none


def test_compose_convert_from_upload(client, fake_api):
    fake_api.body = {"success": True, "yaml": K8S_YAML, "notes": []}
    response = client.post(
        "/compose-to-k8s",
        data={"compose_yaml": ""},
        files={"compose_file": ("docker-compose.yaml", COMPOSE.encode(), "text/yaml")},
    )
    assert response.status_code == 200
    assert fake_api.requests[0][1]["compose_yaml"] == COMPOSE


def test_upload_wins_over_pasted_text(client, fake_api):
    fake_api.body = {"success": True, "yaml": K8S_YAML, "notes": []}
    client.post(
        "/compose-to-k8s",
        data={"compose_yaml": "services: {}"},
        files={"compose_file": ("x.yaml", COMPOSE.encode(), "text/yaml")},
    )
    assert fake_api.requests[0][1]["compose_yaml"] == COMPOSE


def test_upload_strips_utf8_bom(client, fake_api):
    fake_api.body = {"success": True, "yaml": K8S_YAML, "notes": []}
    client.post(
        "/compose-to-k8s",
        data={"compose_yaml": ""},
        files={"compose_file": ("x.yaml", b"\xef\xbb\xbf" + COMPOSE.encode(), "text/yaml")},
    )
    assert fake_api.requests[0][1]["compose_yaml"] == COMPOSE


def test_upload_not_utf8(client, fake_api):
    response = client.post(
        "/compose-to-k8s",
        data={"compose_yaml": ""},
        files={"compose_file": ("x.yaml", b"\xff\xfe\x00bad", "text/yaml")},
    )
    assert response.status_code == 400
    assert "isn&#39;t UTF-8" in response.text
    assert fake_api.requests == []


def test_empty_input(client, fake_api):
    response = client.post("/compose-to-k8s", data={"compose_yaml": "   \n"})
    assert response.status_code == 400
    assert "Paste some YAML" in response.text
    assert fake_api.requests == []


def test_input_too_long_is_rejected_before_calling_api(client, fake_api):
    response = client.post("/compose-to-k8s", data={"compose_yaml": "a" * (MAX_INPUT_CHARS + 1)})
    assert response.status_code == 400
    assert "longer than" in response.text
    assert fake_api.requests == []


def test_body_too_large(client, fake_api):
    response = client.post(
        "/compose-to-k8s",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413
    assert fake_api.requests == []


def test_post_without_content_length(client):
    def body():
        yield b"compose_yaml=x"

    response = client.post("/compose-to-k8s", content=body())
    assert response.status_code == 411


def test_bad_namespace(client, fake_api):
    response = client.post("/compose-to-k8s", data={"compose_yaml": COMPOSE, "namespace": "Bad_NS"})
    assert response.status_code == 400
    assert "Namespace must be" in response.text
    assert 'value="Bad_NS"' in response.text
    assert fake_api.requests == []


def test_compose_rejected_by_api(client, fake_api):
    fake_api.status = 400
    fake_api.body = {"success": False, "error": "No services defined"}
    response = client.post("/compose-to-k8s", data={"compose_yaml": "version: '3'"})
    assert response.status_code == 400
    assert "No services defined" in response.text


def test_compose_unexpected_api_answer(client, fake_api):
    fake_api.status = 422
    fake_api.body = {"detail": [{"msg": "..."}]}
    response = client.post("/compose-to-k8s", data={"compose_yaml": COMPOSE})
    assert response.status_code == 400
    assert "couldn&#39;t process this input" in response.text


@pytest.mark.parametrize("status", [500, 503, 401, 403])
def test_compose_api_error_status(client, fake_api, status):
    fake_api.status = status
    fake_api.body = {"error": "boom"}
    response = client.post("/compose-to-k8s", data={"compose_yaml": COMPOSE})
    assert response.status_code == 502
    assert "unavailable right now" in response.text
    assert "boom" not in response.text


def test_compose_api_non_json(client, fake_api):
    fake_api.text = "<html>502 Bad Gateway</html>"
    fake_api.status = 200
    response = client.post("/compose-to-k8s", data={"compose_yaml": COMPOSE})
    assert response.status_code == 502


def test_compose_api_json_not_object(client, fake_api):
    fake_api.body = ["not", "an", "object"]
    response = client.post("/compose-to-k8s", data={"compose_yaml": COMPOSE})
    assert response.status_code == 502


def test_compose_api_unreachable():
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    from api_client import ApiClient

    api = ApiClient("http://converter-api", transport=httpx.MockTransport(unreachable))
    with TestClient(create_app(api)) as client:
        response = client.post("/compose-to-k8s", data={"compose_yaml": COMPOSE})
    assert response.status_code == 502
    assert "unavailable right now" in response.text


def test_output_is_escaped(client, fake_api):
    fake_api.body = {"success": True, "yaml": "<script>alert(1)</script>", "notes": ["<b>x</b>"]}
    response = client.post("/compose-to-k8s", data={"compose_yaml": "<img src=x onerror=1>"})
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "<img src=x" not in response.text


def test_validate_form_renders(client):
    response = client.get("/validate-yaml")
    assert response.status_code == 200
    assert 'name="yaml_text"' in response.text


def test_validate_valid(client, fake_api):
    fake_api.body = {"valid": True, "documents": 2}
    response = client.post("/validate-yaml", data={"yaml_text": "a: 1\n---\nb: 2\n"})
    assert response.status_code == 200
    assert fake_api.requests == [("/validate/yaml", {"yaml": "a: 1\n---\nb: 2\n"})]
    assert "Valid YAML: 2 documents." in response.text


def test_validate_single_document(client, fake_api):
    fake_api.body = {"valid": True, "documents": 1}
    response = client.post("/validate-yaml", data={"yaml_text": "a: 1"})
    assert "Valid YAML: 1 document." in response.text


def test_validate_invalid_with_position(client, fake_api):
    fake_api.body = {
        "valid": False,
        "error": "Invalid YAML: mapping values",
        "line": 2,
        "column": 4,
    }
    response = client.post("/validate-yaml", data={"yaml_text": "a: 1\n  b: : 2"})
    assert response.status_code == 200
    assert "Invalid YAML: mapping values (line 2, column 4)" in response.text


def test_validate_invalid_without_position(client, fake_api):
    fake_api.body = {"valid": False, "error": "Input nests too deeply"}
    response = client.post("/validate-yaml", data={"yaml_text": "a: 1"})
    assert "Input nests too deeply" in response.text
    assert "(line" not in response.text


def test_validate_from_upload(client, fake_api):
    fake_api.body = {"valid": True, "documents": 1}
    response = client.post(
        "/validate-yaml",
        data={"yaml_text": ""},
        files={"yaml_file": ("x.yaml", b"a: 1\n", "text/yaml")},
    )
    assert response.status_code == 200
    assert fake_api.requests[0][1] == {"yaml": "a: 1\n"}


def test_validate_empty_input(client, fake_api):
    response = client.post("/validate-yaml", data={"yaml_text": ""})
    assert response.status_code == 400
    assert fake_api.requests == []


def test_validate_unexpected_api_answer(client, fake_api):
    fake_api.status = 422
    fake_api.body = {"detail": "bad"}
    response = client.post("/validate-yaml", data={"yaml_text": "a: 1"})
    assert response.status_code == 400
    assert "couldn&#39;t process this input" in response.text


def test_validate_api_down(client, fake_api):
    fake_api.status = 503
    response = client.post("/validate-yaml", data={"yaml_text": "a: 1"})
    assert response.status_code == 502
    assert "unavailable right now" in response.text


def test_resolve_log_level():
    from main import resolve_log_level

    assert resolve_log_level(None) == ("INFO", None)
    assert resolve_log_level(" Debug ") == ("DEBUG", None)
    level, warning = resolve_log_level("loud")
    assert level == "INFO"
    assert "Unknown LOG_LEVEL 'loud'" in warning


def test_configure_logging_warns_on_unknown_level(caplog):
    from main import configure_logging

    assert configure_logging("warning") == "WARNING"
    with caplog.at_level("WARNING", logger="web"):
        assert configure_logging("loud") == "INFO"
    assert "Unknown LOG_LEVEL 'loud'" in caplog.text
