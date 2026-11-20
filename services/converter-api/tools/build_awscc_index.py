"""Build converters/cfn/awscc_index.json from the awscc provider's schema.

The index lists every CloudFormation type the awscc provider has, and per type:
  "n": the awscc resource name, when it doesn't follow snake() (e.g. CloudFront words)
  "m": property paths that are maps (their keys stay as written)
  "j": property paths that are JSON documents (written with jsonencode)
  "r": property paths whose attribute name doesn't follow snake()
  "x": property paths CloudFormation has but this awscc version doesn't (left as notes)
  "a": every attribute a configuration can set, space-separated, so a property neither
       schema knows about is reported instead of written as an invalid argument
Paths are CloudFormation property names joined by dots ("Environment.Variables").

Run it when bumping the awscc provider version:

    mkdir -p /tmp/awscc && cd /tmp/awscc
    cat > main.tf <<'EOF'
    terraform {
      required_providers {
        awscc = { source = "hashicorp/awscc" }
      }
    }
    EOF
    terraform init && terraform providers schema -json > schema.json
    curl -sSfLO https://schema.cloudformation.us-east-1.amazonaws.com/CloudformationSchema.zip
    python tools/build_awscc_index.py /tmp/awscc/schema.json /tmp/awscc/CloudformationSchema.zip
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from converters.cfn.naming import snake  # noqa: E402

OUTPUT = Path(__file__).resolve().parents[1] / "converters" / "cfn" / "awscc_index.json"
_PROVIDER = "registry.terraform.io/hashicorp/awscc"
_MAX_DEPTH = 12


def _resolve(spec: Any, definitions: dict[str, Any]) -> dict[str, Any]:
    for _ in range(20):
        if not (isinstance(spec, dict) and "$ref" in spec):
            break
        ref = spec["$ref"]
        if not ref.startswith("#/definitions/"):
            return {}
        spec = definitions.get(ref.split("/")[-1], {})
    return spec if isinstance(spec, dict) else {}


def _is_object(spec: dict[str, Any]) -> bool:
    kind = spec.get("type")
    return kind == "object" or (isinstance(kind, list) and "object" in kind)


def _walk(
    props: dict[str, Any],
    definitions: dict[str, Any],
    attributes: dict[str, Any],
    path: list[str],
    entry: dict[str, Any],
    depth: int = 0,
) -> None:
    if depth > _MAX_DEPTH:
        return
    flat = {name.replace("_", ""): name for name in attributes}
    for prop, raw in props.items():
        full = ".".join([*path, prop])
        name = snake(prop)
        if name not in attributes:
            alternative = flat.get(prop.lower())
            if alternative is None:
                entry["x"].append(full)
                continue
            entry["r"][full] = alternative
            name = alternative
        attribute = attributes[name]
        spec = _resolve(raw, definitions)
        kind = attribute.get("type")
        if isinstance(kind, list) and kind[0] == "map":
            entry["m"].append(full)
            continue
        if kind == "string" and (
            _is_object(spec) or "oneOf" in spec or "anyOf" in spec or spec.get("type") == "array"
        ):
            entry["j"].append(full)
            continue
        if isinstance(kind, list) and kind[0] in ("list", "set") and kind[1] == "string":
            if _is_object(_resolve(spec.get("items", {}), definitions)):
                entry["j"].append(full)
            continue
        nested = attribute.get("nested_type")
        if not nested:
            continue
        if nested["nesting_mode"] == "map":
            entry["m"].append(full)
            continue
        inner = (
            _resolve(spec.get("items", {}), definitions) if spec.get("type") == "array" else spec
        )
        if "properties" in inner:
            _walk(
                inner["properties"],
                definitions,
                nested.get("attributes", {}),
                [*path, prop],
                entry,
                depth + 1,
            )


def build(provider_schema: Path, cloudformation_zip: Path) -> dict[str, Any]:
    resources = json.loads(provider_schema.read_text())["provider_schemas"][_PROVIDER][
        "resource_schemas"
    ]
    flat_types = {name.replace("_", ""): name for name in resources}
    index: dict[str, Any] = {}
    with zipfile.ZipFile(cloudformation_zip) as archive:
        for member in sorted(archive.namelist()):
            if not member.endswith(".json"):
                continue
            schema = json.loads(archive.read(member))
            cfn_type = schema["typeName"]
            _, service, kind = cfn_type.split("::")
            name = f"awscc_{service.lower()}_{snake(kind)}"
            entry: dict[str, Any] = {"m": [], "j": [], "r": {}, "x": []}
            if name not in resources:
                alternative = flat_types.get(name.replace("_", ""))
                if alternative is None:
                    continue
                name = entry["n"] = alternative
            _walk(
                schema.get("properties", {}),
                schema.get("definitions", {}),
                resources[name]["block"]["attributes"],
                [],
                entry,
            )
            settable = sorted(
                attribute
                for attribute, spec in resources[name]["block"]["attributes"].items()
                if spec.get("optional") or spec.get("required")
            )
            entry["a"] = " ".join(settable)
            index[cfn_type] = {
                key: sorted(set(value)) if isinstance(value, list) else value
                for key, value in entry.items()
                if value
            }
    return index


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    result = build(Path(sys.argv[1]), Path(sys.argv[2]))
    OUTPUT.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(result)} resource types written to {OUTPUT}")
