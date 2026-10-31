"""Convert docker-compose (v3) files into Kubernetes Deployment and Service manifests.

Example:
    compose = parse_compose(open("docker-compose.yaml").read())
    result = convert_compose_to_k8s(compose, namespace="default")
    result["manifests"]["deployments"]  # list of Deployment dicts
    result["notes"]                     # things a human should look at
"""

from __future__ import annotations

import math
import re
import shlex
from decimal import Decimal
from typing import Any

import yaml

# K8s Service names must be DNS-1035 labels: lowercase alphanumerics and '-', starting with a
# letter, max 63 chars. Deployments get the same name so labels and selectors line up.
_DNS_LABEL_MAX = 63
_INVALID_NAME_CHARS = re.compile(r"[^a-z0-9-]+")
_NAME_PREFIX = "svc-"

# Upper bound on YAML values, both as written and after aliases are expanded. A real compose
# file has a few hundred; this stops oversized or "billion laughs" input from eating CPU.
MAX_NODES = 10_000

# Real compose files nest lists/mappings fewer than 10 levels deep.
MAX_DEPTH = 100

# libyaml's C parser ships in the PyYAML wheel and is 10-100x faster than the pure-Python
# one (50,000 nested brackets: ~3ms vs ~0.3s). Fall back if PyYAML was built without it.
_FAST_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# Deployment spec.replicas is an int32.
_MAX_REPLICAS = 2**31 - 1

_NULL_TAG = "tag:yaml.org,2002:null"
_MERGE_TAG = "tag:yaml.org,2002:merge"
_DIGITS = re.compile(r"[0-9]+")


class SafeMergeLoader(_FAST_LOADER):
    """Safe loader whose `<<` merge keys cost linear, not exponential, time.

    PyYAML copies every merged key/value pair once per reference before de-duplicating, so
    `a2: {<<: [*a1, *a1, ...]}` chained a few levels deep grows 9x per level (a 500-char
    input can take minutes). Dropping duplicate keys after each merge keeps it linear. The
    last occurrence of a key wins, which is the same result PyYAML gives.
    """

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        super().flatten_mapping(node)
        seen: set[tuple[str, str]] = set()
        kept: list[tuple[yaml.Node, yaml.Node]] = []
        for key_node, value_node in reversed(node.value):
            if isinstance(key_node, yaml.ScalarNode):
                key = (key_node.tag, key_node.value)
                if key in seen:
                    continue
                seen.add(key)
            kept.append((key_node, value_node))
        kept.reverse()
        node.value = kept


class _ComposeLoader(SafeMergeLoader):
    """Loader that keeps plain scalars as strings, except null and '<<' merge keys.

    PyYAML follows YAML 1.1, where `on` means True, `0755` is octal 493 and an unquoted
    `22:22` is the base-60 number 1342. Compose uses YAML 1.2, so those values must reach
    the container unchanged. Keeping the text as-is gives the same result, and numeric
    fields (ports, replicas, cpus) are converted explicitly below.
    """


_ComposeLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag in (_NULL_TAG, _MERGE_TAG)]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

# Compose memory units -> K8s binary suffixes. Docker treats these as powers of 1024.
_MEMORY_UNITS = {
    "b": "",
    "k": "Ki",
    "kb": "Ki",
    "m": "Mi",
    "mb": "Mi",
    "g": "Gi",
    "gb": "Gi",
}
_MEMORY_PATTERN = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)\s*$")


class ComposeError(ValueError):
    """Raised when the compose input cannot be parsed or converted."""


def parse_compose(yaml_string: str) -> dict[str, Any]:
    """Parse a docker-compose YAML string and check it has a usable `services` section.

    Raises:
        ComposeError: if the YAML is invalid, not a mapping, or has no services.
    """
    if not yaml_string or not yaml_string.strip():
        raise ComposeError("Input is empty")

    try:
        check_yaml_limits(yaml_string)
        data = yaml.load(yaml_string, Loader=_ComposeLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or str(exc)
        raise ComposeError(f"Invalid YAML: {problem}") from exc

    check_node_count(data)

    if not isinstance(data, dict):
        raise ComposeError("Invalid compose file: top level must be a mapping")

    services = data.get("services")
    if not services:
        raise ComposeError("Invalid compose file: no 'services' defined")
    if not isinstance(services, dict):
        raise ComposeError("Invalid compose file: 'services' must be a mapping")

    for name, config in services.items():
        if config is not None and not isinstance(config, dict):
            raise ComposeError(f"Invalid compose file: service '{name}' must be a mapping")

    return data


def check_yaml_limits(text: str, max_depth: int = MAX_DEPTH, max_nodes: int = MAX_NODES) -> None:
    """Raise ComposeError if YAML nests deeper than `max_depth` or has over `max_nodes` values.

    Building objects is the expensive part of loading YAML, and it recurses, so `[[[[...]]]]`
    thousands deep or a 60 KB list of 30,000 items costs about a second of CPU. The parser's
    event stream is flat and runs in C, so checking it first rejects both in milliseconds.
    Aliases are counted once here; check_node_count() covers their expanded size.

    Raises yaml.YAMLError for syntax errors, so callers can report them as usual.
    """
    depth = 0
    nodes = 0
    for event in yaml.parse(text, Loader=_FAST_LOADER):
        if isinstance(event, yaml.NodeEvent):
            nodes += 1
            if nodes > max_nodes:
                raise ComposeError(f"Input too large: more than {max_nodes} values")
        if isinstance(event, yaml.CollectionStartEvent):
            depth += 1
            if depth > max_depth:
                raise ComposeError("Invalid YAML: nested too deeply")
        elif isinstance(event, yaml.CollectionEndEvent):
            depth -= 1


def check_node_count(data: Any, limit: int = MAX_NODES) -> None:
    """Raise ComposeError if `data` has more than `limit` nodes once aliases are expanded.

    Stops counting as soon as the limit is passed, so even a huge alias bomb is rejected fast.
    """
    count = 0
    stack = [data]
    while stack:
        node = stack.pop()
        count += 1
        if count > limit:
            raise ComposeError(
                f"Input too large: more than {limit} values after expanding YAML aliases"
            )
        if isinstance(node, dict):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def to_k8s_name(name: str) -> str:
    """Turn a compose service name into a valid K8s object name.

    'My_App' -> 'my-app'. Names must start with a letter, so '1api' -> 'svc-1api'.
    """
    cleaned = _INVALID_NAME_CHARS.sub("-", str(name).lower()).strip("-")
    if cleaned and not cleaned[0].isalpha():
        cleaned = _NAME_PREFIX + cleaned
    cleaned = cleaned[:_DNS_LABEL_MAX].rstrip("-")
    if not cleaned:
        raise ComposeError(f"Service name '{name}' cannot be turned into a valid K8s name")
    return cleaned


def _expect_list(value: Any, field: str, service: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ComposeError(f"Service '{service}': '{field}' must be a list")
    return value


def _expect_mapping(value: Any, field: str, service: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ComposeError(f"Service '{service}': '{field}' must be a mapping")
    return value


def parse_ports(ports: list[Any] | None) -> list[dict[str, Any]]:
    """Normalise compose port entries into a list of port mappings.

    Supports the short syntax ("80", "8080:80", "127.0.0.1:8080:80", "53:53/udp", 80)
    and the long syntax ({"target": 80, "published": 8080, "protocol": "tcp"}).

    Returns a list of dicts: {"container_port": int, "service_port": int, "protocol": str}.
    """
    if ports is not None and not isinstance(ports, list):
        raise ComposeError("'ports' and 'expose' must be lists")
    result: list[dict[str, Any]] = []
    for entry in ports or []:
        if isinstance(entry, dict):
            target = entry.get("target")
            if target is None:
                raise ComposeError(f"Port entry {entry!r} is missing 'target'")
            published = entry.get("published") or target
            protocol = str(entry.get("protocol", "tcp"))
            result.append(_port(target, published, protocol, entry))
            continue

        text = str(entry).strip()
        protocol = "tcp"
        if "/" in text:
            text, protocol = text.rsplit("/", 1)

        parts = text.split(":")
        container = parts[-1]
        published = parts[-2] if len(parts) >= 2 and parts[-2] else container
        if "-" in container or "-" in published:
            raise ComposeError(f"Port ranges are not supported: '{entry}'")
        result.append(_port(container, published, protocol, entry))
    return result


def _port(container: Any, published: Any, protocol: str, raw: Any) -> dict[str, Any]:
    try:
        container_port = int(container)
        service_port = int(published)
    except (TypeError, ValueError) as exc:
        raise ComposeError(f"Invalid port entry: '{raw}'") from exc
    for value in (container_port, service_port):
        if not 1 <= value <= 65535:
            raise ComposeError(f"Port out of range in entry: '{raw}'")
    protocol = protocol.upper()
    if protocol not in {"TCP", "UDP", "SCTP"}:
        raise ComposeError(f"Unsupported protocol in port entry: '{raw}'")
    return {"container_port": container_port, "service_port": service_port, "protocol": protocol}


def _environment_pairs(environment: list[Any] | dict[str, Any] | None) -> list[tuple[str, Any]]:
    """(name, value) pairs from compose `environment`. value is None when compose would read
    it from the host (`- B` or `B:`), and "" when it is set empty on purpose (`- B=`)."""
    if not environment:
        return []

    pairs: list[tuple[str, Any]] = []
    if isinstance(environment, dict):
        pairs = [(str(name), value) for name, value in environment.items()]
    elif isinstance(environment, list):
        for item in environment:
            if item is None:
                raise ComposeError("'environment' list has an empty entry")
            name, sep, value = str(item).partition("=")
            pairs.append((name, value if sep else None))
    else:
        raise ComposeError("'environment' must be a list or a mapping")

    for name, _ in pairs:
        if not name.strip():
            raise ComposeError("'environment' has a variable with an empty name")
    return [(name.strip(), value) for name, value in pairs]


def parse_environment(environment: list[Any] | dict[str, Any] | None) -> list[dict[str, str]]:
    """Convert compose `environment` (list or dict form) to a K8s `env` list.

    ["A=1", "B"] and {"A": 1, "B": None} both give
    [{"name": "A", "value": "1"}, {"name": "B", "value": ""}].
    """
    return [
        {"name": name, "value": _env_value(value)}
        for name, value in _environment_pairs(environment)
    ]


def _env_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        # YAML turns `true` into True; compose passes it to the container as "true".
        return "true" if value else "false"
    return str(value)


def convert_memory(value: Any) -> str:
    """Convert a compose memory value to K8s format.

    '512M' -> '512Mi', '1g' -> '1Gi', 268435456 (bytes) -> '256Mi'.
    """
    match = _MEMORY_PATTERN.match(str(value))
    if not match:
        raise ComposeError(f"Invalid memory value: '{value}'")
    number, unit = match.groups()
    unit = unit.lower()
    if unit not in _MEMORY_UNITS and unit != "":
        raise ComposeError(f"Unknown memory unit in '{value}'")
    if unit in ("", "b"):
        # Decimal, not float: a 400-digit number would otherwise become inf and crash int().
        number_bytes = int(Decimal(number))
        if number_bytes % (1024 * 1024) == 0:
            return f"{number_bytes // (1024 * 1024)}Mi"
        return str(number_bytes)
    return f"{number}{_MEMORY_UNITS[unit]}"


def convert_cpus(value: Any) -> str:
    """Convert compose `cpus` ('0.5', 2) to K8s CPU format ('500m', '2')."""
    try:
        cpus = float(value)
    except (TypeError, ValueError) as exc:
        raise ComposeError(f"Invalid cpus value: '{value}'") from exc
    if not math.isfinite(cpus):
        raise ComposeError(f"Invalid cpus value: '{value}'")
    if cpus <= 0:
        raise ComposeError(f"cpus must be greater than 0, got '{value}'")
    millicores = round(cpus * 1000)
    if millicores < 1:
        # Below 1 millicore would round to a zero CPU quantity.
        raise ComposeError(f"cpus must be at least 0.001, got '{value}'")
    if millicores % 1000 == 0:
        return str(millicores // 1000)
    return f"{millicores}m"


def _convert_resource_block(block: Any, field: str, service: str) -> dict[str, str]:
    block = _expect_mapping(block, field, service)
    resources: dict[str, str] = {}
    if block.get("cpus") is not None:
        resources["cpu"] = convert_cpus(block["cpus"])
    if block.get("memory") is not None:
        resources["memory"] = convert_memory(block["memory"])
    return resources


def _command_list(value: Any, field: str, service: str) -> list[str]:
    if isinstance(value, list):
        return [str(part) for part in value]
    try:
        return shlex.split(str(value))
    except ValueError as exc:
        raise ComposeError(f"Service '{service}': could not parse '{field}': {exc}") from exc


def _parse_replicas(value: Any, service: str) -> int:
    text = str(value).strip()
    # Not str.isdigit(): that accepts '²' and other characters int() rejects.
    if isinstance(value, bool) or not _DIGITS.fullmatch(text):
        raise ComposeError(f"Service '{service}' has an invalid replicas value: '{value}'")
    replicas = int(text)
    if replicas > _MAX_REPLICAS:
        raise ComposeError(f"Service '{service}' has too many replicas: {replicas}")
    return replicas


def _service_ports(name: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """All port mappings from a service's `ports` and `expose` lists."""
    ports = parse_ports(_expect_list(config.get("ports"), "ports", name))
    return ports + parse_ports(_expect_list(config.get("expose"), "expose", name))


def convert_service_to_deployment(
    name: str, config: dict[str, Any] | None, namespace: str = "default"
) -> dict[str, Any]:
    """Build a K8s apps/v1 Deployment for one compose service."""
    config = config or {}
    k8s_name = to_k8s_name(name)
    labels = {"app": k8s_name}

    image = config.get("image")
    if not image:
        if "build" not in config:
            raise ComposeError(f"Service '{name}' has neither 'image' nor 'build'")
        # Built locally: assume the image gets tagged with the service name.
        image = f"{k8s_name}:latest"

    container: dict[str, Any] = {"name": k8s_name, "image": str(image)}

    if config.get("entrypoint") is not None:
        container["command"] = _command_list(config["entrypoint"], "entrypoint", name)
    if config.get("command") is not None:
        container["args"] = _command_list(config["command"], "command", name)

    ports = _service_ports(name, config)
    if ports:
        seen: set[tuple[int, str]] = set()
        container_ports = []
        for port in ports:
            key = (port["container_port"], port["protocol"])
            if key in seen:
                continue
            seen.add(key)
            container_ports.append(
                {"containerPort": port["container_port"], "protocol": port["protocol"]}
            )
        container["ports"] = container_ports

    env = parse_environment(config.get("environment"))
    if env:
        container["env"] = env

    deploy = _expect_mapping(config.get("deploy"), "deploy", name)
    resources_config = _expect_mapping(deploy.get("resources"), "deploy.resources", name)
    resources: dict[str, dict[str, str]] = {}
    limits = _convert_resource_block(
        resources_config.get("limits"), "deploy.resources.limits", name
    )
    requests = _convert_resource_block(
        resources_config.get("reservations"), "deploy.resources.reservations", name
    )
    if limits:
        resources["limits"] = limits
    if requests:
        resources["requests"] = requests
    if resources:
        container["resources"] = resources

    replicas = deploy.get("replicas")
    replicas = 1 if replicas is None else _parse_replicas(replicas, name)

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": k8s_name, "namespace": namespace, "labels": dict(labels)},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": dict(labels)},
            "template": {
                "metadata": {"labels": dict(labels)},
                "spec": {"containers": [container]},
            },
        },
    }


def convert_service_to_k8s_service(
    name: str, config: dict[str, Any] | None, namespace: str = "default"
) -> dict[str, Any] | None:
    """Build a ClusterIP Service for a compose service's ports. Returns None if it has no ports."""
    config = config or {}
    ports = _service_ports(name, config)
    if not ports:
        return None

    k8s_name = to_k8s_name(name)
    service_ports = []
    seen: dict[tuple[int, str], int] = {}
    for port in ports:
        key = (port["service_port"], port["protocol"])
        if key in seen:
            if seen[key] != port["container_port"]:
                raise ComposeError(
                    f"Service '{name}' publishes port {port['service_port']} twice "
                    f"with different container ports ({seen[key]} and {port['container_port']})"
                )
            continue
        seen[key] = port["container_port"]
        service_ports.append(
            {
                # Port names are required when there is more than one port; max 15 chars.
                "name": f"{port['protocol'].lower()}-{port['service_port']}",
                "port": port["service_port"],
                "targetPort": port["container_port"],
                "protocol": port["protocol"],
            }
        )

    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": k8s_name, "namespace": namespace, "labels": {"app": k8s_name}},
        "spec": {"type": "ClusterIP", "selector": {"app": k8s_name}, "ports": service_ports},
    }


def _service_notes(name: str, config: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    k8s_name = to_k8s_name(name)
    if k8s_name != name:
        notes.append(f"'{name}' was renamed to '{k8s_name}' to be a valid K8s name")
    if "build" in config and not config.get("image"):
        notes.append(
            f"'{name}' uses 'build': image set to '{k8s_name}:latest'; "
            "build and push it to a registry your cluster can pull from"
        )
    if config.get("volumes"):
        notes.append(f"'{name}' uses volumes: consider a PersistentVolumeClaim or ConfigMap")
    if config.get("depends_on"):
        notes.append(
            f"'{name}' uses depends_on: K8s does not order startup; "
            "consider initContainers or readiness probes"
        )
    if config.get("networks"):
        notes.append(
            f"'{name}' uses networks: K8s uses service DNS "
            "(reach other services by name, e.g. http://db:5432)"
        )
    if config.get("healthcheck"):
        notes.append(f"'{name}' has a healthcheck: convert it to liveness/readiness probes")
    if config.get("env_file"):
        notes.append(f"'{name}' uses env_file: move those values into a ConfigMap or Secret")
    for var_name, value in _environment_pairs(config.get("environment")):
        if value is None:
            notes.append(
                f"'{name}' env var '{var_name}' has no value "
                "(compose reads it from the host); set it or use a Secret"
            )
    return notes


def convert_compose_to_k8s(
    compose_dict: dict[str, Any], namespace: str = "default", include_service: bool = True
) -> dict[str, Any]:
    """Convert a parsed compose file into K8s manifests.

    Returns:
        {"manifests": {"deployments": [...], "services": [...]}, "notes": [...]}
    """
    services = compose_dict.get("services") if isinstance(compose_dict, dict) else None
    if not services or not isinstance(services, dict):
        raise ComposeError("Invalid compose file: no 'services' defined")

    deployments: list[dict[str, Any]] = []
    k8s_services: list[dict[str, Any]] = []
    notes: list[str] = []
    used_names: dict[str, str] = {}

    for name, config in services.items():
        name = str(name)
        config = config or {}
        k8s_name = to_k8s_name(name)
        if k8s_name in used_names:
            raise ComposeError(
                f"Services '{used_names[k8s_name]}' and '{name}' both map to K8s name '{k8s_name}'"
            )
        used_names[k8s_name] = name

        deployment = convert_service_to_deployment(name, config, namespace)
        deployments.append(deployment)
        if include_service:
            service = convert_service_to_k8s_service(name, config, namespace)
            if service is not None:
                k8s_services.append(service)
        notes.extend(_service_notes(name, config))
        # Check what was generated, not the compose input: `limits: {pids: 5}` yields none.
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        if "limits" not in container.get("resources", {}):
            notes.append(f"'{name}' has no resource limits")

    return {"manifests": {"deployments": deployments, "services": k8s_services}, "notes": notes}


def manifests_to_yaml(manifests: dict[str, list[dict[str, Any]]]) -> str:
    """Render manifests as a multi-document YAML string (Deployments first, then Services)."""
    documents = manifests.get("deployments", []) + manifests.get("services", [])
    return yaml.safe_dump_all(documents, sort_keys=False, default_flow_style=False)
