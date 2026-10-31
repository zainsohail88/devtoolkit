import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from main import MAX_INPUT_CHARS, app, configure_logging, resolve_log_level

client = TestClient(app)

COMPOSE = "version: '3'\nservices:\n  web:\n    image: nginx\n    ports:\n      - '80:80'"


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_ready():
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_convert_json():
    response = client.post(
        "/convert/compose-to-k8s",
        json={"compose_yaml": COMPOSE, "namespace": "default", "include_service": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["manifests"]["deployments"][0]["metadata"]["name"] == "web"
    assert body["manifests"]["services"][0]["spec"]["ports"][0]["port"] == 80
    assert "'web' has no resource limits" in body["notes"]


def test_convert_yaml():
    response = client.post("/convert/compose-to-k8s/yaml", json={"compose_yaml": COMPOSE})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    kinds = [doc["kind"] for doc in yaml.safe_load_all(body["yaml"])]
    assert kinds == ["Deployment", "Service"]


def test_convert_invalid_yaml():
    for path in ("/convert/compose-to-k8s", "/convert/compose-to-k8s/yaml"):
        response = client.post(path, json={"compose_yaml": "services: [unclosed"})
        assert response.status_code == 400
        body = response.json()
        assert body["success"] is False
        assert body["error"].startswith("Invalid YAML")


def test_convert_rejects_bad_namespace():
    response = client.post(
        "/convert/compose-to-k8s", json={"compose_yaml": COMPOSE, "namespace": "Bad_Name"}
    )
    assert response.status_code == 422


def test_convert_rejects_oversized_input():
    response = client.post(
        "/convert/compose-to-k8s", json={"compose_yaml": "x" * (MAX_INPUT_CHARS + 1)}
    )
    assert response.status_code == 422


def test_validate_yaml_valid():
    response = client.post("/validate/yaml", json={"yaml": "a: 1\n---\nb: 2\n"})
    assert response.json() == {"valid": True, "documents": 2}


def test_validate_yaml_invalid():
    response = client.post("/validate/yaml", json={"yaml": "a: 1\n b: 2\n"})
    body = response.json()
    assert body["valid"] is False
    assert body["error"].startswith("Invalid YAML")
    assert body["line"] == 2


def test_deep_nesting_is_not_a_server_error():
    deep = "[" * 5000 + "]" * 5000
    response = client.post("/convert/compose-to-k8s", json={"compose_yaml": deep})
    assert response.status_code == 400
    response = client.post("/validate/yaml", json={"yaml": deep})
    assert response.status_code == 200
    assert response.json() == {"valid": False, "error": "Invalid YAML: nested too deeply"}


def test_validate_yaml_block_nesting():
    response = client.post("/validate/yaml", json={"yaml": "- " * 5000 + "x"})
    assert response.json()["valid"] is False


def test_validate_yaml_merge_key_bomb_is_fast():
    text = "a0: &a0 {k: v}\n"
    for i in range(1, 13):
        text += f"a{i}: &a{i} {{<<: [" + ", ".join([f"*a{i - 1}"] * 9) + "]}\n"
    start = time.perf_counter()
    response = client.post("/validate/yaml", json={"yaml": text})
    assert response.json() == {"valid": True, "documents": 1}
    assert time.perf_counter() - start < 1


def test_validate_yaml_too_many_values():
    response = client.post("/validate/yaml", json={"yaml": "[" + ",".join(["a"] * 20000) + "]"})
    assert response.json()["error"].startswith("Input too large")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "INFO"), ("debug", "DEBUG"), (" Warning ", "WARNING"), ("CRITICAL", "CRITICAL")],
)
def test_resolve_log_level_valid(value, expected):
    assert resolve_log_level(value) == (expected, None)


def test_resolve_log_level_unknown_falls_back_to_info():
    level, warning = resolve_log_level("verbose")
    assert level == "INFO"
    assert "Unknown LOG_LEVEL 'verbose'" in warning
    assert "debug, info, warning, error, critical" in warning


def test_unknown_log_level_does_not_crash_startup():
    # Importing main with a bad LOG_LEVEL used to raise "ValueError: Unknown level".
    env = {**os.environ, "LOG_LEVEL": "verbose"}
    service_dir = Path(__file__).resolve().parents[1]  # works from any working directory
    result = subprocess.run(
        [sys.executable, "-c", "import main"],
        env=env,
        cwd=service_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Unknown LOG_LEVEL 'verbose'" in result.stderr


def test_configure_logging_warns_on_unknown_level(caplog):
    with caplog.at_level(logging.WARNING, logger="converter-api"):
        assert configure_logging("loud") == "INFO"
    assert "Unknown LOG_LEVEL 'loud'" in caplog.text
