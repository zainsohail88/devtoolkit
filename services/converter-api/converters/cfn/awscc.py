"""Any CloudFormation resource type as an `awscc` provider resource.

The awscc provider is generated from the CloudFormation resource schemas, so its resources
are the CloudFormation types one to one: `AWS::S3::Bucket` is `awscc_s3_bucket`, property
`BucketName` is attribute `bucket_name`. awscc_index.json, built from the provider schema by
tools/build_awscc_index.py, lists the types awscc has and, per type, the property paths whose
keys stay as written (maps, "m"), the JSON document properties ("j"), the names outside the
usual snake_case rule ("r", "n"), the properties this provider version lacks ("x") and every
settable attribute ("a").
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from converters.cfn import hcl
from converters.cfn.builder import Builder, tags_map
from converters.cfn.expressions import intrinsic_name
from converters.cfn.models import Raw
from converters.cfn.naming import snake

INDEX_FILE = Path(__file__).with_name("awscc_index.json")
# Properties that hold Lambda code, for code the SAM or CDK step found in a local folder.
CODE_PROPERTIES = {"AWS::Lambda::Function": "Code", "AWS::Lambda::LayerVersion": "Content"}


@cache
def awscc_index() -> dict[str, dict[str, Any]]:
    """The index, read once."""
    return json.loads(INDEX_FILE.read_text(encoding="utf-8"))


def awscc_type(cfn_type: str) -> str | None:
    """The awscc resource type for a CloudFormation type, or None when awscc has none."""
    entry = awscc_index().get(cfn_type)
    if entry is None:
        return None
    if "n" in entry:
        return entry["n"]
    _, service, kind = cfn_type.split("::")
    return f"awscc_{service.lower()}_{snake(kind)}"


class AwsccShapes:
    """What the index says about one resource type."""

    def __init__(self, cfn_type: str) -> None:
        """The index entry of `cfn_type`."""
        entry = awscc_index().get(cfn_type, {})
        self.maps = set(entry.get("m", ()))
        self.json = set(entry.get("j", ()))
        self.renames: dict[str, str] = entry.get("r", {})
        self.missing = set(entry.get("x", ()))
        self.settable = set(entry.get("a", "").split())

    def name(self, path: str, prop: str) -> str:
        """The attribute name of the property at `path`."""
        return self.renames.get(path) or snake(prop)


class AwsccConverter:
    """Converts one resource with the awscc provider."""

    def __init__(self, builder: Builder) -> None:
        """Convert the resource `builder` holds."""
        self.b = builder
        self.ctx = builder.ctx
        self.shapes = AwsccShapes(builder.info.cfn_type)

    def convert(self) -> None:
        """Every property as an awscc attribute. Properties this awscc version lacks stay
        behind for Props.finish() to report."""
        code = CODE_PROPERTIES.get(self.b.info.cfn_type)
        if code and self.b.hints.get("code_path"):
            self.local_code(code)
        for prop in list(self.b.props.values):
            name = self.shapes.name(prop, prop)
            if prop in self.shapes.missing or name not in self.shapes.settable:
                continue
            value = self.b.props.take(prop)
            if value is not None:
                self.b.props.set(name, self.value(value, prop))

    def local_code(self, prop: str) -> None:
        """awscc reads Lambda code from S3 only, so code from a local folder becomes
        variables for the bucket and key the user uploads it to."""
        b = self.b
        b.props.drop(prop)
        path = b.hints["code_path"]
        where = f"zipped code of {b.logical_id} (from {path})"
        bucket = self.ctx.placeholder_variable(
            f"{b.logical_id}_code_bucket", f"The S3 bucket holding the {where}"
        )
        key = self.ctx.placeholder_variable(
            f"{b.logical_id}_code_key", f"The S3 key of the {where}"
        )
        b.props.set(snake(prop), {"s3_bucket": Raw(bucket), "s3_key": Raw(key)})
        self.ctx.note(
            f"{b.logical_id}: the awscc provider reads Lambda code only from S3. Zip {path}, "
            f"upload it and set {bucket} and {key}. The aws provider zips the code for you."
        )

    def value(self, value: Any, path: str) -> Any:
        """One property value, shaped the way the attribute at `path` expects."""
        if path in self.shapes.json:
            return self.ctx.json(value)
        if path in self.shapes.maps and isinstance(value, list):
            # Tags written as [{Key, Value}] for a resource whose awscc tags are a map.
            return tags_map(self.ctx, value)
        if isinstance(value, list):
            return self.sequence(value, path)
        if not isinstance(value, dict):
            return value
        function = intrinsic_name(value)
        if function == "Fn::If":
            return self.conditional(value[function], path, value)
        if function is not None:
            return self.ctx.expr(value)
        if path in self.shapes.maps:
            return {str(key): self.ctx.expr(item) for key, item in value.items()}
        return self.structure(value, path)

    def sequence(self, items: list[Any], path: str) -> Any:
        """A list. As in ExpressionConverter.sequence, NoValue items go and
        `!If [C, x, NoValue]` items are filtered."""
        kept = [item for item in items if not self.ctx.is_no_value(item)]
        converted = [self.value(item, path) for item in kept]
        if any(self.ctx.optional_item(item)[0] for item in kept):
            return Raw(f"[for item in {hcl.inline(converted)} : item if item != null]")
        return converted

    def conditional(self, args: Any, path: str, original: dict[str, Any]) -> Any:
        """An Fn::If with both branches shaped for this path, so their keys are named
        right."""
        if not isinstance(args, list) or len(args) != 3:
            return self.ctx.expr(original)
        name, yes, no = args
        yes_text = hcl.inline(self.value(yes, path))
        no_text = hcl.inline(self.value(no, path))
        return Raw(f"{self.ctx.condition(name)} ? {yes_text} : {no_text}")

    def structure(self, value: dict[str, Any], path: str) -> dict[str, Any]:
        """A nested object with attribute names from the index."""
        result: dict[str, Any] = {}
        for key, item in value.items():
            child = f"{path}.{key}"
            if self.ctx.is_no_value(item):
                continue
            if child in self.shapes.missing:
                self.ctx.note(
                    f"{self.b.logical_id} ({self.b.info.cfn_type}): {self.b.info.tf_type} in this "
                    f"awscc provider version has no argument for '{self.b.logical_id}.{child}', "
                    "so it was left out. Set it outside Terraform or try a newer provider "
                    "version."
                )
                continue
            result[self.shapes.name(child, str(key))] = self.value(item, child)
        return result
