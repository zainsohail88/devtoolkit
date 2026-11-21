"""Render converted manifests as files: plain Kubernetes YAML, or a Helm chart.

The output is proportional to the input: one file per resource that the compose file
needs, and nothing else. A Helm chart adds only Chart.yaml and a values.yaml holding the
things people change between environments (image, replicas, resources, storage size).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from converters.compose_constants import (
    CHART_NAME_INVALID,
    FILE_SUFFIX,
    MANIFEST_ORDER,
    OUTPUTS,
    SENTINEL,
)
from converters.compose_to_k8s import ComposeError, to_k8s_name


def render_files(
    result: dict[str, Any], output: str = "manifests", chart_name: str | None = None
) -> list[dict[str, str]]:
    """Turn a convert_compose_to_k8s() result into a list of {"path", "content"} files.

    output="manifests": one YAML file per resource, in apply order.
    output="helm": a chart with Chart.yaml, values.yaml and one template per resource.
    """
    if output not in OUTPUTS:
        raise ComposeError(f"Unknown output '{output}'. Use one of: {', '.join(OUTPUTS)}")
    manifests = result["manifests"]
    if output == "manifests":
        return [
            {"path": file_name(kind, resource), "content": dump(resource)}
            for kind in MANIFEST_ORDER
            for resource in manifests.get(kind, [])
        ]
    return HelmChart(manifests, chart_name or "app").render()


def file_name(kind: str, resource: dict[str, Any]) -> str:
    """The file name of one manifest: <name>-<kind suffix>.yaml."""
    return f"{resource['metadata']['name']}-{FILE_SUFFIX[kind]}.yaml"


def dump(document: Any) -> str:
    """One YAML document, keys in the order they were built."""
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def chart_name_from(compose: dict[str, Any]) -> str:
    """A chart name from the top-level `name` of the compose file, or 'app'."""
    raw = compose.get("name") if isinstance(compose, dict) else None
    if not raw:
        return "app"
    cleaned = CHART_NAME_INVALID.sub("-", str(raw).lower()).strip("-")
    return cleaned[:63].rstrip("-") or "app"


def without_namespace(resource: dict[str, Any]) -> dict[str, Any]:
    """Helm puts resources in the release namespace, so templates carry none."""
    copy = dict(resource)
    copy["metadata"] = {k: v for k, v in resource["metadata"].items() if k != "namespace"}
    return copy


def values_ref(section: str, name: str) -> str:
    """A reference to the values of `name` in `section`. Names can contain '-', which Go
    templates cannot take after a dot, so it uses index."""
    return f'(index .Values.{section} "{name}")'


class ResourcesBlock:
    """Swaps the `resources: <sentinel>` line of a deployment template for a toYaml block
    indented under the key."""

    def __init__(self, ref: str) -> None:
        """`ref`: the values reference of the service."""
        self.ref = ref

    def replace(self, match: re.Match[str]) -> str:
        """The block for one matched line; group 1 is its indentation."""
        indent = match.group(1)
        return (
            f"{indent}resources:\n"
            f"{indent}  {{{{- toYaml {self.ref}.resources | nindent {len(indent) + 2} }}}}"
        )


class HelmChart:
    """A Helm chart for converted manifests: Chart.yaml, values.yaml with what people
    change between environments, and one template per resource."""

    def __init__(self, manifests: dict[str, list[dict[str, Any]]], chart_name: str) -> None:
        """The manifests of a convert_compose_to_k8s() result and the chart name."""
        self.manifests = manifests
        self.chart_name = chart_name

    def render(self) -> list[dict[str, str]]:
        """Every file of the chart as {"path", "content"}."""
        values: dict[str, Any] = {}
        files: list[dict[str, str]] = []

        workloads: dict[str, Any] = {}
        for deployment in self.manifests.get("deployments", []):
            name = deployment["metadata"]["name"]
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            entry: dict[str, Any] = {
                "image": container["image"],
                "replicas": deployment["spec"]["replicas"],
            }
            if "resources" in container:
                entry["resources"] = container["resources"]
            workloads[name] = entry
            files.append(
                {
                    "path": f"templates/{name}-deployment.yaml",
                    "content": self.deployment_template(deployment, name),
                }
            )
        if workloads:
            values["services"] = workloads

        storage: dict[str, Any] = {}
        for pvc in self.manifests.get("persistent_volume_claims", []):
            name = pvc["metadata"]["name"]
            storage[name] = {"size": pvc["spec"]["resources"]["requests"]["storage"]}
            files.append(
                {"path": f"templates/{name}-pvc.yaml", "content": self.pvc_template(pvc, name)}
            )
        if storage:
            values["volumes"] = storage

        for service in self.manifests.get("services", []):
            name = service["metadata"]["name"]
            files.append(
                {
                    "path": f"templates/{name}-service.yaml",
                    "content": dump(without_namespace(service)),
                }
            )

        chart = {
            "apiVersion": "v2",
            "name": self.chart_name,
            "description": "Generated by DevToolkit from a docker-compose file",
            "type": "application",
            "version": "0.1.0",
            "appVersion": "0.1.0",
        }
        return [
            {"path": "Chart.yaml", "content": dump(chart)},
            {"path": "values.yaml", "content": dump(values)},
        ] + files

    def deployment_template(self, deployment: dict[str, Any], name: str) -> str:
        """A deployment with image, replicas and resources read from values.yaml."""
        doc = without_namespace(deployment)
        spec = dict(doc["spec"])
        template = dict(spec["template"])
        pod = dict(template["spec"])
        container = dict(pod["containers"][0])

        container["image"] = SENTINEL.format(kind="image", name=name)
        spec["replicas"] = SENTINEL.format(kind="replicas", name=name)
        has_resources = "resources" in container
        if has_resources:
            container["resources"] = SENTINEL.format(kind="resources", name=name)
        pod["containers"] = [container] + pod["containers"][1:]
        template["spec"] = pod
        spec["template"] = template
        doc["spec"] = spec

        text = dump(doc)
        ref = values_ref("services", name)
        text = text.replace(
            SENTINEL.format(kind="image", name=name), f"{{{{ {ref}.image | quote }}}}"
        )
        text = text.replace(
            SENTINEL.format(kind="replicas", name=name), f"{{{{ {ref}.replicas }}}}"
        )
        if has_resources:
            # The sentinel sits on its own line as `resources: __...__`.
            text = re.sub(
                r"^(\s*)resources: "
                + re.escape(SENTINEL.format(kind="resources", name=name))
                + "$",
                ResourcesBlock(ref).replace,
                text,
                flags=re.M,
            )
        return text

    def pvc_template(self, pvc: dict[str, Any], name: str) -> str:
        """A PersistentVolumeClaim with its size read from values.yaml."""
        doc = without_namespace(pvc)
        spec = dict(doc["spec"])
        resources = dict(spec["resources"])
        resources["requests"] = {"storage": SENTINEL.format(kind="storage", name=name)}
        spec["resources"] = resources
        doc["spec"] = spec
        text = dump(doc)
        return text.replace(
            SENTINEL.format(kind="storage", name=name),
            f"{{{{ {values_ref('volumes', name)}.size | quote }}}}",
        )


__all__ = ["OUTPUTS", "chart_name_from", "render_files", "to_k8s_name"]
