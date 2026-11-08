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


def _load_yaml(text: str, loader_class: type[yaml.SafeLoader]) -> Any:
    """Build the single document in `text` with one of the safe loaders above.

    This is what yaml.load() does internally, spelled out so the loader is visibly a
    SafeLoader subclass: it can only build the standard YAML types (mappings, lists,
    strings, numbers, booleans, null), never arbitrary Python objects.
    """
    loader = loader_class(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def load_yaml_documents(text: str) -> list[Any]:
    """Build every document in a multi-document YAML string (separated by ---).

    Uses SafeMergeLoader: safe types only, and linear-time `<<` merges. Raises yaml.YAMLError
    on syntax errors.
    """
    loader = SafeMergeLoader(text)
    try:
        documents: list[Any] = []
        while loader.check_data():
            documents.append(loader.get_data())
        return documents
    finally:
        loader.dispose()


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
# Applied to a stripped value. No two adjacent repetitions can match the same character, so
# matching stays linear however long the input is.
_MEMORY_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)[ \t]*([a-zA-Z]*)$")


class ComposeError(ValueError):
    """Raised when the compose input cannot be parsed or converted.

    `message` is written for the user and is safe to send back to them: it describes their
    input and never includes internal details.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_compose(yaml_string: str) -> dict[str, Any]:
    """Parse a docker-compose YAML string and check it has a usable `services` section.

    Raises:
        ComposeError: if the YAML is invalid, not a mapping, or has no services.
    """
    if not yaml_string or not yaml_string.strip():
        raise ComposeError("Input is empty")

    try:
        check_yaml_limits(yaml_string)
        data = _load_yaml(yaml_string, _ComposeLoader)
    except yaml.YAMLError as exc:
        # `problem` is the parser's description of the input ("mapping values are not
        # allowed here"). Nothing else from the exception reaches the user.
        problem = getattr(exc, "problem", None) or "syntax error"
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
    match = _MEMORY_PATTERN.match(str(value).strip())
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


# Placeholder resources, added when a service declares none. Kubernetes schedules an
# unbounded pod otherwise; the note tells the user these are starting points.
DEFAULT_REQUESTS = {"cpu": "100m", "memory": "128Mi"}
DEFAULT_LIMITS = {"cpu": "500m", "memory": "512Mi"}
# Named volumes become PersistentVolumeClaims of this size; the note says to adjust it.
DEFAULT_VOLUME_SIZE = "1Gi"
# Init containers that wait for a dependency's port. Pinned, tiny, has `nc`.
WAIT_IMAGE = "busybox:1.37"

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|us|µs|ns|h|m|s)")
_DURATION_SECONDS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}
_BIND_PREFIXES = ("/", "./", "../", "~")


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
        matches = _DURATION.findall(text)
        if not text or "".join(n + u for n, u in matches) != text.replace(" ", ""):
            raise ComposeError(f"Service '{service}': invalid duration in '{field}': '{value}'")
        seconds = sum(float(n) * _DURATION_SECONDS[u] for n, u in matches)
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
        if isinstance(retries, bool) or not _DIGITS.fullmatch(text) or int(text) < 1:
            raise ComposeError(f"Service '{name}': invalid healthcheck.retries: '{retries}'")
        probe["failureThreshold"] = int(text)
    if check.get("start_period") is not None:
        probe["initialDelaySeconds"] = parse_duration(
            check["start_period"], "healthcheck.start_period", name
        )
    return probe


def _mount_name(target: str) -> str:
    cleaned = _INVALID_NAME_CHARS.sub("-", target.lower()).strip("-")
    return (cleaned or "data")[:_DNS_LABEL_MAX].rstrip("-")


def convert_volumes(
    name: str, config: dict[str, Any], declared: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    """Compose `volumes` of one service -> (pod volumes, volumeMounts, PVC names, notes).

    Named volumes become PersistentVolumeClaims (one per volume name, shared across
    services). Anonymous volumes and tmpfs become emptyDir. Bind mounts have no Kubernetes
    equivalent and are reported as notes.
    """
    entries = _expect_list(config.get("volumes"), "volumes", name)
    volumes: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    claims: list[str] = []
    notes: list[str] = []
    used: set[str] = set()

    def add(volume: dict[str, Any], target: str, read_only: bool) -> None:
        # The same volume mounted at two paths is one pod volume with two mounts.
        if volume["name"] not in used:
            used.add(volume["name"])
            volumes.append(volume)
        mount: dict[str, Any] = {"name": volume["name"], "mountPath": target}
        if read_only:
            mount["readOnly"] = True
        mounts.append(mount)

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
                kind = "bind" if source.startswith(_BIND_PREFIXES) else "volume"
            else:
                raise ComposeError(f"Service '{name}': invalid volume entry '{entry}'")
        if not target.startswith("/"):
            raise ComposeError(
                f"Service '{name}': volume target must be an absolute path: '{target}'"
            )

        if kind == "bind":
            notes.append(
                f"'{name}' bind mount '{source}:{target}' was not converted: Kubernetes has no "
                "host paths; use a ConfigMap, bake it into the image, or a PersistentVolumeClaim"
            )
        elif kind == "tmpfs":
            add({"name": _mount_name(target), "emptyDir": {"medium": "Memory"}}, target, False)
        elif kind == "volume" and not source:
            add({"name": _mount_name(target), "emptyDir": {}}, target, read_only)
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
            add({"name": claim, "persistentVolumeClaim": {"claimName": claim}}, target, read_only)
        else:
            notes.append(f"'{name}' volume type '{kind}' is not supported and was skipped")
    return volumes, mounts, claims, notes


def convert_depends_on(
    name: str, config: dict[str, Any], services: dict[str, Any], include_service: bool
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compose `depends_on` -> init containers that wait for each dependency's first port.

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
                "container was added; make '{name}' retry its connection"
            )
            continue
        port = ports[0]["service_port"]
        inits.append(
            {
                "name": f"wait-for-{dep_name}"[:_DNS_LABEL_MAX].rstrip("-"),
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
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}},
    }


def namespace_manifest(namespace: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}


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
            f"'{name}' uses networks: not needed in Kubernetes, services reach each other "
            "by name (e.g. http://db:5432)"
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

    Output is proportional to the input: a Namespace only when it isn't `default`, a
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
                "on the same node; use ReadWriteMany storage if they can't"
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
    documents = [
        doc
        for kind in ("namespaces", "persistent_volume_claims", "deployments", "services")
        for doc in manifests.get(kind, [])
    ]
    return yaml.safe_dump_all(documents, sort_keys=False, default_flow_style=False)
