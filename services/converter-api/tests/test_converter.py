import time

import pytest
import yaml

from converters.compose_to_k8s import (
    ComposeError,
    convert_compose_to_k8s,
    convert_cpus,
    convert_memory,
    convert_service_to_deployment,
    convert_service_to_k8s_service,
    manifests_to_yaml,
    parse_compose,
    parse_environment,
    parse_ports,
    to_k8s_name,
)

BASIC_COMPOSE = """
version: '3'
services:
  web:
    image: nginx:1.27
    ports:
      - '8080:80'
"""


def container_of(deployment: dict) -> dict:
    return deployment["spec"]["template"]["spec"]["containers"][0]


# --- basic conversion ---------------------------------------------------------


def test_basic_service_conversion():
    result = convert_compose_to_k8s(parse_compose(BASIC_COMPOSE), namespace="shop")
    deployment = result["manifests"]["deployments"][0]

    assert deployment["apiVersion"] == "apps/v1"
    assert deployment["kind"] == "Deployment"
    assert deployment["metadata"] == {"name": "web", "namespace": "shop", "labels": {"app": "web"}}
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["selector"]["matchLabels"] == {"app": "web"}
    assert deployment["spec"]["template"]["metadata"]["labels"] == {"app": "web"}
    assert container_of(deployment)["image"] == "nginx:1.27"


def test_service_created_for_ports():
    result = convert_compose_to_k8s(parse_compose(BASIC_COMPOSE))
    service = result["manifests"]["services"][0]

    assert service["kind"] == "Service"
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == {"app": "web"}
    assert service["spec"]["ports"] == [
        {"name": "tcp-8080", "port": 8080, "targetPort": 80, "protocol": "TCP"}
    ]


def test_include_service_false_skips_services():
    result = convert_compose_to_k8s(parse_compose(BASIC_COMPOSE), include_service=False)
    assert result["manifests"]["services"] == []
    assert len(result["manifests"]["deployments"]) == 1


def test_no_service_without_ports():
    assert convert_service_to_k8s_service("worker", {"image": "busybox"}) is None


def test_multiple_services():
    compose = parse_compose(
        """
services:
  api:
    image: my/api
    ports: ["8000"]
  db:
    image: postgres:16
"""
    )
    result = convert_compose_to_k8s(compose)
    names = [d["metadata"]["name"] for d in result["manifests"]["deployments"]]
    assert names == ["api", "db"]
    assert [s["metadata"]["name"] for s in result["manifests"]["services"]] == ["api"]


# --- ports --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("80", {"container_port": 80, "service_port": 80, "protocol": "TCP"}),
        (80, {"container_port": 80, "service_port": 80, "protocol": "TCP"}),
        ("8080:80", {"container_port": 80, "service_port": 8080, "protocol": "TCP"}),
        ("127.0.0.1:8080:80", {"container_port": 80, "service_port": 8080, "protocol": "TCP"}),
        ("127.0.0.1::80", {"container_port": 80, "service_port": 80, "protocol": "TCP"}),
        ("53:53/udp", {"container_port": 53, "service_port": 53, "protocol": "UDP"}),
        (
            {"target": 80, "published": 8080, "protocol": "tcp"},
            {"container_port": 80, "service_port": 8080, "protocol": "TCP"},
        ),
        ({"target": 9000}, {"container_port": 9000, "service_port": 9000, "protocol": "TCP"}),
    ],
)
def test_ports_parsing(entry, expected):
    assert parse_ports([entry]) == [expected]


@pytest.mark.parametrize(
    "entry",
    ["3000-3005:3000-3005", "abc", "0", "70000", "80/icmp", {"published": 80}],
)
def test_invalid_ports(entry):
    with pytest.raises(ComposeError):
        parse_ports([entry])


def test_container_ports_on_deployment_are_deduplicated():
    deployment = convert_service_to_deployment(
        "web", {"image": "nginx", "ports": ["8080:80", "8081:80"], "expose": ["9000"]}
    )
    assert container_of(deployment)["ports"] == [
        {"containerPort": 80, "protocol": "TCP"},
        {"containerPort": 9000, "protocol": "TCP"},
    ]


def test_duplicate_service_port_listed_once():
    service = convert_service_to_k8s_service("web", {"image": "x", "ports": ["80:80", "80:80"]})
    assert len(service["spec"]["ports"]) == 1


def test_same_published_port_different_targets_rejected():
    with pytest.raises(ComposeError, match="publishes port 80 twice"):
        convert_service_to_k8s_service("web", {"image": "x", "ports": ["80:80", "80:81"]})


def test_same_port_number_tcp_and_udp_allowed():
    service = convert_service_to_k8s_service("dns", {"image": "x", "ports": ["53:53", "53:53/udp"]})
    assert [p["name"] for p in service["spec"]["ports"]] == ["tcp-53", "udp-53"]


def test_expose_ports_create_service():
    service = convert_service_to_k8s_service("api", {"image": "x", "expose": ["3000"]})
    assert service["spec"]["ports"][0]["port"] == 3000


# --- environment ----------------------------------------------------------------


def test_environment_list_format():
    env = parse_environment(["DEBUG=1", "URL=postgres://db:5432/app?x=y", "EMPTY="])
    assert env == [
        {"name": "DEBUG", "value": "1"},
        {"name": "URL", "value": "postgres://db:5432/app?x=y"},
        {"name": "EMPTY", "value": ""},
    ]


def test_environment_dict_format():
    env = parse_environment({"PORT": 8000, "DEBUG": True, "OFF": False, "FROM_HOST": None})
    assert env == [
        {"name": "PORT", "value": "8000"},
        {"name": "DEBUG", "value": "true"},
        {"name": "OFF", "value": "false"},
        {"name": "FROM_HOST", "value": ""},
    ]


def test_environment_on_deployment_and_host_var_note():
    compose = parse_compose(
        """
services:
  app:
    image: app
    environment:
      - MODE=prod
      - API_KEY
"""
    )
    result = convert_compose_to_k8s(compose)
    assert container_of(result["manifests"]["deployments"][0])["env"] == [
        {"name": "MODE", "value": "prod"},
        {"name": "API_KEY", "value": ""},
    ]
    assert any("API_KEY" in note for note in result["notes"])


def test_environment_invalid_type():
    with pytest.raises(ComposeError):
        parse_environment("A=1")


# --- deploy: replicas and resources -----------------------------------------------


def test_replicas_and_resources():
    compose = parse_compose(
        """
services:
  api:
    image: api
    deploy:
      replicas: 3
      resources:
        limits:
          cpus: '0.5'
          memory: 512M
        reservations:
          cpus: '0.25'
          memory: 128m
"""
    )
    result = convert_compose_to_k8s(compose)
    deployment = result["manifests"]["deployments"][0]
    assert deployment["spec"]["replicas"] == 3
    assert container_of(deployment)["resources"] == {
        "limits": {"cpu": "500m", "memory": "512Mi"},
        "requests": {"cpu": "250m", "memory": "128Mi"},
    }
    assert not any("no resource limits" in note for note in result["notes"])


@pytest.mark.parametrize("bad", ["many", -1])
def test_invalid_replicas(bad):
    with pytest.raises(ComposeError):
        convert_service_to_deployment("x", {"image": "x", "deploy": {"replicas": bad}})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("512M", "512Mi"), ("1g", "1Gi"), ("64kb", "64Ki"), (268435456, "256Mi"), ("1000b", "1000")],
)
def test_convert_memory(value, expected):
    assert convert_memory(value) == expected


@pytest.mark.parametrize("value", ["lots", "5x"])
def test_convert_memory_invalid(value):
    with pytest.raises(ComposeError):
        convert_memory(value)


@pytest.mark.parametrize(("value", "expected"), [("0.5", "500m"), (2, "2"), ("1.5", "1500m")])
def test_convert_cpus(value, expected):
    assert convert_cpus(value) == expected


@pytest.mark.parametrize("value", ["fast", 0])
def test_convert_cpus_invalid(value):
    with pytest.raises(ComposeError):
        convert_cpus(value)


# --- command, build, names ---------------------------------------------------------


def test_command_and_entrypoint():
    deployment = convert_service_to_deployment(
        "worker",
        {"image": "python:3.12", "entrypoint": ["python"], "command": "app.py --port 80"},
    )
    container = container_of(deployment)
    assert container["command"] == ["python"]
    assert container["args"] == ["app.py", "--port", "80"]


def test_build_uses_service_name_as_image():
    result = convert_compose_to_k8s({"services": {"api": {"build": "."}}})
    assert container_of(result["manifests"]["deployments"][0])["image"] == "api:latest"
    assert any("uses 'build'" in note for note in result["notes"])


def test_service_without_image_or_build():
    with pytest.raises(ComposeError, match="neither 'image' nor 'build'"):
        convert_service_to_deployment("x", {})


def test_name_sanitising():
    assert to_k8s_name("My_App.v2") == "my-app-v2"
    with pytest.raises(ComposeError):
        to_k8s_name("___")


def test_name_collision():
    with pytest.raises(ComposeError, match="both map"):
        convert_compose_to_k8s({"services": {"my_app": {"image": "a"}, "my-app": {"image": "b"}}})


# --- notes --------------------------------------------------------------------------


def test_notes_for_manual_attention():
    compose = parse_compose(
        """
services:
  web_app:
    image: web
    volumes: ["./data:/data"]
    depends_on: [db]
    networks: [backend]
    env_file: .env
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost"]
"""
    )
    notes = " | ".join(convert_compose_to_k8s(compose)["notes"])
    for expected in [
        "renamed to 'web-app'",
        "PersistentVolumeClaim",
        "initContainers",
        "service DNS",
        "liveness/readiness",
        "env_file",
        "no resource limits",
    ]:
        assert expected in notes


# --- input validation ------------------------------------------------------------------


def test_invalid_yaml_handling():
    with pytest.raises(ComposeError, match="Invalid YAML"):
        parse_compose("services:\n  web:\n    image: nginx\n   ports: [80")


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_input(text):
    with pytest.raises(ComposeError, match="empty"):
        parse_compose(text)


@pytest.mark.parametrize(
    "text",
    [
        "version: '3'\n",
        "version: '3'\nservices:\n",
        "version: '3'\nservices: {}\n",
    ],
)
def test_missing_services_error(text):
    with pytest.raises(ComposeError, match="no 'services'"):
        parse_compose(text)


def test_top_level_must_be_mapping():
    with pytest.raises(ComposeError, match="mapping"):
        parse_compose("- just\n- a list\n")


def test_services_must_be_mapping():
    with pytest.raises(ComposeError, match="'services' must be a mapping"):
        parse_compose("services:\n  - web\n")


def test_service_must_be_mapping():
    with pytest.raises(ComposeError, match="service 'web' must be a mapping"):
        parse_compose("services:\n  web: nginx\n")


def test_convert_without_services():
    with pytest.raises(ComposeError):
        convert_compose_to_k8s({})


# --- YAML output ----------------------------------------------------------------------


def test_manifests_to_yaml_round_trip():
    result = convert_compose_to_k8s(parse_compose(BASIC_COMPOSE))
    documents = list(yaml.safe_load_all(manifests_to_yaml(result["manifests"])))
    assert [doc["kind"] for doc in documents] == ["Deployment", "Service"]


# --- regressions from code review --------------------------------------------------------


def _alias_bomb(levels: int) -> str:
    text = "x:\n  a0: &a0 [aaaaaaaa, aaaaaaaa, aaaaaaaa, aaaaaaaa, aaaaaaaa]\n"
    for i in range(1, levels + 1):
        text += f"  a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 9) + "]\n"
    return text + f"services:\n  w:\n    image: x\n    environment: [*a{levels}]\n"


def test_alias_bomb_rejected_quickly():
    with pytest.raises(ComposeError, match="Input too large"):
        parse_compose(_alias_bomb(8))


def test_normal_anchors_and_merge_keys_still_work():
    compose = parse_compose(
        """
x-common: &common
  image: app:1.0
  environment: {MODE: prod}
services:
  api:
    <<: *common
    ports: ["8000"]
  worker:
    <<: *common
"""
    )
    result = convert_compose_to_k8s(compose)
    images = [container_of(d)["image"] for d in result["manifests"]["deployments"]]
    assert images == ["app:1.0", "app:1.0"]


@pytest.mark.parametrize(
    "text",
    ["[" * 5000 + "]" * 5000, "- " * 5000 + "x", "a:\n" + "- " * 200 + "x"],
)
def test_deeply_nested_yaml_rejected(text):
    with pytest.raises(ComposeError, match="nested too deeply"):
        parse_compose(text)


def test_reasonable_nesting_allowed():
    parse_compose("services:\n  w:\n    image: x\n    labels: " + "[" * 50 + "]" * 50 + "\n")


@pytest.mark.parametrize("value", [".inf", "-.inf", ".nan", "1" + "0" * 400])
def test_cpus_non_finite_rejected(value):
    limits = f"{{resources: {{limits: {{cpus: '{value}'}}}}}}"
    compose = parse_compose(f"services:\n  w:\n    image: x\n    deploy: {limits}\n")
    with pytest.raises(ComposeError, match="cpus"):
        convert_compose_to_k8s(compose)


@pytest.mark.parametrize("value", [".inf", "1.5", "true", "-1", "9999999999999"])
def test_replicas_invalid_values_rejected(value):
    compose = parse_compose(f"services:\n  w:\n    image: x\n    deploy: {{replicas: {value}}}\n")
    with pytest.raises(ComposeError, match="replicas"):
        convert_compose_to_k8s(compose)


def test_huge_memory_number_does_not_crash():
    assert convert_memory("9" * 400).isdigit()


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ("deploy: foo", "'deploy' must be a mapping"),
        ("deploy: {resources: foo}", "'deploy.resources' must be a mapping"),
        ("deploy: {resources: {limits: [1]}}", "'deploy.resources.limits' must be a mapping"),
        ("ports: 80", "'ports' must be a list"),
        ("ports: '8080:80'", "'ports' must be a list"),
        ("expose: 3000", "'expose' must be a list"),
    ],
)
def test_wrong_field_types_give_clear_errors(snippet, message):
    compose = parse_compose(f"services:\n  w:\n    image: x\n    {snippet}\n")
    with pytest.raises(ComposeError, match=message):
        convert_compose_to_k8s(compose)


def test_parse_ports_rejects_non_list():
    with pytest.raises(ComposeError, match="must be lists"):
        parse_ports("8080:80")


def test_unbalanced_quotes_in_command():
    compose = parse_compose('services:\n  w:\n    image: x\n    command: "echo \'hi"\n')
    with pytest.raises(ComposeError, match="could not parse 'command'"):
        convert_compose_to_k8s(compose)


def test_name_starting_with_digit_gets_prefix():
    compose = parse_compose("services:\n  1api:\n    image: x\n    ports: ['80']\n")
    result = convert_compose_to_k8s(compose)
    assert result["manifests"]["services"][0]["metadata"]["name"] == "svc-1api"
    assert result["manifests"]["deployments"][0]["metadata"]["name"] == "svc-1api"
    assert any("renamed to 'svc-1api'" in note for note in result["notes"])


def test_yaml_1_2_scalars_kept_as_written():
    compose = parse_compose(
        "services:\n  w:\n    image: x\n    ports: [22:22]\n"
        "    environment: {F: on, MODE: 0755, N: 1.10, EMPTY: }\n"
    )
    result = convert_compose_to_k8s(compose)
    assert result["manifests"]["services"][0]["spec"]["ports"][0]["port"] == 22
    assert container_of(result["manifests"]["deployments"][0])["env"] == [
        {"name": "F", "value": "on"},
        {"name": "MODE", "value": "0755"},
        {"name": "N", "value": "1.10"},
        {"name": "EMPTY", "value": ""},
    ]


def test_null_command_and_entrypoint_ignored():
    compose = parse_compose("services:\n  w:\n    image: x\n    command:\n    entrypoint:\n")
    container = container_of(convert_compose_to_k8s(compose)["manifests"]["deployments"][0])
    assert "args" not in container
    assert "command" not in container


# --- regressions from the second code review -------------------------------------------


def _merge_bomb(levels: int) -> str:
    text = "x:\n  a0: &a0 {k: v}\n"
    for i in range(1, levels + 1):
        text += f"  a{i}: &a{i} {{<<: [" + ", ".join([f"*a{i - 1}"] * 9) + "]}\n"
    return text + "services:\n  w: {image: x}\n"


def test_merge_key_bomb_is_linear():
    start = time.perf_counter()
    parse_compose(_merge_bomb(12))  # 9**12 merged pairs before the fix
    assert time.perf_counter() - start < 1


def test_merge_keys_last_value_wins():
    compose = parse_compose(
        """
x-a: &a {image: a, environment: {MODE: a}}
x-b: &b {image: b}
services:
  w:
    <<: [*a, *b]
    image: own
"""
    )
    service = compose["services"]["w"]
    # Explicit keys beat merged ones; among merged mappings the first listed wins.
    assert service["image"] == "own"
    assert service["environment"] == {"MODE": "a"}


def test_too_many_values_rejected_before_loading():
    flat = "services:\n  w:\n    image: x\n    labels: [" + ",".join(["a"] * 30000) + "]\n"
    start = time.perf_counter()
    with pytest.raises(ComposeError, match="Input too large"):
        parse_compose(flat)
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize("value", ["²", "٣"])
def test_replicas_non_ascii_digits_rejected(value):
    compose = parse_compose(f"services:\n  w:\n    image: x\n    deploy: {{replicas: '{value}'}}\n")
    with pytest.raises(ComposeError, match="replicas"):
        convert_compose_to_k8s(compose)


def test_cpus_below_one_millicore_rejected():
    with pytest.raises(ComposeError, match="at least 0.001"):
        convert_cpus("0.0004")
    assert convert_cpus("0.001") == "1m"


def test_memory_non_ascii_digits_rejected():
    with pytest.raises(ComposeError):
        convert_memory("٥١٢M")


def test_explicit_empty_env_value_not_flagged_as_host_var():
    compose = parse_compose(
        "services:\n  w:\n    image: x\n    environment: ['EMPTY=', 'FROM_HOST']\n"
    )
    notes = " | ".join(convert_compose_to_k8s(compose)["notes"])
    assert "'FROM_HOST' has no value" in notes
    assert "EMPTY" not in notes


def test_dict_env_null_vs_empty_string():
    compose = parse_compose("services:\n  w:\n    image: x\n    environment: {A: , B: ''}\n")
    notes = " | ".join(convert_compose_to_k8s(compose)["notes"])
    assert "'A' has no value" in notes
    assert "'B'" not in notes


def test_limits_note_when_no_k8s_limits_generated():
    compose = parse_compose(
        "services:\n  w:\n    image: x\n    deploy: {resources: {limits: {pids: 5}}}\n"
    )
    assert "'w' has no resource limits" in convert_compose_to_k8s(compose)["notes"]


@pytest.mark.parametrize(
    ("environment", "message"),
    [([None], "empty entry"), (["=x"], "empty name"), ({" ": "x"}, "empty name")],
)
def test_invalid_env_entries_rejected(environment, message):
    with pytest.raises(ComposeError, match=message):
        parse_environment(environment)
