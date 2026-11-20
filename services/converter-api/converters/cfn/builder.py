"""Building one Terraform resource from one CloudFormation resource.

A converter takes properties out of `Props` one at a time (`take`, `attr`, `block`). Whatever
is left when it returns was not converted: it is kept in the output as a comment and
reported in the notes, never dropped silently.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from converters.cfn import hcl
from converters.cfn.context import Context, ResourceInfo, intrinsic_name, json_value
from converters.cfn.hcl import Raw

MISSING: Any = object()
_SNIPPET = 120


def snippet(value: Any) -> str:
    text = json.dumps(value, default=str, separators=(", ", ": "))
    return text if len(text) <= _SNIPPET else text[: _SNIPPET - 3] + "..."


class Props:
    """The properties of a resource (or of one nested CloudFormation structure) being turned
    into the items of an HCL body."""

    def __init__(self, ctx: Context, values: Any, where: str, owner: str) -> None:
        self.ctx = ctx
        self.values: dict[str, Any] = dict(values) if isinstance(values, dict) else {}
        self.where = where  # "MyBucket.VersioningConfiguration", for notes
        self.owner = owner  # "MyBucket (AWS::S3::Bucket)"
        self.items: list[Any] = []

    # --- reading ----------------------------------------------------------------------

    def has(self, name: str) -> bool:
        value = self.values.get(name, MISSING)
        return value is not MISSING and value is not None and not self.ctx.is_no_value(value)

    def peek(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def take(self, name: str, default: Any = None) -> Any:
        """Remove and return a property (default when it is absent or AWS::NoValue)."""
        value = self.values.pop(name, MISSING)
        if value is MISSING or value is None or self.ctx.is_no_value(value):
            return default
        return value

    def literal(self, name: str, kind: type, value: Any = MISSING) -> Any:
        """A property that must be a literal list or mapping (`kind`) for the converter to
        look inside it. Taken from the properties unless `value` is given. When it is a
        function (`!If`, `!Ref`...) instead, it is noted and kept as a comment, and None is
        returned, so callers never mistake an intrinsic for data."""
        if value is MISSING:
            value = self.take(name)
        if value is None or (
            isinstance(value, kind)
            and not (isinstance(value, dict) and intrinsic_name(value) is not None)
        ):
            return value
        self.ctx.note(
            f"{self.owner}: '{self.where}.{name}' is computed by a function and was not "
            "converted; set it by hand"
        )
        self.comment(f"{name} was not converted: {snippet(value)}")
        return None

    def drop(self, *names: str) -> None:
        """Properties that need no Terraform counterpart (the converter covers them)."""
        for name in names:
            self.values.pop(name, None)

    # --- writing ----------------------------------------------------------------------

    def set(self, tf_name: str, value: Any) -> None:
        self.items.append(hcl.Attr(tf_name, value))

    def attr(
        self,
        tf_name: str,
        cfn_name: str,
        convert: Callable[[Any], Any] | None = None,
    ) -> bool:
        """Copy a property to an attribute when present. Returns whether it was."""
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
        return self.attr(tf_name, cfn_name, lambda v: _as_number(self.ctx, v))

    def boolean(self, tf_name: str, cfn_name: str) -> bool:
        return self.attr(tf_name, cfn_name, lambda v: _as_bool(self.ctx, v))

    def json(self, tf_name: str, cfn_name: str) -> bool:
        return self.attr(tf_name, cfn_name, lambda v: json_value(self.ctx, v))

    def list_attr(self, tf_name: str, cfn_name: str) -> bool:
        """A property that is a list in Terraform, even when CloudFormation gives one value."""

        def convert(value: Any) -> Any:
            converted = self.ctx.expr(value)
            if isinstance(value, list) or isinstance(converted, Raw | list):
                return converted
            return [converted]

        return self.attr(tf_name, cfn_name, convert)

    def tags(self, cfn_name: str = "Tags", tf_name: str = "tags") -> None:
        """CloudFormation tags ([{Key, Value}] or a map) as a Terraform tags map."""
        if not self.has(cfn_name):
            self.values.pop(cfn_name, None)
            return
        self.set(tf_name, tags_map(self.ctx, self.values.pop(cfn_name)))

    def comment(self, text: str) -> None:
        self.items.append(hcl.Comment(text))

    def block(
        self,
        tf_name: str,
        cfn_name: str,
        fill: Callable[[Props], None],
    ) -> bool:
        """A nested structure (or a list of them) as nested blocks, filled by `fill`."""
        if not self.has(cfn_name):
            self.values.pop(cfn_name, None)
            return False
        value = self.values.pop(cfn_name)
        self.add_blocks(tf_name, value, fill, f"{self.where}.{cfn_name}")
        return True

    def add_blocks(
        self, tf_name: str, value: Any, fill: Callable[[Props], None], where: str
    ) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                self.add_blocks(tf_name, item, fill, f"{where}[{index}]")
            return
        if isinstance(value, dict) and intrinsic_name(value) == "Fn::If":
            if self._dynamic_block(tf_name, value["Fn::If"], fill, where):
                return
        if not isinstance(value, dict) or intrinsic_name(value) is not None:
            self.ctx.note(
                f"{self.owner}: '{where}' is computed by a function and could not be turned "
                "into Terraform blocks; set it by hand"
            )
            self.comment(f"{where.split('.', 1)[-1]} was not converted: {snippet(value)}")
            return
        inner = Props(self.ctx, value, where, self.owner)
        fill(inner)
        inner.finish()
        self.items.append(hcl.Block(tf_name, [], inner.items))

    def _dynamic_block(
        self, tf_name: str, args: Any, fill: Callable[[Props], None], where: str
    ) -> bool:
        """`!If [Cond, {...}, !Ref AWS::NoValue]` as a dynamic block present only when the
        condition holds."""
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
        self.items.append(
            hcl.Block(
                "dynamic",
                [tf_name],
                [
                    hcl.Attr("for_each", Raw(f"{condition} ? [1] : []")),
                    hcl.Block("content", [], inner.items),
                ],
            )
        )
        return True

    def finish(self) -> None:
        """Report and keep (as comments) every property nobody took."""
        for name, value in self.values.items():
            if value is None or self.ctx.is_no_value(value):
                continue
            path = f"{self.where}.{name}"
            self.ctx.note(f"{self.owner}: property '{path}' was not converted; set it by hand")
            self.comment(f"{name} was not converted: {snippet(value)}")
        self.values.clear()


def _as_number(ctx: Context, value: Any) -> Any:
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    return ctx.expr(value)


def _as_bool(ctx: Context, value: Any) -> Any:
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return ctx.expr(value)


def tags_map(ctx: Context, value: Any) -> Any:
    """[{Key, Value}, ...] or {key: value} as a map. A computed list becomes a for expression."""
    if isinstance(value, dict) and intrinsic_name(value) is None:
        return {str(k): ctx.expr(v) for k, v in value.items()}
    if isinstance(value, list) and all(
        isinstance(tag, dict) and "Key" in tag and intrinsic_name(tag) is None for tag in value
    ):
        result: dict[Any, Any] = {}
        for tag in value:
            tag_key = ctx.expr(tag["Key"])
            result[tag_key if isinstance(tag_key, str) else Raw(hcl.inline(tag_key))] = ctx.expr(
                tag.get("Value", "")
            )
        return result
    return Raw(f"{{ for tag in {ctx.text(value)} : tag.Key => tag.Value }}")


@dataclass
class Builder:
    """One CloudFormation resource on its way to one or more Terraform blocks."""

    ctx: Context
    info: ResourceInfo
    resource: dict[str, Any]
    props: Props = field(init=False)
    extra: list[hcl.Block] = field(default_factory=list)
    lifecycle: list[Any] = field(default_factory=list)
    # Top-level properties covered elsewhere (a SAM or CDK step) but read by converters.
    hints: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        owner = f"{self.info.logical_id} ({self.info.cfn_type})"
        self.props = Props(
            self.ctx, self.resource.get("Properties") or {}, self.info.logical_id, owner
        )

    @property
    def logical_id(self) -> str:
        return self.info.logical_id

    def self_ref(self, attribute: str) -> Raw:
        return Raw(self.info.expression(attribute))

    def count_items(self, when: str | None = None) -> list[Any]:
        """`count = ...` for a resource that has a Condition, and/or that exists only `when`
        an expression holds (an `!If [C, x, !Ref AWS::NoValue]` list item)."""
        condition = self.resource.get("Condition")
        parts = [self.ctx.condition(condition)] if condition else []
        if when:
            parts.append(when if not parts else f"({when})")
        if not parts:
            return []
        return [hcl.Attr("count", Raw(f"{' && '.join(parts)} ? 1 : 0")), hcl.BLANK]

    def name_attr(self, tf_name: str, cfn_name: str, lowercase: bool = False) -> None:
        """A name Terraform requires but CloudFormation generates when it is left out.
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

    def _extra_name(self, suffix: str) -> str:
        """Main name plus suffix, without saying the main name twice: a policy named
        'MyRolePolicy' on 'MyRole' is 'my_role_policy', not 'my_role_my_role_policy'."""
        if not suffix:
            return self.info.name
        repeated = f"_{self.info.name}_"
        if suffix.startswith(repeated):
            suffix = suffix[len(repeated) - 1 :]
        return f"{self.info.name}{suffix}"

    def add_data(self, kind: str, suffix: str, items: list[Any]) -> str:
        """A data source belonging to this resource (e.g. archive_file for Lambda code).
        Returns its address."""
        name = self.ctx.names_for(f"data.{kind}").take(self._extra_name(suffix))
        self.extra.append(hcl.Block("data", [kind, name], self.count_items() + items))
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
        """An extra resource next to the main one (e.g. aws_s3_bucket_versioning). Returns its
        address. It shares the main resource's Condition unless counted=False; `when` adds a
        condition of its own (see Context.optional_item)."""
        name = self.ctx.names_for(tf_type).take(self._extra_name(suffix))
        body = (self.count_items(when) if counted else []) + items
        self.extra.append(hcl.Block("resource", [tf_type, name], body))
        return f"{tf_type}.{name}"
