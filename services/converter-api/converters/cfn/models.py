"""The data the converter works with: the HCL syntax tree it writes, and where each
CloudFormation parameter and resource ends up in Terraform."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Raw:
    """An HCL expression, written exactly as given: `var.name`, `"${var.a}-x"`."""

    text: str


@dataclass(frozen=True)
class Call:
    """`fn(arg)`, with `arg` rendered as an HCL value, so it can span several lines."""

    fn: str
    arg: Any


@dataclass(frozen=True)
class Comment:
    """A `#` comment line in a body or an object."""

    text: str


@dataclass
class Attr:
    """`name = value` in a body."""

    name: str
    value: Any


@dataclass
class Block:
    """`type "label" ... { body }`. The body holds Attr, Block and Comment items."""

    type: str
    labels: list[str] = field(default_factory=list)
    body: list[Any] = field(default_factory=list)


# A blank line between groups of body items. A comment with this text is never written.
BLANK = Comment("\0blank")


@dataclass(frozen=True)
class Expression:
    """An HCL expression placed in a template by the SAM or CDK step, such as
    `file("${path.module}/api.yaml")`. JSON and YAML have no way to make this type, so only
    the converter itself can place one."""

    text: str


@dataclass
class ParamInfo:
    """Where a CloudFormation parameter ends up."""

    name: str  # Terraform variable name
    cfn_type: str
    tf_type: str
    ssm: str | None = None  # data source name when the value comes from SSM


@dataclass
class ResourceInfo:
    """Where a CloudFormation resource ends up in Terraform."""

    logical_id: str
    cfn_type: str
    tf_type: str  # e.g. "aws_s3_bucket"; "" when it has no Terraform resource
    name: str  # Terraform resource name
    ref: str = "id"  # attribute that `Ref` returns
    attrs: dict[str, str] = field(default_factory=dict)  # Fn::GetAtt name -> attribute
    kind: str = "aws"  # "aws", "awscc" or "unsupported"
    conditional: bool = False
    # Per-attribute overrides: a whole expression in place of `address.attribute`.
    overrides: dict[str, str] = field(default_factory=dict)

    @property
    def address(self) -> str:
        """`<type>.<name>`, as Terraform addresses the resource."""
        return f"{self.tf_type}.{self.name}"

    def expression(self, attribute: str) -> str:
        """The expression that reads `attribute` of the resource, with `[0]` when the
        resource has a count."""
        if attribute in self.overrides:
            return self.overrides[attribute]
        index = "[0]" if self.conditional else ""
        return f"{self.address}{index}.{attribute}"
