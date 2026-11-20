"""Read a CloudFormation, SAM or CDK-synthesized template (JSON or YAML) into a dict.

YAML short forms (`!Ref X`, `!GetAtt A.B`, `!Sub ...`) become the long JSON forms
(`{"Ref": "X"}`, `{"Fn::GetAtt": ["A", "B"]}`), so the converter only handles one shape.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

import yaml

from converters.compose_to_k8s import SafeMergeLoader, check_yaml_limits

# CloudFormation accepts up to 1 MB from S3. 500,000 characters covers large CDK stacks
# while keeping one request to well under a second of CPU.
MAX_TEMPLATE_CHARS = 500_000
MAX_NODES = 100_000
MAX_DEPTH = 100

SAM_TRANSFORM = "AWS::Serverless-2016-10-31"
LANGUAGE_EXTENSIONS = "AWS::LanguageExtensions"

_TOP_LEVEL = {
    "AWSTemplateFormatVersion",
    "Description",
    "Metadata",
    "Parameters",
    "Rules",
    "Mappings",
    "Conditions",
    "Transform",
    "Resources",
    "Outputs",
    "Globals",
    "Hooks",
}

_FUNCTIONS = {
    "And",
    "Base64",
    "Cidr",
    "Equals",
    "FindInMap",
    "ForEach",
    "GetAtt",
    "GetAZs",
    "If",
    "ImportValue",
    "Join",
    "Length",
    "Not",
    "Or",
    "Select",
    "Split",
    "Sub",
    "ToJsonString",
    "Transform",
}


class CfnError(ValueError):
    """Raised when a template cannot be read or converted.

    `message` describes the user's input and is safe to show them. `line` and `column` are
    1-based, set for syntax errors.
    """

    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column


class _CfnLoader(SafeMergeLoader):
    """Safe YAML loader for CloudFormation: short-form tags, and YAML 1.2 scalars.

    Under YAML 1.1 (PyYAML's default) `2010-09-09` is a date, `on` is True and `0755` is 493.
    CloudFormation keeps those as strings, so only true/false, null and plain decimal
    numbers are typed here.
    """


_NULL = re.compile(r"^(?:~|null|Null|NULL|)$")
_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_INT = re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$")
_FLOAT = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$")

_CfnLoader.yaml_implicit_resolvers = {}
for _tag, _pattern, _first in (
    ("tag:yaml.org,2002:null", _NULL, ["~", "n", "N", ""]),
    ("tag:yaml.org,2002:bool", _BOOL, list("tTfF")),
    ("tag:yaml.org,2002:int", _INT, list("-+0123456789")),
    ("tag:yaml.org,2002:float", _FLOAT, list("-+.0123456789")),
    ("tag:yaml.org,2002:merge", re.compile(r"^(?:<<)$"), ["<"]),
):
    _CfnLoader.add_implicit_resolver(_tag, _pattern, _first)


def _construct_bool(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> bool:
    return loader.construct_scalar(node).lower() == "true"


_CfnLoader.add_constructor("tag:yaml.org,2002:bool", _construct_bool)


def _construct_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> dict[str, Any]:
    """`!Name value` -> `{"Fn::Name": value}` (`!Ref` and `!Condition` keep their name)."""
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    if suffix == "Ref":
        return {"Ref": value}
    if suffix == "Condition":
        return {"Condition": value}
    if suffix not in _FUNCTIONS:
        mark = node.start_mark
        raise CfnError(f"Unknown YAML tag '!{suffix}'", mark.line + 1, mark.column + 1)
    if suffix == "GetAtt" and isinstance(value, str):
        # Short form `!GetAtt Resource.Attribute.With.Dots`: the first dot splits.
        resource, _, attribute = value.partition(".")
        value = [resource, attribute]
    return {f"Fn::{suffix}": value}


_CfnLoader.add_multi_constructor("!", _construct_tag)


def _syntax_error(exc: yaml.YAMLError) -> CfnError:
    # Only the parser's description of the input reaches the user, never the exception text.
    problem = getattr(exc, "problem", None) or "syntax error"
    mark = getattr(exc, "problem_mark", None)
    line = mark.line + 1 if mark is not None else None
    column = mark.column + 1 if mark is not None else None
    return CfnError(f"Invalid YAML: {problem}", line, column)


# CloudFormation's rule for logical IDs, and for parameter, condition, mapping and output
# names. Enforcing it also keeps every name safe to use in generated HCL.
_NAME = re.compile(r"^[A-Za-z0-9]{1,255}$")


def _check_shape(data: Any) -> None:
    """Depth and size limits on loaded data, without recursion. Infinite or NaN numbers
    (YAML `.inf`, `1e400`) are rejected: no CloudFormation value is one."""
    stack: list[tuple[Any, int]] = [(data, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES:
            raise CfnError(f"Template too large: more than {MAX_NODES:,} values")
        if depth > MAX_DEPTH:
            raise CfnError("Template nested too deeply")
        if isinstance(node, float) and not math.isfinite(node):
            raise CfnError(f"Invalid number in template: {node}")
        if isinstance(node, dict):
            stack.extend((k, depth + 1) for k in node)
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)


def load_template(text: str) -> dict[str, Any]:
    """Parse template text (JSON or YAML) and check the parts the converter relies on.

    Raises CfnError with a message for the user.
    """
    if not text or not text.strip():
        raise CfnError("Input is empty")
    if len(text) > MAX_TEMPLATE_CHARS:
        raise CfnError(f"Template is longer than {MAX_TEMPLATE_CHARS:,} characters")
    stripped = text.lstrip("﻿ \t\r\n")
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            offset = len(text) - len(stripped)
            line = exc.lineno + text[:offset].count("\n")
            raise CfnError(f"Invalid JSON: {exc.msg}", line, exc.colno) from exc
        except RecursionError as exc:
            raise CfnError("Template nested too deeply") from exc
    else:
        try:
            check_yaml_limits(text, max_depth=MAX_DEPTH, max_nodes=MAX_NODES)
            loader = _CfnLoader(text)
            try:
                data = loader.get_single_data()
            finally:
                loader.dispose()
        except yaml.YAMLError as exc:
            raise _syntax_error(exc) from exc
        except ValueError as exc:
            # check_yaml_limits raises ComposeError, a ValueError with a user-safe message.
            if isinstance(exc, CfnError):
                raise
            raise CfnError(getattr(exc, "message", "Template too large")) from exc
    _check_shape(data)
    validate_template(data)
    return data


def validate_template(data: Any) -> None:
    """The structure every template needs: a mapping with a non-empty Resources mapping,
    each resource a mapping with a Type."""
    if not isinstance(data, dict):
        raise CfnError("Invalid template: the top level must be a mapping")
    resources = data.get("Resources")
    if not resources:
        raise CfnError("Invalid template: no 'Resources' defined")
    if not isinstance(resources, dict):
        raise CfnError("Invalid template: 'Resources' must be a mapping")
    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            raise CfnError(f"Invalid template: resource '{logical_id}' must be a mapping")
        kind = resource.get("Type")
        if not isinstance(kind, str) or not kind:
            raise CfnError(f"Invalid template: resource '{logical_id}' has no 'Type'")
        properties = resource.get("Properties")
        if properties is not None and not isinstance(properties, dict):
            raise CfnError(f"Invalid template: 'Properties' of '{logical_id}' must be a mapping")
    for section in ("Parameters", "Mappings", "Conditions", "Outputs"):
        value = data.get(section)
        if value is not None and not isinstance(value, dict):
            raise CfnError(f"Invalid template: '{section}' must be a mapping")
    for name, parameter in (data.get("Parameters") or {}).items():
        if not isinstance(parameter, dict) or not isinstance(parameter.get("Type"), str):
            raise CfnError(f"Invalid template: parameter '{name}' needs a 'Type'")
    for name, output in (data.get("Outputs") or {}).items():
        if not isinstance(output, dict) or "Value" not in output:
            raise CfnError(f"Invalid template: output '{name}' needs a 'Value'")
    for section in ("Resources", "Parameters", "Conditions", "Mappings", "Outputs"):
        for name in data.get(section) or {}:
            if not isinstance(name, str) or not _NAME.match(name):
                raise CfnError(
                    f"Invalid template: '{name}' in {section} must be letters and digits only "
                    "(A-Z, a-z, 0-9)"
                )


def transforms(template: dict[str, Any]) -> list[str]:
    value = template.get("Transform")
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [item if isinstance(item, str) else "AWS::Include" for item in items]


def detect_source(template: dict[str, Any]) -> str:
    """'sam', 'cdk' or 'cloudformation'."""
    if SAM_TRANSFORM in transforms(template):
        return "sam"
    for resource in template["Resources"].values():
        if resource.get("Type") == "AWS::CDK::Metadata":
            return "cdk"
        metadata = resource.get("Metadata")
        if isinstance(metadata, dict) and "aws:cdk:path" in metadata:
            return "cdk"
    return "cloudformation"


def unknown_sections(template: dict[str, Any]) -> list[str]:
    return [str(name) for name in template if name not in _TOP_LEVEL]
