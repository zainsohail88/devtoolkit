from __future__ import annotations

import base64
import hashlib
import io
import re
import zipfile
from html.parser import HTMLParser

import httpx
import pytest
from fastapi.testclient import TestClient

from main import (
    EXAMPLE_COMPOSE,
    HTML_CACHE,
    MAX_BODY_BYTES,
    MAX_INPUT_CHARS,
    SECURITY_HEADERS,
    STATIC_CACHE,
    create_app,
    static_url,
)

COMPOSE = 'services:\n  web:\n    image: nginx\n    ports:\n      - "8080:80"\n'
FILES = [
    {"path": "web-deployment.yaml", "content": "apiVersion: apps/v1\nkind: Deployment\n"},
    {"path": "web-service.yaml", "content": "apiVersion: v1\nkind: Service\n"},
]
FRAGMENT = {"X-Requested-With": "fetch"}


class _InlineScripts(HTMLParser):
    """Collects the text of every <script> element without a src attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self._inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("src") is None:
            self._inside = True
            self.scripts.append("")

    def handle_data(self, data: str) -> None:
        if self._inside:
            self.scripts[-1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inside = False


def inline_scripts(html: str) -> list[str]:
    parser = _InlineScripts()
    parser.feed(html)
    return parser.scripts


def ok_files(fake_api, files=FILES, notes=()):
    fake_api.body = {"success": True, "output": "manifests", "files": files, "notes": list(notes)}


# --- basics ------------------------------------------------------------------------------


def test_probes(client):
    assert client.get("/health").json() == {"status": "healthy"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_index_names_the_product_and_lists_the_tools(client):
    html = client.get("/").text
    assert "<h1" in html and ">DevToolkit</h1>" in html
    assert "Converters for infrastructure configs. Open source, nothing stored." in html
    assert 'href="/compose-to-k8s" class="card' in html
    assert 'href="/validate-yaml" class="card' in html
    assert html.count("<svg") >= 6  # icons are inlined, not fetched
    assert "DevOps conversion tools" not in html
    assert "Paste a file" not in html


def test_no_api_docs_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_security_headers_on_every_response(client):
    for path in ("/", "/compose-to-k8s", "/health", "/static/app.js", "/nope"):
        headers = client.get(path).headers
        for name, value in SECURITY_HEADERS.items():
            assert headers[name] == value, (path, name)


def test_inline_theme_script_is_allowed_by_csp(client):
    """The CSP has no 'unsafe-inline'; the one inline script must match the hash exactly."""
    html = client.get("/").text
    scripts = inline_scripts(html)
    assert len(scripts) == 1
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    assert f"'sha256-{digest}'" in script_src
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
    assert 'localStorage.getItem("theme")' in scripts[0]


def test_cache_headers(client):
    assert client.get("/").headers["Cache-Control"] == HTML_CACHE
    assert client.get("/nope").headers["Cache-Control"] == HTML_CACHE
    assert client.get("/static/app.js").headers["Cache-Control"] == STATIC_CACHE
    assert "Cache-Control" not in client.get("/health").headers


def test_static_urls_are_versioned_by_content(client):
    html = client.get("/").text
    assert re.search(r'src="/static/app\.js\?v=[0-9a-f]{8}"', html)
    # Built files (app.css, vendor/editor.js) may be absent in tests; the page still works.
    assert re.search(r'href="/static/app\.css\?v=([0-9a-f]{8}|0)"', html)
    assert re.search(r'src="/static/vendor/editor\.js\?v=([0-9a-f]{8}|0)"', html)
    assert static_url("does-not-exist.css") == "/static/does-not-exist.css?v=0"


def test_page_structure_and_theme_toggle(client):
    html = client.get("/compose-to-k8s").text
    assert '<html lang="en">' in html
    assert 'href="#main"' in html and 'id="main"' in html  # skip link and its target
    assert html.count("<h1") == 1
    assert "<nav" in html and "<main" in html and "<footer" in html
    assert "data-theme-toggle hidden" in html  # shown only once app.js has wired it up
    assert 'href="/compose-to-k8s" class="nav-link" aria-current="page"' in html
    assert 'href="/validate-yaml" class="nav-link">' in html


def test_404_page_has_a_way_back(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "There is no page at this address." in response.text
    assert 'href="/"' in response.text


def test_405_page(client):
    response = client.post("/", content=b"", headers={"Content-Length": "0"})
    assert response.status_code == 405
    assert "isn&#39;t allowed here" in response.text


def test_malformed_form_gets_an_html_error(client, fake_api):
    response = client.post(
        "/compose-to-k8s", data={"compose_yaml": COMPOSE, "include_service": "maybe"}
    )
    assert response.status_code == 400
    assert "wasn&#39;t filled in correctly" in response.text
    assert fake_api.requests == []


# --- compose -> kubernetes ---------------------------------------------------------------


def test_compose_form_renders_empty_state(client):
    html = client.get("/compose-to-k8s").text
    assert 'name="compose_yaml"' in html
    assert 'value="default"' in html
    assert 'name="output" value="manifests" checked' in html
    assert "data-empty" in html  # nothing converted yet
    assert "data-zip hidden" in html  # the zip button waits for files
    assert "data-load-example" in html


def test_example_fills_the_form_or_returns_plain_text(client):
    html = client.get("/compose-to-k8s?example=1").text
    assert "image: nginx:1.27" in html and 'value="shop"' in html
    text = client.get("/compose-to-k8s?example=1", headers={"Accept": "text/plain"}).text
    assert text == EXAMPLE_COMPOSE


def test_compose_convert_renders_files_tabs_and_notes(client, fake_api):
    ok_files(fake_api, notes=["'web' had no resource limits: placeholder requests were added"])
    response = client.post(
        "/compose-to-k8s",
        data={"compose_yaml": COMPOSE, "namespace": "shop", "include_service": "true"},
    )
    assert response.status_code == 200
    assert fake_api.requests == [
        (
            "/convert/compose-to-k8s/files",
            {
                "compose_yaml": COMPOSE,
                "namespace": "shop",
                "include_service": True,
                "output": "manifests",
            },
        )
    ]
    html = response.text
    assert "<html" in html  # a plain post gets the whole page
    assert 'data-file-tab="0"' in html and 'data-file-tab="1"' in html
    assert "web-deployment.yaml" in html and "web-service.yaml" in html
    assert "kind: Deployment" in html and "kind: Service" in html
    assert "placeholder requests were added" in html
    assert "Needs a decision (1)" in html
    assert "data-zip>" in html  # the zip button is shown once there are files
    # The form keeps what the user typed.
    assert 'value="shop"' in html
    assert "image: nginx" in html


def test_compose_fragment_for_the_page_script(client, fake_api):
    ok_files(fake_api)
    response = client.post("/compose-to-k8s", data={"compose_yaml": COMPOSE}, headers=FRAGMENT)
    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'data-file-panel="0"' in response.text
    assert "<h3" not in response.text.split("data-files")[0]  # no notes section before files


def test_compose_helm_output_and_services_off(client, fake_api):
    ok_files(fake_api, files=[{"path": "Chart.yaml", "content": "apiVersion: v2\n"}])
    fake_api.body["output"] = "helm"
    response = client.post(
        "/compose-to-k8s", data={"compose_yaml": COMPOSE, "output": "helm"}, headers=FRAGMENT
    )
    assert response.status_code == 200
    sent = fake_api.requests[0][1]
    assert sent["output"] == "helm" and sent["include_service"] is False
    assert "Chart.yaml" in response.text


def test_compose_convert_from_upload(client, fake_api):
    ok_files(fake_api)
    response = client.post(
        "/compose-to-k8s",
        data={"compose_yaml": ""},
        files={"compose_file": ("docker-compose.yaml", COMPOSE.encode(), "text/yaml")},
    )
    assert response.status_code == 200
    assert fake_api.requests[0][1]["compose_yaml"] == COMPOSE


def test_upload_wins_over_pasted_text_and_strips_bom(client, fake_api):
    ok_files(fake_api)
    client.post(
        "/compose-to-k8s",
        data={"compose_yaml": "services: {}"},
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


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"compose_yaml": "   \n"}, "Paste some YAML"),
        ({"compose_yaml": "a" * (MAX_INPUT_CHARS + 1)}, "longer than"),
        ({"compose_yaml": COMPOSE, "namespace": "Bad_NS"}, "Namespace must be"),
        ({"compose_yaml": COMPOSE, "output": "kustomize"}, "Output must be"),
    ],
)
def test_input_rejected_before_calling_api(client, fake_api, data, message):
    response = client.post("/compose-to-k8s", data=data)
    assert response.status_code == 400
    assert message in response.text
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


def test_compose_rejected_by_api_with_position(client, fake_api):
    fake_api.status = 400
    fake_api.body = {"success": False, "error": "Invalid YAML: bad indent", "line": 3, "column": 2}
    response = client.post("/compose-to-k8s", data={"compose_yaml": "a:\n b: [1"}, headers=FRAGMENT)
    assert response.status_code == 400
    assert 'data-error-line="3" data-error-column="2"' in response.text
    assert "Invalid YAML: bad indent" in response.text and "(line 3, column 2)" in response.text


def test_compose_rejected_by_api_without_position(client, fake_api):
    fake_api.status = 400
    fake_api.body = {"success": False, "error": "No services defined"}
    response = client.post("/compose-to-k8s", data={"compose_yaml": "version: '3'"})
    assert response.status_code == 400
    assert "No services defined" in response.text
    assert "data-error-line" not in response.text


def test_compose_unexpected_api_answer(client, fake_api):
    fake_api.status = 422
    fake_api.body = {"detail": [{"msg": "..."}]}
    response = client.post("/compose-to-k8s", data={"compose_yaml": COMPOSE})
    assert response.status_code == 400
    assert "couldn&#39;t process this input" in response.text


def test_compose_success_without_files_list_is_an_error(client, fake_api):
    fake_api.body = {"success": True, "files": "not-a-list"}
    response = client.post("/compose-to-k8s", data={"compose_yaml": COMPOSE})
    assert response.status_code == 400


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
    ok_files(
        fake_api,
        files=[{"path": "x.yaml", "content": "<script>alert(1)</script>"}],
        notes=["<b>x</b>"],
    )
    response = client.post("/compose-to-k8s", data={"compose_yaml": "<img src=x onerror=1>"})
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "<img src=x" not in response.text
    assert "<b>x</b>" not in response.text
    # Still exactly one inline script on the page: the theme one.
    scripts = inline_scripts(response.text)
    assert len(scripts) == 1
    assert "alert(1)" not in scripts[0]


def test_namespace_pattern_is_valid_under_the_browser_v_flag(client):
    """Browsers compile the pattern attribute with the regex v flag, where a hyphen inside a
    character class must be escaped. An unescaped one makes the browser drop the pattern."""
    html = client.get("/compose-to-k8s").text
    assert 'pattern="[a-z0-9]([a-z0-9\\-]{0,61}[a-z0-9])?"' in html


# --- zip download --------------------------------------------------------------------------


def test_zip_download_contains_every_file(client, fake_api):
    ok_files(fake_api)
    response = client.post("/compose-to-k8s/zip", data={"compose_yaml": COMPOSE})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="kubernetes-manifests.zip"' in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["web-deployment.yaml", "web-service.yaml"]
        assert archive.read("web-service.yaml") == b"apiVersion: v1\nkind: Service\n"


def test_zip_download_for_helm_is_named_after_the_chart(client, fake_api):
    ok_files(fake_api, files=[{"path": "Chart.yaml", "content": "apiVersion: v2\n"}])
    response = client.post("/compose-to-k8s/zip", data={"compose_yaml": COMPOSE, "output": "helm"})
    assert 'filename="helm-chart.zip"' in response.headers["content-disposition"]


def test_zip_download_shows_the_page_with_the_error_when_conversion_fails(client, fake_api):
    fake_api.status = 400
    fake_api.body = {"success": False, "error": "No services defined"}
    response = client.post("/compose-to-k8s/zip", data={"compose_yaml": "version: '3'"})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "No services defined" in response.text


# --- yaml check ----------------------------------------------------------------------------


def test_validate_form_renders(client):
    html = client.get("/validate-yaml").text
    assert 'name="yaml_text"' in html
    assert "YAML check" in html


def test_validate_valid(client, fake_api):
    fake_api.body = {"valid": True, "documents": 2}
    response = client.post("/validate-yaml", data={"yaml_text": "a: 1\n---\nb: 2\n"})
    assert response.status_code == 200
    assert fake_api.requests == [("/validate/yaml", {"yaml": "a: 1\n---\nb: 2\n"})]
    assert "Valid YAML: 2 documents." in response.text
    assert "<html" in response.text


def test_validate_fragment_single_document(client, fake_api):
    fake_api.body = {"valid": True, "documents": 1}
    response = client.post("/validate-yaml", data={"yaml_text": "a: 1"}, headers=FRAGMENT)
    assert "Valid YAML: 1 document." in response.text
    assert "<html" not in response.text


def test_validate_invalid_with_position_marks_the_line(client, fake_api):
    fake_api.body = {
        "valid": False,
        "error": "Invalid YAML: mapping values are not allowed here",
        "line": 2,
        "column": 4,
    }
    response = client.post("/validate-yaml", data={"yaml_text": "a: 1\n  b: : 2"})
    assert response.status_code == 200
    assert 'data-error-line="2" data-error-column="4"' in response.text
    assert "(line 2, column 4)" in response.text


def test_validate_invalid_without_position(client, fake_api):
    fake_api.body = {"valid": False, "error": "Input nests too deeply"}
    response = client.post("/validate-yaml", data={"yaml_text": "a: 1"})
    assert "Input nests too deeply" in response.text
    assert "data-error-line" not in response.text


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
    response = client.post("/validate-yaml", data={"yaml_text": ""}, headers=FRAGMENT)
    assert response.status_code == 400
    assert "Paste some YAML" in response.text
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


# --- logging -------------------------------------------------------------------------------


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


def test_other_http_errors_render_the_error_page(fake_api):
    from fastapi import HTTPException

    from api_client import ApiClient

    api = ApiClient("http://converter-api", transport=httpx.MockTransport(fake_api.handler))
    app = create_app(api)

    @app.get("/boom")
    def boom() -> None:
        raise HTTPException(503, "Down for maintenance")

    with TestClient(app) as client:
        response = client.get("/boom")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert "Down for maintenance" in response.text
    assert 'href="/"' in response.text
