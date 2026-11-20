"""Any CloudFormation resource type as an `awscc` provider resource.

The awscc provider is generated from the CloudFormation resource schemas, so its resources
are the CloudFormation types one-to-one: `AWS::S3::Bucket` is `awscc_s3_bucket`, property
`BucketName` is attribute `bucket_name`. awscc_index.json (built from the provider's schema by
tools/build_awscc_index.py) lists the types awscc has, and, per type, the property paths whose
keys must stay as written (maps, "m"), the JSON-document properties ("j"), and the few names
that don't follow the usual snake_case rule ("r", "n").
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from converters.cfn import hcl
from converters.cfn.builder import Builder, tags_map
from converters.cfn.context import intrinsic_name, json_value
from converters.cfn.hcl import Raw
from converters.cfn.naming import snake

_INDEX_FILE = Path(__file__).with_name("awscc_index.json")


@cache
def _index() -> dict[str, dict[str, Any]]:
    return json.loads(_INDEX_FILE.read_text(encoding="utf-8"))


def awscc_type(cfn_type: str) -> str | None:
    """The awscc resource type for a CloudFormation type, or None if awscc has none."""
    entry = _index().get(cfn_type)
    if entry is None:
        return None
    if "n" in entry:
        return entry["n"]
    _, service, kind = cfn_type.split("::")
    return f"awscc_{service.lower()}_{snake(kind)}"


class _Shapes:
    def __init__(self, cfn_type: str) -> None:
        entry = _index().get(cfn_type, {})
        self.maps = set(entry.get("m", ()))
        self.json = set(entry.get("j", ()))
        self.renames: dict[str, str] = entry.get("r", {})
        self.missing = set(entry.get("x", ()))
        self.settable = set(entry.get("a", "").split())

    def name(self, path: str, prop: str) -> str:
        return self.renames.get(path) or snake(prop)


# Properties that hold Lambda code, for code the SAM or CDK step found in a local folder.
_CODE_PROPERTIES = {"AWS::Lambda::Function": "Code", "AWS::Lambda::LayerVersion": "Content"}


def convert_awscc(b: Builder) -> None:
    """Every property of the resource as an awscc attribute. Properties this awscc version
    doesn't have stay behind for Props.finish() to report."""
    shapes = _Shapes(b.info.cfn_type)
    code = _CODE_PROPERTIES.get(b.info.cfn_type)
    if code and b.hints.get("code_path"):
        _local_code(b, code)
    for prop in list(b.props.values):
        if prop in shapes.missing or shapes.name(prop, prop) not in shapes.settable:
            continue
        value = b.props.take(prop)
        if value is None:
            continue
        b.props.set(shapes.name(prop, prop), _value(b, shapes, value, prop))


def _local_code(b: Builder, prop: str) -> None:
    """awscc reads Lambda code from S3 only, so code from a local folder becomes variables
    for the bucket and key the user uploads it to."""
    b.props.drop(prop)
    path = b.hints["code_path"]
    where = f"the S3 {{}} holding the zipped code of {b.logical_id} (from {path})"
    bucket = b.ctx.placeholder_variable(f"{b.logical_id}_code_bucket", where.format("bucket"))
    key = b.ctx.placeholder_variable(f"{b.logical_id}_code_key", where.format("key"))
    b.props.set(snake(prop), {"s3_bucket": Raw(bucket), "s3_key": Raw(key)})
    b.ctx.note(
        f"{b.logical_id}: the awscc provider takes Lambda code from S3 only; zip {path}, upload "
        f"it, and set {bucket} and {key} (or use the aws provider, which zips it for you)"
    )


def _value(b: Builder, shapes: _Shapes, value: Any, path: str) -> Any:
    ctx = b.ctx
    if path in shapes.json:
        return json_value(ctx, value)
    if path in shapes.maps and isinstance(value, list):
        # Tags written as [{Key, Value}] for a resource whose awscc tags are a map.
        return tags_map(ctx, value)
    if isinstance(value, list):
        # As in Context.expr: NoValue items go, and `!If [C, x, NoValue]` items are filtered.
        kept = [item for item in value if not ctx.is_no_value(item)]
        items = [_value(b, shapes, item, path) for item in kept]
        if any(ctx.optional_item(item)[0] for item in kept):
            return Raw(f"[for item in {hcl.inline(items)} : item if item != null]")
        return items
    if not isinstance(value, dict):
        return value
    function = intrinsic_name(value)
    if function == "Fn::If" and isinstance(value[function], list) and len(value[function]) == 3:
        # Convert both branches with this path's shape, so their keys are named right.
        name, yes, no = value[function]
        yes_text = hcl.inline(_value(b, shapes, yes, path))
        no_text = hcl.inline(_value(b, shapes, no, path))
        return Raw(f"{ctx.condition(name)} ? {yes_text} : {no_text}")
    if function is not None:
        return ctx.expr(value)
    if path in shapes.maps:
        return {str(k): ctx.expr(v) for k, v in value.items()}
    result: dict[str, Any] = {}
    for key, item in value.items():
        child = f"{path}.{key}"
        if ctx.is_no_value(item):
            continue
        if child in shapes.missing:
            b.ctx.note(
                f"{b.info.logical_id} ({b.info.cfn_type}): property '{b.logical_id}.{child}' "
                f"is not in {b.info.tf_type} yet and was left out; set it by hand"
            )
            continue
        result[shapes.name(child, str(key))] = _value(b, shapes, item, child)
    return result
