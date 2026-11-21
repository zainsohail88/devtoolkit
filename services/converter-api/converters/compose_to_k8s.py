"""Convert docker-compose (v3) files into Kubernetes manifests.

Each service becomes a Deployment (with probes from `healthcheck`, init containers from
`depends_on`, volume mounts from `volumes`) and, if it has ports, a Service. Named volumes
become PersistentVolumeClaims. Only what the compose file needs is generated.

Example:
    compose = parse_compose(open("docker-compose.yaml").read())
    result = convert_compose_to_k8s(compose, namespace="default")
    result["manifests"]["deployments"]  # list of Deployment dicts
    result["notes"]                     # things a human should look at
"""

from __future__ import annotations

import copy
import math
import re
import shlex
from decimal import Decimal
from typing import Any

import yaml

from converters.compose_constants import (
    BIND_PREFIXES,
    DEFAULT_LIMITS,
    DEFAULT_REQUESTS,
    DEFAULT_VOLUME_SIZE,
    DIGITS,
    DNS_LABEL_MAX,
    DURATION,
    DURATION_SECONDS,
    INVALID_NAME_CHARS,
    MANIFEST_ORDER,
    MAX_REPLICAS,
    MEMORY_PATTERN,
    MEMORY_UNITS,
    NAME_PREFIX,
    WAIT_IMAGE,
)
from converters.yaml_safety import (
    PlainScalarLoader,
    YamlLimitError,
    check_node_count,
    check_yaml_limits,
    load_single,
)


class ComposeError(ValueError):
    """Raised when the compose input cannot be parsed or converted.

    `message` is written for the user and is safe to send back to them: it describes their
    input and never includes internal details.
    """

    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        """The user text, and the position in the input for a YAML syntax error."""
        super().__init__(message)
        self.message = message
        # 1-based position in the input, when the error is a YAML syntax error.
        self.line = line
        self.column = column


def parse_compose(yaml_string: str) -> dict[str, Any]:
    """Parse a docker-compose YAML string and check it has a usable `services` section.

    Raises:
        ComposeError: if the YAML is invalid, not a mapping, or has no services.
    """
    if not yaml_string or not yaml_string.strip():
        raise ComposeError("Input is empty")

    try:
        check_yaml_limits(yaml_string)
        data = load_single(yaml_string, PlainScalarLoader)
        check_node_count(data)
    except YamlLimitError as exc:
        raise ComposeError(exc.message) from exc
    except yaml.YAMLError as exc:
        # `problem` is the description of the input by the parser ("mapping values are not
        # allowed here"). Nothing else from the exception reaches the user.
        problem = getattr(exc, "problem", None) or "syntax error"
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        column = mark.column + 1 if mark is not None else None
        raise ComposeError(f"Invalid YAML: {problem}", line, column) from exc

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


def to_k8s_name(name: str) -> str:
    """Turn a compose service name into a valid K8s object name.

    'My_App' -> 'my-app'. Names must start with a letter, so '1api' -> 'svc-1api'.
    """
    cleaned = INVALID_NAME_CHARS.sub("-", str(name).lower()).strip("-")
    if cleaned and not cleaned[0].isalpha():
        cleaned = NAME_PREFIX + cleaned
    cleaned = cleaned[:DNS_LABEL_MAX].rstrip("-")
    if not cleaned:
        raise ComposeError(f"Service name '{name}' cannot be turned into a valid K8s name")
    return cleaned


def _expect_list(value: Any, field: str, service: str) -> list[Any]:
    """`value` of `field` as a list: [] for None, an error for anything else."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ComposeError(f"Service '{service}': '{field}' must be a list")
    return value


def _expect_mapping(value: Any, field: str, service: str) -> dict[str, Any]:
    """`value` of `field` as a mapping: {} for None, an error for anything else."""
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
    """One checked port mapping; `raw` is the compose entry, for error messages."""
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
    """An environment value as the text the container gets."""
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
    match = MEMORY_PATTERN.match(str(value).strip())
    if not match:
        raise ComposeError(f"Invalid memory value: '{value}'")
    number, unit = match.groups()
    unit = unit.lower()
    if unit not in MEMORY_UNITS and unit != "":
        raise ComposeError(f"Unknown memory unit in '{value}'")
    if unit in ("", "b"):
        # Decimal, not float: a 400-digit number would otherwise become inf and crash int().
        number_bytes = int(Decimal(number))
        if number_bytes % (1024 * 1024) == 0:
            return f"{number_bytes // (1024 * 1024)}Mi"
        return str(number_bytes)
    return f"{number}{MEMORY_UNITS[unit]}"


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
    """A deploy.resources limits or reservations block as K8s cpu and memory."""
    block = _expect_mapping(block, field, service)
    resources: dict[str, str] = {}
    if block.get("cpus") is not None:
        resources["cpu"] = convert_cpus(block["cpus"])
    if block.get("memory") is not None:
        resources["memory"] = convert_memory(block["memory"])
    return resources


def _command_list(value: Any, field: str, service: str) -> list[str]:
    """`entrypoint` or `command` as a list: a list as it is, a string split like a shell."""
    if isinstance(value, list):
        return [str(part) for part in value]
    try:
        return shlex.split(str(value))
    except ValueError as exc:
        raise ComposeError(f"Service '{service}': could not parse '{field}': {exc}") from exc


def _parse_replicas(value: Any, service: str) -> int:
    """deploy.replicas as a checked int32."""
    text = str(value).strip()
    # Not str.isdigit(): that accepts '²' and other characters int() rejects.
    if isinstance(value, bool) or not DIGITS.fullmatch(text):
        raise ComposeError(f"Service '{service}' has an invalid replicas value: '{value}'")
    replicas = int(text)
    if replicas > MAX_REPLICAS:
        raise ComposeError(f"Service '{service}' has too many replicas: {replicas}")
    return replicas


def _service_ports(name: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """All port mappings from the `ports` and `expose` lists of a service."""
    ports = parse_ports(_expect_list(config.get("ports"), "ports", name))
    return ports + parse_ports(_expect_list(config.get("expose"), "expose", name))


def _is_true(value: Any) -> bool:
    """Compose booleans. The loader keeps scalars as text, so `true` arrives as "true"."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "on", "1")


def parse_duration(value: Any, field: str, service: str) -> int:
    """Compose duration ('1m30s', '500ms', '10s', or a plain number of seconds) to whole
    seconds, rounded up and at least 1, since Kubernetes probes take integer seconds."""
    if isinstance(value, bool):
        raise ComposeError(f"Service '{service}': invalid duration in '{field}': '{value}'")
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        seconds = float(text)
    else:
        matches = DURATION.findall(text)
        if not text or "".join(n + u for n, u in matches) != text.replace(" ", ""):
            raise ComposeError(f"Service '{service}': invalid duration in '{field}': '{value}'")
        seconds = sum(float(n) * DURATION_SECONDS[u] for n, u in matches)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ComposeError(f"Service '{service}': invalid duration in '{field}': '{value}'")
    return max(1, math.ceil(seconds))


def convert_healthcheck(name: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """Compose `healthcheck` -> one probe spec (used for both liveness and readiness).

    Returns None when there is no usable healthcheck (missing, `disable: true`, or NONE).
    """
    check = config.get("healthcheck")
    if not check:
        return None
    check = _expect_mapping(check, "healthcheck", name)
    if _is_true(check.get("disable", False)):
        return None
    test = check.get("test")
    if test is None:
        return None
    if isinstance(test, list):
        parts = [str(p) for p in test]
        if not parts or parts[0] == "NONE":
            return None
        if parts[0] == "CMD":
            command = parts[1:]
        elif parts[0] == "CMD-SHELL":
            command = ["sh", "-c", " ".join(parts[1:])]
        else:
            command = parts
    else:
        command = ["sh", "-c", str(test)]
    if not command:
        return None

    probe: dict[str, Any] = {"exec": {"command": command}}
    if check.get("interval") is not None:
        probe["periodSeconds"] = parse_duration(check["interval"], "healthcheck.interval", name)
    if check.get("timeout") is not None:
        probe["timeoutSeconds"] = parse_duration(check["timeout"], "healthcheck.timeout", name)
    if check.get("retries") is not None:
        retries = check["retries"]
        text = str(retries).strip()
        if isinstance(retries, bool) or not DIGITS.fullmatch(text) or int(text) < 1:
            raise ComposeError(f"Service '{name}': invalid healthcheck.retries: '{retries}'")
        probe["failureThreshold"] = int(text)
    if check.get("start_period") is not None:
        probe["initialDelaySeconds"] = parse_duration(
            check["start_period"], "healthcheck.start_period", name
        )
    return probe


def _mount_name(target: str) -> str:
    """A pod volume name from a mount path."""
    cleaned = INVALID_NAME_CHARS.sub("-", target.lower()).strip("-")
    return (cleaned or "data")[:DNS_LABEL_MAX].rstrip("-")


class PodVolumes:
    """The pod volumes and container mounts of one service. The same volume mounted at
    two paths is one pod volume with two mounts."""

    def __init__(self) -> None:
        """No volumes yet."""
        self.volumes: list[dict[str, Any]] = []
        self.mounts: list[dict[str, Any]] = []
        self.used: set[str] = set()

    def add(self, volume: dict[str, Any], target: str, read_only: bool) -> None:
        """Mount `volume` at `target`, adding it to the pod the first time."""
        if volume["name"] not in self.used:
            self.used.add(volume["name"])
            self.volumes.append(volume)
        mount: dict[str, Any] = {"name": volume["name"], "mountPath": target}
        if read_only:
            mount["readOnly"] = True
        self.mounts.append(mount)


def convert_volumes(
    name: str, config: dict[str, Any], declared: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    """Compose `volumes` of one service -> (pod volumes, volumeMounts, PVC names, notes).

    Named volumes become PersistentVolumeClaims (one per volume name, shared across
    services). Anonymous volumes and tmpfs become emptyDir. Bind mounts have no Kubernetes
    equivalent and are reported as notes.
    """
    entries = _expect_list(config.get("volumes"), "volumes", name)
    pod = PodVolumes()
    claims: list[str] = []
    notes: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            kind = str(entry.get("type", "volume"))
            source = entry.get("source")
            target = entry.get("target")
            read_only = _is_true(entry.get("read_only", False))
            if not target:
                raise ComposeError(f"Service '{name}': volume entry {entry!r} is missing 'target'")
            target = str(target)
        else:
            parts = str(entry).split(":")
            if len(parts) == 1:
                kind, source, target, read_only = "volume", None, parts[0], False
            elif len(parts) in (2, 3):
                source, target = parts[0], parts[1]
                read_only = len(parts) == 3 and "ro" in parts[2].split(",")
                kind = "bind" if source.startswith(BIND_PREFIXES) else "volume"
            else:
                raise ComposeError(f"Service '{name}': invalid volume entry '{entry}'")
        if not target.startswith("/"):
            raise ComposeError(
                f"Service '{name}': volume target must be an absolute path: '{target}'"
            )

        if kind == "bind":
            notes.append(
                f"'{name}' bind mount '{source}:{target}' was not converted: Kubernetes has no "
                "host paths; use a ConfigMap, bake it into the image or use a PersistentVolumeClaim"
            )
        elif kind == "tmpfs":
            pod.add({"name": _mount_name(target), "emptyDir": {"medium": "Memory"}}, target, False)
        elif kind == "volume" and not source:
            pod.add({"name": _mount_name(target), "emptyDir": {}}, target, read_only)
        elif kind == "volume":
            claim = to_k8s_name(str(source))
            spec = declared.get(str(source)) if isinstance(declared, dict) else None
            spec = spec if isinstance(spec, dict) else {}
            if _is_true(spec.get("external", False)):
                claim = to_k8s_name(str(spec.get("name") or source))
                notes.append(
                    f"'{name}' mounts external volume '{source}': a PersistentVolumeClaim named "
                    f"'{claim}' must already exist"
                )
            else:
                claims.append(claim)
            pod.add(
                {"name": claim, "persistentVolumeClaim": {"claimName": claim}}, target, read_only
            )
        else:
            notes.append(f"'{name}' volume type '{kind}' is not supported and was skipped")
    return pod.volumes, pod.mounts, claims, notes


def convert_depends_on(
    name: str, config: dict[str, Any], services: dict[str, Any], include_service: bool
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compose `depends_on` -> init containers that wait for the first port of each dependency.

    Kubernetes starts pods in any order; an init container that blocks until the dependency
    answers on its Service port gives the same effect. Dependencies without ports (or when
    Services are not generated) cannot be waited on and are reported as notes.
    """
    depends = config.get("depends_on")
    if not depends:
        return [], []
    if isinstance(depends, dict):
        names = [str(d) for d in depends]
    elif isinstance(depends, list):
        names = [str(d) for d in depends]
    else:
        raise ComposeError(f"Service '{name}': 'depends_on' must be a list or a mapping")

    inits: list[dict[str, Any]] = []
    notes: list[str] = []
    for dep in names:
        if dep not in services:
            raise ComposeError(f"Service '{name}' depends on unknown service '{dep}'")
        dep_name = to_k8s_name(dep)
        ports = _service_ports(dep, services[dep] or {}) if include_service else []
        if not ports:
            why = "has no ports" if include_service else "has no Service (Services were disabled)"
            notes.append(
                f"'{name}' depends on '{dep}', which {why}: nothing to wait for, so no init "
                f"container was added; make '{name}' retry its connection"
            )
            continue
        port = ports[0]["service_port"]
        inits.append(
            {
                "name": f"wait-for-{dep_name}"[:DNS_LABEL_MAX].rstrip("-"),
                "image": WAIT_IMAGE,
                "command": ["sh", "-c", f"until nc -z {dep_name} {port}; do sleep 2; done"],
            }
        )
    return inits, notes


def _resources(name: str, config: dict[str, Any]) -> tuple[dict[str, dict[str, str]], str | None]:
    """Container resources from deploy.resources, filled in when the compose file has none."""
    deploy = _expect_mapping(config.get("deploy"), "deploy", name)
    resources_config = _expect_mapping(deploy.get("resources"), "deploy.resources", name)
    limits = _convert_resource_block(
        resources_config.get("limits"), "deploy.resources.limits", name
    )
    requests = _convert_resource_block(
        resources_config.get("reservations"), "deploy.resources.reservations", name
    )
    if limits:
        resources: dict[str, dict[str, str]] = {"limits": limits}
        if requests:
            resources["requests"] = requests
        return resources, None
    if requests:
        return {"requests": requests, "limits": dict(requests)}, (
            f"'{name}' had reservations but no limits: limits set equal to the reservations"
        )
    return {"requests": dict(DEFAULT_REQUESTS), "limits": dict(DEFAULT_LIMITS)}, (
        f"'{name}' had no resource limits: placeholder requests "
        f"({DEFAULT_REQUESTS['cpu']}, {DEFAULT_REQUESTS['memory']}) and limits "
        f"({DEFAULT_LIMITS['cpu']}, {DEFAULT_LIMITS['memory']}) were added; adjust them"
    )


def convert_service_to_deployment(
    name: str,
    config: dict[str, Any] | None,
    namespace: str = "default",
    services: dict[str, Any] | None = None,
    declared_volumes: dict[str, Any] | None = None,
    include_service: bool = True,
) -> dict[str, Any]:
    """Build a K8s apps/v1 Deployment for one compose service.

    Also returns, under the private key "_extra", the PVC names it needs and the notes it
    produced; convert_compose_to_k8s() collects those and strips the key.
    """
    config = config or {}
    k8s_name = to_k8s_name(name)
    labels = {"app": k8s_name}
    notes: list[str] = []

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

    resources, resources_note = _resources(name, config)
    container["resources"] = resources
    if resources_note:
        notes.append(resources_note)

    probe = convert_healthcheck(name, config)
    if probe is not None:
        container["livenessProbe"] = probe
        # A deep copy, or PyYAML would emit the shared `exec` dict as an anchor and alias.
        container["readinessProbe"] = copy.deepcopy(probe)

    volumes, mounts, claims, volume_notes = convert_volumes(name, config, declared_volumes or {})
    notes.extend(volume_notes)
    if mounts:
        container["volumeMounts"] = mounts

    pod: dict[str, Any] = {}
    inits, depends_notes = convert_depends_on(name, config, services or {}, include_service)
    notes.extend(depends_notes)
    if inits:
        pod["initContainers"] = inits
    pod["containers"] = [container]
    if volumes:
        pod["volumes"] = volumes

    deploy = _expect_mapping(config.get("deploy"), "deploy", name)
    replicas = deploy.get("replicas")
    replicas = 1 if replicas is None else _parse_replicas(replicas, name)

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": k8s_name, "namespace": namespace, "labels": dict(labels)},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": dict(labels)},
            "template": {"metadata": {"labels": dict(labels)}, "spec": pod},
        },
        "_extra": {"claims": claims, "notes": notes},
    }


def persistent_volume_claim(
    name: str, namespace: str, size: str = DEFAULT_VOLUME_SIZE
) -> dict[str, Any]:
    """A ReadWriteOnce PersistentVolumeClaim of `size`."""
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}},
    }


def namespace_manifest(namespace: str) -> dict[str, Any]:
    """A Namespace manifest."""
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}


def convert_service_to_k8s_service(
    name: str, config: dict[str, Any] | None, namespace: str = "default"
) -> dict[str, Any] | None:
    """Build a ClusterIP Service for the ports of a compose service, or None without ports."""
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
    """Things the conversion could not do for the user and that need a decision."""
    notes: list[str] = []
    k8s_name = to_k8s_name(name)
    if k8s_name != name:
        notes.append(f"'{name}' was renamed to '{k8s_name}' to be a valid K8s name")
    if "build" in config and not config.get("image"):
        notes.append(
            f"'{name}' uses 'build': image set to '{k8s_name}:latest'; "
            "build and push it to a registry your cluster can pull from"
        )
    if config.get("networks"):
        notes.append(
            f"'{name}' uses networks: Kubernetes services reach each other by name "
            "(e.g. http://db:5432), so the networks were left out"
        )
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

    Output is proportional to the input: a Namespace only when it is not `default`, a
    PersistentVolumeClaim per named volume, a Deployment per service, and a Service per
    service with ports.

    Returns:
        {"manifests": {"namespaces": [...], "persistent_volume_claims": [...],
                       "deployments": [...], "services": [...]},
         "notes": [...]}
    """
    services = compose_dict.get("services") if isinstance(compose_dict, dict) else None
    if not services or not isinstance(services, dict):
        raise ComposeError("Invalid compose file: no 'services' defined")
    declared_volumes = compose_dict.get("volumes") or {}
    if not isinstance(declared_volumes, dict):
        raise ComposeError("Invalid compose file: top-level 'volumes' must be a mapping")
    services = {str(name): (config or {}) for name, config in services.items()}

    deployments: list[dict[str, Any]] = []
    k8s_services: list[dict[str, Any]] = []
    notes: list[str] = []
    used_names: dict[str, str] = {}
    claim_users: dict[str, list[str]] = {}

    for name, config in services.items():
        k8s_name = to_k8s_name(name)
        if k8s_name in used_names:
            raise ComposeError(
                f"Services '{used_names[k8s_name]}' and '{name}' both map to K8s name '{k8s_name}'"
            )
        used_names[k8s_name] = name

        deployment = convert_service_to_deployment(
            name, config, namespace, services, declared_volumes, include_service
        )
        extra = deployment.pop("_extra")
        deployments.append(deployment)
        for claim in extra["claims"]:
            claim_users.setdefault(claim, [])
            if name not in claim_users[claim]:
                claim_users[claim].append(name)
        if include_service:
            service = convert_service_to_k8s_service(name, config, namespace)
            if service is not None:
                k8s_services.append(service)
        notes.extend(_service_notes(name, config))
        notes.extend(extra["notes"])

    claims = [persistent_volume_claim(claim, namespace) for claim in claim_users]
    for claim, users in claim_users.items():
        shared = f", shared by {', '.join(repr(u) for u in users)}" if len(users) > 1 else ""
        notes.append(
            f"volume '{claim}' became a PersistentVolumeClaim of {DEFAULT_VOLUME_SIZE} "
            f"(placeholder size, ReadWriteOnce{shared}); adjust it before applying"
        )
        if len(users) > 1:
            notes.append(
                f"'{claim}' is mounted by more than one service: with ReadWriteOnce they must run "
                "on the same node; use ReadWriteMany storage if they cannot"
            )

    namespaces = [] if namespace == "default" else [namespace_manifest(namespace)]
    return {
        "manifests": {
            "namespaces": namespaces,
            "persistent_volume_claims": claims,
            "deployments": deployments,
            "services": k8s_services,
        },
        "notes": notes,
    }


def manifests_to_yaml(manifests: dict[str, list[dict[str, Any]]]) -> str:
    """Render manifests as one multi-document YAML string, in apply order: Namespace,
    PersistentVolumeClaims, Deployments, Services."""
    documents = [doc for kind in MANIFEST_ORDER for doc in manifests.get(kind, [])]
    return yaml.safe_dump_all(documents, sort_keys=False, default_flow_style=False)
