"""Building one Terraform resource from one CloudFormation resource.

A converter takes properties out of `Props` one at a time (`take`, `attr`, `block`). What is
left when it returns was not converted: it stays in the output as a comment and is reported
in the notes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from converters.cfn import hcl
from converters.cfn.context import Context
from converters.cfn.expressions import intrinsic_name
from converters.cfn.models import BLANK, Attr, Block, Comment, Raw, ResourceInfo

# Marks "no value given", where None is a value.
MISSING: Any = object()
SNIPPET_LENGTH = 120


def snippet(value: Any) -> str:
    """A short JSON rendering of a value, for a comment."""
    text = json.dumps(value, default=str, separators=(", ", ": "))
    return text if len(text) <= SNIPPET_LENGTH else text[: SNIPPET_LENGTH - 3] + "..."


def tags_map(ctx: Context, value: Any) -> Any:
    """[{Key, Value}, ...] or {key: value} as a map. A computed list becomes a for
    expression."""
    if isinstance(value, dict) and intrinsic_name(value) is None:
        return {str(name): ctx.expr(item) for name, item in value.items()}
    literal_tags = isinstance(value, list) and all(
        isinstance(tag, dict) and "Key" in tag and intrinsic_name(tag) is None for tag in value
    )
    if literal_tags:
        result: dict[Any, Any] = {}
        for tag in value:
            tag_key = ctx.expr(tag["Key"])
            name = tag_key if isinstance(tag_key, str) else Raw(hcl.inline(tag_key))
            result[name] = ctx.expr(tag.get("Value", ""))
        return result
    return Raw(f"{{ for tag in {ctx.text(value)} : tag.Key => tag.Value }}")


class Props:
    """The properties of a resource, or of one nested CloudFormation structure, on their way
    to the items of an HCL body."""

    def __init__(self, ctx: Context, values: Any, where: str, owner: str) -> None:
        """Properties `values`, found at `where` ("MyBucket.VersioningConfiguration") in
        `owner` ("MyBucket (AWS::S3::Bucket)"); both are for notes."""
        self.ctx = ctx
        self.values: dict[str, Any] = dict(values) if isinstance(values, dict) else {}
        self.where = where
        self.owner = owner
        self.items: list[Any] = []

    # --- reading ------------------------------------------------------------------------

    def has(self, name: str) -> bool:
        """Whether the property is present with a value."""
        value = self.values.get(name, MISSING)
        return value is not MISSING and value is not None and not self.ctx.is_no_value(value)

    def peek(self, name: str, default: Any = None) -> Any:
        """A property, left in place."""
        return self.values.get(name, default)

    def take(self, name: str, default: Any = None) -> Any:
        """Remove and return a property (`default` when it is absent or AWS::NoValue)."""
        value = self.values.pop(name, MISSING)
        if value is MISSING or value is None or self.ctx.is_no_value(value):
            return default
        return value

    def literal(self, name: str, kind: type, value: Any = MISSING) -> Any:
        """A property that must be a literal list or mapping (`kind`) for the converter to
        look inside it, taken from the properties unless `value` is given. When it is a
        function (`!If`, `!Ref`...) instead, it is noted and kept as a comment and None is
        returned, so callers never mistake an intrinsic for data."""
        if value is MISSING:
            value = self.take(name)
        is_data = isinstance(value, kind) and not (
            isinstance(value, dict) and intrinsic_name(value) is not None
        )
        if value is None or is_data:
            return value
        self.ctx.note(
            f"{self.owner}: '{self.where}.{name}' is computed by a function and was not "
            "converted; set it by hand"
        )
        self.comment(f"{name} was not converted: {snippet(value)}")
        return None

    def drop(self, name: str) -> None:
        """Remove a property the converter covers another way."""
        self.values.pop(name, None)

    # --- value conversions, for attr() --------------------------------------------------

    def as_number(self, value: Any) -> Any:
        """A number from a number or numeric text; anything else as an expression."""
        if isinstance(value, str):
            try:
                number = float(value)
            except ValueError:
                return value
            return int(number) if number.is_integer() else number
        return self.ctx.expr(value)

    def as_bool(self, value: Any) -> Any:
        """A boolean from a boolean or "true"/"false"; anything else as an expression."""
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        return self.ctx.expr(value)

    def as_list(self, value: Any) -> Any:
        """A list, also when CloudFormation gives one value."""
        converted = self.ctx.expr(value)
        if isinstance(value, list) or isinstance(converted, Raw | list):
            return converted
        return [converted]

    # --- writing ------------------------------------------------------------------------

    def set(self, tf_name: str, value: Any) -> None:
        """Add `tf_name = value`."""
        self.items.append(Attr(tf_name, value))

    def comment(self, text: str) -> None:
        """Add a comment line."""
        self.items.append(Comment(text))

    def attr(
        self, tf_name: str, cfn_name: str, convert: Callable[[Any], Any] | None = None
    ) -> bool:
        """Copy a property to an attribute when it is present, through `convert` when given
        (default: as an expression). Returns whether it was present."""
        if not self.has(cfn_name):
            self.values.pop(cfn_name, None)
            return False
        value = self.values.pop(cfn_name)
        self.set(tf_name, convert(value) if convert else self.ctx.expr(value))
        return True

    def attrs(self, mapping: dict[str, str]) -> None:
        """Copy several properties: {tf_name: cfn_name}."""
        for tf_name, cfn_name in mapping.items():
            self.attr(tf_name, cfn_name)

    def number(self, tf_name: str, cfn_name: str) -> bool:
        """A numeric property, with numeric text turned into a number."""
        return self.attr(tf_name, cfn_name, self.as_number)

    def boolean(self, tf_name: str, cfn_name: str) -> bool:
        """A boolean property, with "true"/"false" turned into a boolean."""
        return self.attr(tf_name, cfn_name, self.as_bool)

    def json(self, tf_name: str, cfn_name: str) -> bool:
        """A JSON document property, as jsonencode()."""
        return self.attr(tf_name, cfn_name, self.ctx.json)

    def list_attr(self, tf_name: str, cfn_name: str) -> bool:
        """A property that is a list in Terraform, also when CloudFormation gives one value."""
        return self.attr(tf_name, cfn_name, self.as_list)

    def tags(self, cfn_name: str = "Tags", tf_name: str = "tags") -> None:
        """CloudFormation tags ([{Key, Value}] or a map) as a Terraform tags map."""
        if not self.has(cfn_name):
            self.values.pop(cfn_name, None)
            return
        self.set(tf_name, tags_map(self.ctx, self.values.pop(cfn_name)))

    def block(self, tf_name: str, cfn_name: str, fill: Callable[[Props], None]) -> bool:
        """A nested structure, or a list of them, as nested blocks filled by `fill`."""
        if not self.has(cfn_name):
            self.values.pop(cfn_name, None)
            return False
        value = self.values.pop(cfn_name)
        self.add_blocks(tf_name, value, fill, f"{self.where}.{cfn_name}")
        return True

    def add_blocks(
        self, tf_name: str, value: Any, fill: Callable[[Props], None], where: str
    ) -> None:
        """One block per structure in `value`. An `!If` with an AWS::NoValue branch becomes
        a dynamic block; any other function is noted and kept as a comment."""
        if isinstance(value, list):
            for index, item in enumerate(value):
                self.add_blocks(tf_name, item, fill, f"{where}[{index}]")
            return
        if isinstance(value, dict) and intrinsic_name(value) == "Fn::If":
            if self.dynamic_block(tf_name, value["Fn::If"], fill, where):
                return
        if not isinstance(value, dict) or intrinsic_name(value) is not None:
            self.ctx.note(
                f"{self.owner}: '{where}' is computed by a function, so it was not turned into "
                "Terraform blocks; set it by hand"
            )
            self.comment(f"{where.split('.', 1)[-1]} was not converted: {snippet(value)}")
            return
        inner = Props(self.ctx, value, where, self.owner)
        fill(inner)
        inner.finish()
        self.items.append(Block(tf_name, [], inner.items))

    def dynamic_block(
        self, tf_name: str, args: Any, fill: Callable[[Props], None], where: str
    ) -> bool:
        """`!If [Cond, {...}, !Ref AWS::NoValue]` as a dynamic block present only when the
        condition holds. Returns whether `args` had that shape."""
        if not isinstance(args, list) or len(args) != 3:
            return False
        name, yes, no = args
        if self.ctx.is_no_value(no) and isinstance(yes, dict):
            condition, body = self.ctx.condition(name), yes
        elif self.ctx.is_no_value(yes) and isinstance(no, dict):
            condition, body = f"!{self.ctx.condition(name)}", no
        else:
            return False
        inner = Props(self.ctx, body, where, self.owner)
        fill(inner)
        inner.finish()
        for_each = Attr("for_each", Raw(f"{condition} ? [1] : []"))
        content = Block("content", [], inner.items)
        self.items.append(Block("dynamic", [tf_name], [for_each, content]))
        return True

    def finish(self) -> None:
        """Report, and keep as comments, every property nobody took."""
        for name, value in self.values.items():
            if value is None or self.ctx.is_no_value(value):
                continue
            path = f"{self.where}.{name}"
            self.ctx.note(f"{self.owner}: property '{path}' was not converted; set it by hand")
            self.comment(f"{name} was not converted: {snippet(value)}")
        self.values.clear()


class Builder:
    """One CloudFormation resource on its way to one or more Terraform blocks."""

    def __init__(
        self,
        ctx: Context,
        info: ResourceInfo,
        resource: dict[str, Any],
        hints: dict[str, Any] | None = None,
    ) -> None:
        """A builder for `resource`, going to `info`. `hints` carry facts the SAM or CDK
        step found, such as a local code folder."""
        self.ctx = ctx
        self.info = info
        self.resource = resource
        self.hints: dict[str, Any] = hints if hints is not None else {}
        owner = f"{info.logical_id} ({info.cfn_type})"
        self.props = Props(ctx, resource.get("Properties") or {}, info.logical_id, owner)
        self.extra: list[Block] = []
        self.lifecycle: list[Any] = []
        # A condition the main resource exists under, on top of its own Condition.
        self.main_when: str | None = None

    @property
    def logical_id(self) -> str:
        """The resource's logical ID."""
        return self.info.logical_id

    def self_ref(self, attribute: str) -> Raw:
        """An expression reading one of this resource's attributes."""
        return Raw(self.info.expression(attribute))

    def count_items(self, when: str | None = None) -> list[Any]:
        """`count = ...` for a resource with a Condition, or one that exists only `when` an
        expression holds (an `!If [C, x, !Ref AWS::NoValue]` list item). None of either
        gives no items."""
        condition = self.resource.get("Condition")
        parts = [self.ctx.condition(condition)] if condition else []
        if when:
            parts.append(when if not parts else f"({when})")
        if not parts:
            return []
        return [Attr("count", Raw(f"{' && '.join(parts)} ? 1 : 0")), BLANK]

    def main_count_items(self) -> list[Any]:
        """The count items of the main resource."""
        return self.count_items(self.main_when)

    def name_attr(self, tf_name: str, cfn_name: str, lowercase: bool = False) -> None:
        """A name Terraform requires and CloudFormation generates when it is left out.
        CloudFormation uses '<stack>-<LogicalId>-<random>'; this uses '<stack>-<LogicalId>'."""
        if self.props.attr(tf_name, cfn_name):
            return
        self.ctx.stack_name_used = True
        suffix = self.logical_id.lower() if lowercase else self.logical_id
        self.props.set(tf_name, Raw(f'"${{var.stack_name}}-{hcl.template_text(suffix)}"'))
        self.ctx.note(
            f"{self.logical_id} had no {cfn_name}, so CloudFormation generated one; Terraform "
            f"needs it, so it is now '<stack_name>-{suffix}' (to adopt the existing resource, "
            "use its current name)"
        )

    def extra_name(self, suffix: str) -> str:
        """The main name plus a suffix, saying the main name once: a policy named
        'MyRolePolicy' on 'MyRole' is 'my_role_policy'."""
        if not suffix:
            return self.info.name
        repeated = f"_{self.info.name}_"
        if suffix.startswith(repeated):
            suffix = suffix[len(repeated) - 1 :]
        return f"{self.info.name}{suffix}"

    def add_data(self, kind: str, suffix: str, items: list[Any]) -> str:
        """A data source belonging to this resource, such as archive_file for Lambda code.
        Returns its address."""
        name = self.ctx.names_for(f"data.{kind}").take(self.extra_name(suffix))
        self.extra.append(Block("data", [kind, name], self.count_items() + items))
        index = "[0]" if self.resource.get("Condition") else ""
        return f"data.{kind}.{name}{index}"

    def add_resource(
        self,
        tf_type: str,
        suffix: str,
        items: list[Any],
        counted: bool = True,
        when: str | None = None,
    ) -> str:
        """An extra resource next to the main one, such as aws_s3_bucket_versioning. Returns
        its address. It shares the main resource's Condition unless counted=False; `when`
        adds a condition of its own (see ExpressionConverter.optional_item)."""
        name = self.ctx.names_for(tf_type).take(self.extra_name(suffix))
        body = (self.count_items(when) if counted else []) + items
        self.extra.append(Block("resource", [tf_type, name], body))
        return f"{tf_type}.{name}"
