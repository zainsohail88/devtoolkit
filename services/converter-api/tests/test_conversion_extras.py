"""Probes, volumes, init containers, placeholder resources, namespaces and rendering."""

from __future__ import annotations

import pytest
import yaml

from converters.compose_to_k8s import (
    DEFAULT_LIMITS,
    DEFAULT_REQUESTS,
    DEFAULT_VOLUME_SIZE,
    WAIT_IMAGE,
    ComposeError,
    convert_compose_to_k8s,
    convert_depends_on,
    convert_healthcheck,
    convert_volumes,
    manifests_to_yaml,
    parse_compose,
    parse_duration,
)
from converters.render import chart_name_from, render_files


def convert(text: str, **kwargs):
    return convert_compose_to_k8s(parse_compose(text), **kwargs)


def container_of(result, name="web"):
    deployment = next(
        d for d in result["manifests"]["deployments"] if d["metadata"]["name"] == name
    )
    return deployment["spec"]["template"]["spec"]


# --- durations ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("10s", 10), ("1m30s", 90), ("500ms", 1), ("2h", 7200), ("1.5s", 2), (5, 5), ("3", 3)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value, "interval", "web") == seconds


@pytest.mark.parametrize("value", ["", "soon", "10x", "-5s", True, 0, "inf"])
def test_parse_duration_invalid(value):
    with pytest.raises(ComposeError, match="invalid duration"):
        parse_duration(value, "interval", "web")


# --- healthcheck -> probes ---------------------------------------------------------------


def test_healthcheck_cmd_becomes_probes():
    result = convert(
        "services:\n  web:\n    image: nginx\n    healthcheck:\n"
        '      test: ["CMD", "curl", "-f", "http://localhost/"]\n'
        "      interval: 30s\n      timeout: 3s\n      retries: 5\n      start_period: 1m\n"
    )
    c = container_of(result)["containers"][0]
    assert c["livenessProbe"] == {
        "exec": {"command": ["curl", "-f", "http://localhost/"]},
        "periodSeconds": 30,
        "timeoutSeconds": 3,
        "failureThreshold": 5,
        "initialDelaySeconds": 60,
    }
    assert c["readinessProbe"] == c["livenessProbe"]
    assert c["readinessProbe"] is not c["livenessProbe"]
    # No anchors in the YAML: the two probes are independent documents.
    assert "&id" not in manifests_to_yaml(result["manifests"])
    assert not any("healthcheck" in n for n in result["notes"])


@pytest.mark.parametrize(
    ("test", "command"),
    [
        ('["CMD-SHELL", "pg_isready -U app"]', ["sh", "-c", "pg_isready -U app"]),
        (
            '"curl -f http://localhost || exit 1"',
            ["sh", "-c", "curl -f http://localhost || exit 1"],
        ),
        ('["pg_isready"]', ["pg_isready"]),
    ],
)
def test_healthcheck_test_forms(test, command):
    result = convert(f"services:\n  web:\n    image: x\n    healthcheck:\n      test: {test}\n")
    assert container_of(result)["containers"][0]["livenessProbe"] == {"exec": {"command": command}}


@pytest.mark.parametrize(
    "healthcheck",
    ['{test: ["NONE"]}', "{disable: true, test: [CMD, true]}", "{interval: 5s}", "{test: []}"],
)
def test_healthcheck_without_probe(healthcheck):
    result = convert(f"services:\n  web:\n    image: x\n    healthcheck: {healthcheck}\n")
    assert "livenessProbe" not in container_of(result)["containers"][0]


@pytest.mark.parametrize("retries", ["many", 0, True, -1])
def test_healthcheck_invalid_retries(retries):
    with pytest.raises(ComposeError, match="retries"):
        convert_healthcheck("web", {"healthcheck": {"test": ["CMD", "true"], "retries": retries}})


def test_healthcheck_must_be_mapping():
    with pytest.raises(ComposeError, match="must be a mapping"):
        convert_healthcheck("web", {"healthcheck": ["CMD", "true"]})


# --- volumes -----------------------------------------------------------------------------


def test_named_volume_becomes_pvc_and_mount():
    result = convert(
        "services:\n  db:\n    image: postgres\n    volumes:\n"
        "      - dbdata:/var/lib/postgresql/data\nvolumes:\n  dbdata: {}\n"
    )
    pod = container_of(result, "db")
    assert pod["volumes"] == [{"name": "dbdata", "persistentVolumeClaim": {"claimName": "dbdata"}}]
    assert pod["containers"][0]["volumeMounts"] == [
        {"name": "dbdata", "mountPath": "/var/lib/postgresql/data"}
    ]
    pvcs = result["manifests"]["persistent_volume_claims"]
    assert len(pvcs) == 1
    assert pvcs[0]["kind"] == "PersistentVolumeClaim"
    assert pvcs[0]["metadata"] == {"name": "dbdata", "namespace": "default"}
    assert pvcs[0]["spec"]["resources"]["requests"]["storage"] == DEFAULT_VOLUME_SIZE
    assert any("placeholder size" in n for n in result["notes"])


def test_shared_named_volume_is_one_pvc_with_a_warning():
    result = convert(
        "services:\n  a:\n    image: x\n    volumes: [shared:/data]\n"
        "  b:\n    image: x\n    volumes: [shared:/data:ro]\n"
    )
    assert len(result["manifests"]["persistent_volume_claims"]) == 1
    assert container_of(result, "b")["containers"][0]["volumeMounts"][0]["readOnly"] is True
    assert any("mounted by more than one service" in n for n in result["notes"])


def test_bind_mount_is_a_note_not_a_mount():
    result = convert(
        "services:\n  web:\n    image: x\n    volumes:\n"
        "      - ./site:/srv:ro\n      - /host/logs:/logs\n"
    )
    pod = container_of(result)
    assert "volumes" not in pod
    assert "volumeMounts" not in pod["containers"][0]
    assert sum("bind mount" in n for n in result["notes"]) == 2
    assert result["manifests"]["persistent_volume_claims"] == []


def test_anonymous_and_tmpfs_volumes_become_emptydir():
    result = convert(
        "services:\n  web:\n    image: x\n    volumes:\n      - /scratch\n"
        "      - type: tmpfs\n        target: /run\n"
    )
    pod = container_of(result)
    assert pod["volumes"] == [
        {"name": "scratch", "emptyDir": {}},
        {"name": "run", "emptyDir": {"medium": "Memory"}},
    ]
    mounts = [m["mountPath"] for m in pod["containers"][0]["volumeMounts"]]
    assert mounts == ["/scratch", "/run"]


def test_long_syntax_volume_and_external_volume():
    result = convert(
        "services:\n  web:\n    image: x\n    volumes:\n"
        "      - type: volume\n        source: media\n        target: /media\n"
        "        read_only: true\n"
        "      - type: volume\n        source: legacy\n        target: /legacy\n"
        "volumes:\n  media: {}\n  legacy:\n    external: true\n    name: legacy-claim\n"
    )
    pod = container_of(result)
    assert [v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"]] == [
        "media",
        "legacy-claim",
    ]
    assert pod["containers"][0]["volumeMounts"][0]["readOnly"] is True
    # Only the non-external volume gets a PVC.
    assert [p["metadata"]["name"] for p in result["manifests"]["persistent_volume_claims"]] == [
        "media"
    ]
    assert any("must already exist" in n for n in result["notes"])


def test_volume_errors():
    with pytest.raises(ComposeError, match="missing 'target'"):
        convert_volumes("web", {"volumes": [{"type": "volume", "source": "x"}]}, {})
    with pytest.raises(ComposeError, match="invalid volume entry"):
        convert_volumes("web", {"volumes": ["a:b:c:d"]}, {})
    with pytest.raises(ComposeError, match="absolute path"):
        convert_volumes("web", {"volumes": ["data:relative/path"]}, {})
    with pytest.raises(ComposeError, match="top-level 'volumes' must be a mapping"):
        convert("services:\n  web:\n    image: x\nvolumes: [a, b]\n")


def test_unsupported_volume_type_is_skipped_with_note():
    volumes, mounts, claims, notes = convert_volumes(
        "web", {"volumes": [{"type": "npipe", "source": "x", "target": "/x"}]}, {}
    )
    assert volumes == [] and mounts == [] and claims == []
    assert "not supported" in notes[0]


# --- depends_on -> init containers -------------------------------------------------------


def test_depends_on_waits_for_dependency_port():
    result = convert(
        "services:\n  web:\n    image: x\n    depends_on: [db]\n"
        "  db:\n    image: postgres\n    ports: ['5432']\n"
    )
    assert container_of(result)["initContainers"] == [
        {
            "name": "wait-for-db",
            "image": WAIT_IMAGE,
            "command": ["sh", "-c", "until nc -z db 5432; do sleep 2; done"],
        }
    ]
    assert not any("depends_on" in n for n in result["notes"])


def test_depends_on_mapping_form_and_renamed_dependency():
    result = convert(
        "services:\n  web:\n    image: x\n    depends_on:\n      My_DB:\n"
        "        condition: service_healthy\n"
        "  My_DB:\n    image: postgres\n    expose: ['5432']\n"
    )
    init = container_of(result)["initContainers"][0]
    assert init["name"] == "wait-for-my-db"
    assert "nc -z my-db 5432" in init["command"][2]


def test_depends_on_without_ports_is_a_note():
    result = convert(
        "services:\n  web:\n    image: x\n    depends_on: [worker]\n  worker:\n    image: x\n"
    )
    assert "initContainers" not in container_of(result)
    assert any("has no ports" in n for n in result["notes"])


def test_depends_on_without_services_is_a_note():
    result = convert(
        "services:\n  web:\n    image: x\n    depends_on: [db]\n"
        "  db:\n    image: x\n    ports: ['5432']\n",
        include_service=False,
    )
    assert "initContainers" not in container_of(result)
    assert any("Services were disabled" in n for n in result["notes"])


def test_depends_on_errors():
    with pytest.raises(ComposeError, match="unknown service 'db'"):
        convert("services:\n  web:\n    image: x\n    depends_on: [db]\n")
    with pytest.raises(ComposeError, match="must be a list or a mapping"):
        convert_depends_on("web", {"depends_on": "db"}, {"db": {}}, True)


# --- resources ---------------------------------------------------------------------------


def test_placeholder_resources_when_none_given():
    result = convert("services:\n  web:\n    image: x\n")
    assert container_of(result)["containers"][0]["resources"] == {
        "requests": DEFAULT_REQUESTS,
        "limits": DEFAULT_LIMITS,
    }
    assert any("placeholder requests" in n for n in result["notes"])


def test_reservations_without_limits_become_guaranteed():
    result = convert(
        "services:\n  web:\n    image: x\n    deploy:\n      resources:\n"
        "        reservations: {cpus: '0.25', memory: 64M}\n"
    )
    resources = container_of(result)["containers"][0]["resources"]
    assert resources["requests"] == resources["limits"] == {"cpu": "250m", "memory": "64Mi"}
    assert any("limits set equal to the reservations" in n for n in result["notes"])


LIMITS_ONLY = (
    "services:\n  web:\n    image: x\n    deploy:\n      resources:\n        limits: {memory: 1g}\n"
)


def test_given_limits_are_kept_without_a_note():
    result = convert(LIMITS_ONLY)
    assert container_of(result)["containers"][0]["resources"] == {"limits": {"memory": "1Gi"}}
    assert not any("resource" in n for n in result["notes"])


# --- namespace and ordering --------------------------------------------------------------


def test_namespace_manifest_only_when_not_default():
    assert convert("services:\n  web:\n    image: x\n")["manifests"]["namespaces"] == []
    result = convert("services:\n  web:\n    image: x\n", namespace="shop")
    assert result["manifests"]["namespaces"] == [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "shop"}}
    ]
    kinds = [d["kind"] for d in yaml.safe_load_all(manifests_to_yaml(result["manifests"]))]
    assert kinds == ["Namespace", "Deployment"]


# --- rendering ---------------------------------------------------------------------------

COMPOSE = (
    "name: My Shop\nservices:\n  web:\n    image: nginx:1.27\n    ports: ['8080:80']\n"
    "    depends_on: [db]\n  db:\n    image: postgres:16\n    ports: ['5432']\n"
    "    volumes: [dbdata:/var/lib/postgresql/data]\nvolumes:\n  dbdata: {}\n"
)


def test_render_manifests_one_file_per_resource_in_apply_order():
    files = render_files(convert(COMPOSE, namespace="shop"))
    assert [f["path"] for f in files] == [
        "shop-namespace.yaml",
        "dbdata-pvc.yaml",
        "web-deployment.yaml",
        "db-deployment.yaml",
        "web-service.yaml",
        "db-service.yaml",
    ]
    for f in files:
        doc = yaml.safe_load(f["content"])
        assert doc["metadata"]["name"] == f["path"].rsplit("-", 1)[0]


def test_render_manifests_is_proportional():
    files = render_files(convert("services:\n  job:\n    image: x\n"))
    assert [f["path"] for f in files] == ["job-deployment.yaml"]


def test_chart_name():
    assert chart_name_from({"name": "My Shop!"}) == "my-shop"
    assert chart_name_from({}) == "app"
    assert chart_name_from({"name": "---"}) == "app"
    assert chart_name_from("not a mapping") == "app"


def test_render_helm_chart():
    compose = parse_compose(COMPOSE)
    result = convert_compose_to_k8s(compose, namespace="shop")
    files = {
        f["path"]: f["content"] for f in render_files(result, "helm", chart_name_from(compose))
    }
    assert list(files) == [
        "Chart.yaml",
        "values.yaml",
        "templates/web-deployment.yaml",
        "templates/db-deployment.yaml",
        "templates/dbdata-pvc.yaml",
        "templates/web-service.yaml",
        "templates/db-service.yaml",
    ]
    chart = yaml.safe_load(files["Chart.yaml"])
    assert chart["apiVersion"] == "v2" and chart["name"] == "my-shop"
    values = yaml.safe_load(files["values.yaml"])
    assert values["services"]["web"]["image"] == "nginx:1.27"
    assert values["services"]["db"]["replicas"] == 1
    assert "resources" in values["services"]["db"]
    assert values["volumes"] == {"dbdata": {"size": "1Gi"}}

    web = files["templates/web-deployment.yaml"]
    assert 'image: {{ (index .Values.services "web").image | quote }}' in web
    assert 'replicas: {{ (index .Values.services "web").replicas }}' in web
    assert '{{- toYaml (index .Values.services "web").resources | nindent 10 }}' in web
    assert "namespace:" not in web  # Helm supplies the release namespace
    assert "wait-for-db" in web  # init containers survive templating
    pvc = files["templates/dbdata-pvc.yaml"]
    assert 'storage: {{ (index .Values.volumes "dbdata").size | quote }}' in pvc
    assert "namespace:" not in files["templates/web-service.yaml"]


def test_render_helm_without_resources_block():
    # A service whose compose declared limits keeps them in values and the template.
    result = convert(LIMITS_ONLY)
    web = next(
        f for f in render_files(result, "helm") if f["path"] == "templates/web-deployment.yaml"
    )
    assert "toYaml" in web["content"]


def test_render_unknown_output():
    with pytest.raises(ComposeError, match="Unknown output"):
        render_files(convert("services:\n  web:\n    image: x\n"), "kustomize")


def test_healthcheck_cmd_with_no_command_has_no_probe():
    assert convert_healthcheck("web", {"healthcheck": {"test": ["CMD"]}}) is None


def test_same_volume_mounted_twice_is_one_pod_volume():
    result = convert(
        "services:\n  web:\n    image: x\n    volumes:\n      - data:/a\n      - data:/b:ro\n"
    )
    pod = container_of(result)
    assert len(pod["volumes"]) == 1
    assert [m["mountPath"] for m in pod["containers"][0]["volumeMounts"]] == ["/a", "/b"]
    assert len(result["manifests"]["persistent_volume_claims"]) == 1
