"""Conversion state shared by every part of a template, and CloudFormation intrinsic
functions as Terraform expressions.

`Context.expr(value)` takes any CloudFormation value (literal, list, mapping, or an
intrinsic such as `{"Fn::Sub": "..."}`) and returns an HCL value for hcl.render().
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any

from converters.cfn import hcl
from converters.cfn.hcl import Raw
from converters.cfn.naming import UniqueNames, identifier, snake
from converters.cfn.parse import CfnError

PSEUDO_DATA = {
    "AWS::Region": ("aws_region", "current", "region"),
    "AWS::AccountId": ("aws_caller_identity", "current", "account_id"),
    "AWS::Partition": ("aws_partition", "current", "partition"),
    "AWS::URLSuffix": ("aws_partition", "current", "dns_suffix"),
}
# CloudFormation's own limit on the number of CIDR blocks Fn::Cidr returns.
_MAX_CIDR_COUNT = 256
_ATTRIBUTE = re.compile(r"^[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*$")
_SUB_VARIABLE = re.compile(r"\$\{([^}]*)\}")
# Operands that need no parentheses: a reference, a number, a quoted string, a call with
# simple arguments, or a negation of one of those.
_SIMPLE_EXPRESSION = re.compile(
    r'^!?(?:[A-Za-z_][A-Za-z0-9_.\[\]"*-]*|-?[0-9.]+|"(?:[^"\\]|\\.)*"'
    r"|[A-Za-z_][A-Za-z0-9_]*\([^()]*\))$"
)
# Nested Fn::If / Fn::Join chains deeper than this are not something people write by hand.
_MAX_EXPRESSION_DEPTH = 50


@dataclass(frozen=True)
class Expression:
    """An HCL expression placed in a template by the SAM or CDK step, such as
    `file("${path.module}/api.yaml")`. JSON and YAML can't produce this type, so a template
    can't inject one."""

    text: str


@dataclass
class ParamInfo:
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
    kind: str = "aws"  # "aws", "awscc", or "unsupported"
    conditional: bool = False
    # Per-attribute overrides: a whole expression instead of `address.attribute`.
    overrides: dict[str, str] = field(default_factory=dict)

    @property
    def address(self) -> str:
        return f"{self.tf_type}.{self.name}"

    def expression(self, attribute: str) -> str:
        if attribute in self.overrides:
            return self.overrides[attribute]
        index = "[0]" if self.conditional else ""
        return f"{self.address}{index}.{attribute}"


class Context:
    """Everything the conversion of one template accumulates: names, notes, data sources and
    variables that references created."""

    def __init__(self, template: dict[str, Any], provider: str = "aws") -> None:
        self.template = template
        self.provider = provider
        self.notes: list[str] = []
        self._note_set: set[str] = set()
        self.params: dict[str, ParamInfo] = {}
        self.resources: dict[str, ResourceInfo] = {}
        self.conditions: dict[str, str] = {}  # CFN condition name -> local name
        self.mappings: dict[str, str] = {}  # CFN mapping name -> local name
        self.data_sources: dict[tuple[str, str], hcl.Block] = {}
        self.extra_variables: dict[str, hcl.Block] = {}
        self.variable_names = UniqueNames()
        self.local_names = UniqueNames()
        self.output_names = UniqueNames()
        # Terraform names per resource or data source type, so the extra resources a
        # converter adds can't collide with each other or with converted resources.
        self.type_names: dict[str, UniqueNames] = {}
        # Always available for AWS::StackName, so no parameter can take the name.
        self.variable_names.reserve("stack_name")
        self.stack_name_used = False
        # Above 0 while a JSON document (jsonencode) is being converted.
        self.json_depth = 0
        self.parameter_types = {
            name: str(spec.get("Type"))
            for name, spec in (template.get("Parameters") or {}).items()
            if isinstance(spec, dict)
        }

    # --- notes ------------------------------------------------------------------------

    def note(self, message: str) -> None:
        """Record something the user must look at. Duplicates are dropped."""
        if message not in self._note_set:
            self._note_set.add(message)
            self.notes.append(message)

    def names_for(self, kind: str) -> UniqueNames:
        return self.type_names.setdefault(kind, UniqueNames())

    # --- data sources and variables created by references ------------------------------

    def data(self, kind: str, name: str, body: list[Any] | None = None) -> str:
        """Register `data "<kind>" "<name>"` (once) and return its address."""
        if (kind, name) not in self.data_sources:
            self.data_sources[(kind, name)] = hcl.Block("data", [kind, name], body or [])
        return f"data.{kind}.{name}"

    def placeholder_variable(self, wanted: str, description: str, tf_type: str = "string") -> str:
        """A variable standing in for a value Terraform can't compute (another stack's export,
        an attribute of a resource that was not converted). Returns `var.<name>`."""
        key = f"{wanted}\0{description}"
        if key not in self.extra_variables:
            name = self.variable_names.take(identifier(wanted))
            self.extra_variables[key] = hcl.Block(
                "variable",
                [name],
                [hcl.Attr("description", description), hcl.Attr("type", Raw(tf_type))],
            )
        return f"var.{self.extra_variables[key].labels[0]}"

    # --- expressions ------------------------------------------------------------------

    def expr(self, value: Any, depth: int = 0) -> Any:
        """A CloudFormation value as an HCL value. Mapping keys are kept as written."""
        if depth > _MAX_EXPRESSION_DEPTH:
            raise CfnError("Template expressions are nested too deeply")
        if isinstance(value, Expression):
            return Raw(value.text)
        if isinstance(value, dict):
            function = intrinsic_name(value)
            if function is not None:
                return self._intrinsic(function, value[function], depth + 1)
            return self._mapping(value, depth)
        if isinstance(value, list):
            return self._list(value, depth)
        return value

    def _mapping(self, value: dict[Any, Any], depth: int) -> Any:
        """A mapping, without the keys CloudFormation removes: those set to AWS::NoValue,
        and `!If [Cond, x, !Ref AWS::NoValue]` ones when the condition picks NoValue."""
        fixed: dict[str, Any] = {}
        optional: list[str] = []
        for key, item in value.items():
            if self.is_no_value(item):
                continue
            condition, present = self.optional_item(item)
            if condition is None:
                fixed[str(key)] = self.expr(item, depth + 1)
            else:
                entry = hcl.inline({str(key): self.expr(present, depth + 1)})
                optional.append(f"{condition} ? {entry} : {{}}")
        if not optional:
            return fixed
        return Raw(f"merge({', '.join([hcl.inline(fixed), *optional])})")

    def _list(self, items: list[Any], depth: int) -> Any:
        """A list, without the items CloudFormation removes: `!Ref AWS::NoValue`, and
        `!If [Cond, x, !Ref AWS::NoValue]` when the condition picks NoValue."""
        kept = [item for item in items if not self.is_no_value(item)]
        converted = [self.expr(item, depth + 1) for item in kept]
        if not any(self.optional_item(item)[0] for item in kept):
            return converted
        return Raw(f"[for item in {hcl.inline(converted)} : item if item != null]")

    def optional_item(self, item: Any) -> tuple[str | None, Any]:
        """For `!If [Cond, x, !Ref AWS::NoValue]` (or with the branches swapped): the
        condition under which the item exists, and x. Otherwise (None, item)."""
        if not (isinstance(item, dict) and intrinsic_name(item) == "Fn::If"):
            return None, item
        args = item["Fn::If"]
        if not isinstance(args, list) or len(args) != 3:
            return None, item
        name, yes, no = args
        if self.is_no_value(no) and not self.is_no_value(yes):
            return self.condition(name), yes
        if self.is_no_value(yes) and not self.is_no_value(no):
            return f"!{self.condition(name)}", no
        return None, item

    def text(self, value: Any) -> str:
        """An expression's source text on one line."""
        return hcl.inline(self.expr(value))

    def is_no_value(self, value: Any) -> bool:
        return isinstance(value, dict) and value == {"Ref": "AWS::NoValue"}

    def _intrinsic(self, function: str, args: Any, depth: int) -> Any:
        handler = _HANDLERS.get(function)
        if handler is None:
            self.note(f"Intrinsic function '{function}' is not supported; it was replaced by null")
            return None
        return handler(self, args, depth)

    # --- Ref and Fn::GetAtt -------------------------------------------------------------

    def ref(self, name: Any) -> Any:
        if not isinstance(name, str):
            raise CfnError("'Ref' takes the name of a parameter or resource")
        if name == "AWS::NoValue":
            return None
        if name in PSEUDO_DATA:
            kind, label, attribute = PSEUDO_DATA[name]
            return Raw(f"{self.data(kind, label)}.{attribute}")
        if name == "AWS::StackName":
            self.stack_name_used = True
            return Raw("var.stack_name")
        if name == "AWS::StackId":
            self.stack_name_used = True
            self.note(
                "AWS::StackId has no Terraform equivalent; the stack name (var.stack_name) "
                "is used instead"
            )
            return Raw("var.stack_name")
        if name == "AWS::NotificationARNs":
            self.note("AWS::NotificationARNs has no Terraform equivalent; replaced by []")
            return []
        if name in self.params:
            return Raw(self.param_expression(name))
        if name in self.resources:
            info = self.resources[name]
            if info.kind == "unsupported":
                return Raw(self._unresolved(info, "Ref", "the value of Ref"))
            return Raw(info.expression(info.ref))
        raise CfnError(f"'Ref' refers to '{name}', which is not a parameter or resource")

    def param_expression(self, name: str) -> str:
        info = self.params[name]
        if info.ssm is not None:
            value = f"data.aws_ssm_parameter.{info.ssm}.insecure_value"
            if info.tf_type.startswith("list("):
                return f'split(",", {value})'
            return value
        return f"var.{info.name}"

    def get_att(self, args: Any) -> Any:
        if isinstance(args, str):
            args = list(args.partition(".")[::2])
        if not isinstance(args, list) or len(args) != 2:
            raise CfnError("'Fn::GetAtt' takes [resource, attribute]")
        logical_id, attribute = args
        if not isinstance(logical_id, str) or logical_id not in self.resources:
            raise CfnError(f"'Fn::GetAtt' refers to unknown resource '{logical_id}'")
        if not isinstance(attribute, str):
            # An attribute name chosen at deploy time (e.g. !GetAtt [R, !Ref Param]).
            self.note(
                f"Fn::GetAtt on '{logical_id}' with a computed attribute name was not converted"
            )
            return None
        info = self.resources[logical_id]
        if info.kind == "unsupported":
            return Raw(self._unresolved(info, attribute, f"the '{attribute}' attribute"))
        if attribute in info.attrs:
            return Raw(info.expression(info.attrs[attribute]))
        if not _ATTRIBUTE.match(attribute):
            raise CfnError(f"'Fn::GetAtt' attribute '{attribute}' of '{logical_id}' is not valid")
        tf_attribute = ".".join(snake(part) for part in attribute.split("."))
        if info.kind == "aws":
            self.note(
                f"'{logical_id}.{attribute}' was mapped to '{info.address}.{tf_attribute}'; "
                "check that attribute exists"
            )
        return Raw(info.expression(tf_attribute))

    def _unresolved(self, info: ResourceInfo, attribute: str, what: str) -> str:
        wanted = f"{info.logical_id}_{attribute}" if attribute != "Ref" else info.logical_id
        return self.placeholder_variable(
            wanted,
            f"{what} of {info.logical_id} ({info.cfn_type}), which was not converted",
        )

    def condition(self, name: Any) -> str:
        if not isinstance(name, str) or name not in self.conditions:
            raise CfnError(f"Unknown condition '{name}'")
        return f"local.{self.conditions[name]}"


def intrinsic_name(value: dict[str, Any]) -> str | None:
    """The function name if `value` is an intrinsic function call, else None."""
    if len(value) != 1:
        return None
    (name,) = value
    if name == "Ref" or (isinstance(name, str) and name.startswith("Fn::")):
        return name
    if name == "Condition" and isinstance(value[name], str):
        return name
    return None


def _wrap(text: str) -> str:
    """Parentheses around a compound expression used as an operand."""
    if _SIMPLE_EXPRESSION.match(text):
        return text
    return f"({text})"


def _args(args: Any, count: int | tuple[int, ...], function: str) -> list[Any]:
    counts = (count,) if isinstance(count, int) else count
    if not isinstance(args, list) or len(args) not in counts:
        expected = " or ".join(str(c) for c in counts)
        raise CfnError(f"'{function}' takes a list of {expected} values")
    return args


def _fn_ref(ctx: Context, args: Any, depth: int) -> Any:
    return ctx.ref(args)


def _fn_get_att(ctx: Context, args: Any, depth: int) -> Any:
    return ctx.get_att(args)


def _fn_condition(ctx: Context, args: Any, depth: int) -> Any:
    return Raw(ctx.condition(args))


def _scalar_text(value: Any) -> str:
    """A literal as CloudFormation turns it into text: true, 1.5, abc."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return hcl.render(value)
    return str(value)


_TRAILING_MARKERS = re.compile(r"[$%]+$")


def _template(pieces: list[Any]) -> Any:
    """Literal strings and HCL values as one value: the text itself when every piece is
    literal, the value when there is only one, otherwise a template string.

    Literal runs are joined before they are escaped, so text split across pieces ("$" then
    "{x}") can't form an interpolation, and a "$" or "%" right before an interpolation is
    written as ${"$"}, because "$${" would escape it."""
    merged: list[Any] = []
    for piece in pieces:
        if isinstance(piece, str) and merged and isinstance(merged[-1], str):
            merged[-1] += piece
        elif piece != "":
            merged.append(piece)
    if not merged:
        return ""
    if len(merged) == 1:
        return merged[0]
    out: list[str] = []
    for index, piece in enumerate(merged):
        if not isinstance(piece, str):
            out.append("${" + hcl.inline(piece) + "}")
            continue
        markers = _TRAILING_MARKERS.search(piece)
        if markers and index + 1 < len(merged):
            out.append(hcl.template_text(piece[: markers.start()]))
            out.append("${" + hcl.quote(markers.group()) + "}")
        else:
            out.append(hcl.template_text(piece))
    return Raw('"' + "".join(out) + '"')


def _fn_sub(ctx: Context, args: Any, depth: int) -> Any:
    variables: dict[str, Any] = {}
    if isinstance(args, list):
        if len(args) != 2 or not isinstance(args[1], dict):
            raise CfnError("'Fn::Sub' takes a string, or [string, {variables}]")
        args, variables = args
    if not isinstance(args, str):
        raise CfnError("'Fn::Sub' needs a string")

    pieces: list[tuple[str, Any]] = []  # ("text", str) or ("expr", hcl value)
    position = 0
    for match in _SUB_VARIABLE.finditer(args):
        if match.start() > position:
            pieces.append(("text", args[position : match.start()]))
        name = match.group(1).strip()
        position = match.end()
        if name.startswith("!"):
            pieces.append(("text", "${" + name[1:] + "}"))
        elif name in variables:
            pieces.append(("expr", ctx.expr(variables[name], depth + 1)))
        elif "." in name and not name.startswith("AWS::"):
            resource, _, attribute = name.partition(".")
            pieces.append(("expr", ctx.get_att([resource, attribute])))
        else:
            pieces.append(("expr", ctx.ref(name)))
    if position < len(args):
        pieces.append(("text", args[position:]))

    return _template([value for _, value in pieces])


def _fn_join(ctx: Context, args: Any, depth: int) -> Any:
    delimiter, items = _args(args, 2, "Fn::Join")
    if isinstance(items, list):
        # CloudFormation leaves out AWS::NoValue items, and their delimiter with them.
        items = [item for item in items if not ctx.is_no_value(item)]
        optional = any(ctx.optional_item(item)[0] for item in items)
        listed = any(
            isinstance(item, dict) and intrinsic_name(item) in ("Fn::GetAZs", "Fn::Split")
            for item in items
        )
        if isinstance(delimiter, str) and not optional and not listed:
            # A template string reads better than join() when every item is one value.
            pieces: list[Any] = []
            for index, item in enumerate(items):
                if index:
                    pieces.append(delimiter)
                if isinstance(item, str | int | float):
                    pieces.append(_scalar_text(item))
                else:
                    pieces.append(ctx.expr(item, depth + 1))
            return _template(pieces)
    return Raw(f"join({hcl.inline(ctx.expr(delimiter, depth))}, {ctx.text(items)})")


def _fn_select(ctx: Context, args: Any, depth: int) -> Any:
    index, items = _args(args, 2, "Fn::Select")
    if isinstance(items, list) and isinstance(index, int | str) and str(index).isdigit():
        position = int(index)
        if position >= len(items):
            raise CfnError(f"'Fn::Select' index {position} is out of range")
        return ctx.expr(items[position], depth)
    return Raw(f"element({ctx.text(items)}, {hcl.inline(ctx.expr(index, depth))})")


def _fn_split(ctx: Context, args: Any, depth: int) -> Any:
    delimiter, source = _args(args, 2, "Fn::Split")
    if isinstance(delimiter, str) and isinstance(source, str):
        return source.split(delimiter)
    return Raw(f"split({hcl.inline(ctx.expr(delimiter, depth))}, {ctx.text(source)})")


def _fn_get_azs(ctx: Context, args: Any, depth: int) -> Any:
    if args not in ("", None) and not (isinstance(args, dict) and args == {"Ref": "AWS::Region"}):
        ctx.note("Fn::GetAZs for another region was converted to the current region's zones")
    address = ctx.data("aws_availability_zones", "available", [hcl.Attr("state", "available")])
    return Raw(f"{address}.names")


def _fn_base64(ctx: Context, args: Any, depth: int) -> Any:
    return Raw(f"base64encode({hcl.inline(ctx.expr(args, depth))})")


def _fn_cidr(ctx: Context, args: Any, depth: int) -> Any:
    block, count, bits = _args(args, 3, "Fn::Cidr")
    try:
        count_value = int(count)
        bits_value = int(bits)
    except (TypeError, ValueError):
        count_value = bits_value = -1
    if count_value > _MAX_CIDR_COUNT:
        raise CfnError(f"'Fn::Cidr' can make at most {_MAX_CIDR_COUNT} blocks")
    if isinstance(block, str) and count_value > 0 and bits_value > 0:
        try:
            network = ipaddress.ip_network(block, strict=False)
            new_prefix = network.max_prefixlen - bits_value
            subnets = []
            for subnet in network.subnets(new_prefix=new_prefix):
                subnets.append(str(subnet))
                if len(subnets) == count_value:
                    break
            return subnets
        except ValueError as exc:
            raise CfnError(f"'Fn::Cidr' can't split '{block}' that way") from exc
    block_text = hcl.inline(ctx.expr(block, depth))
    count_text = hcl.inline(ctx.expr(count, depth))
    bits_text = hcl.inline(ctx.expr(bits, depth))
    size = f'(strcontains({block_text}, ":") ? 128 : 32)'
    newbits = f'{size} - {_wrap(bits_text)} - tonumber(split("/", {block_text})[1])'
    return Raw(f"[for i in range({count_text}) : cidrsubnet({block_text}, {newbits}, i)]")


def _map_key(ctx: Context, value: Any, depth: int) -> str:
    return "[" + hcl.inline(ctx.expr(value, depth)) + "]"


def _fn_find_in_map(ctx: Context, args: Any, depth: int) -> Any:
    args = _args(args, (3, 4), "Fn::FindInMap")
    name, first, second = args[:3]
    if not isinstance(name, str) or name not in ctx.mappings:
        raise CfnError(f"'Fn::FindInMap' refers to unknown mapping '{name}'")
    local = f"local.{ctx.mappings[name]}"
    if len(args) == 4:
        default = args[3]
        if isinstance(default, dict) and "DefaultValue" in default:
            default = default["DefaultValue"]
        first_text = hcl.inline(ctx.expr(first, depth))
        second_text = hcl.inline(ctx.expr(second, depth))
        default_text = hcl.inline(ctx.expr(default, depth))
        return Raw(f"lookup(lookup({local}, {first_text}, {{}}), {second_text}, {default_text})")
    return Raw(local + _map_key(ctx, first, depth) + _map_key(ctx, second, depth))


def _fn_if(ctx: Context, args: Any, depth: int) -> Any:
    name, when_true, when_false = _args(args, 3, "Fn::If")
    condition = ctx.condition(name)
    yes = hcl.inline(ctx.expr(when_true, depth))
    no = hcl.inline(ctx.expr(when_false, depth))
    if ctx.json_depth and not (isinstance(when_true, str) and isinstance(when_false, str)):
        # Inside a JSON document the branches may differ in shape (a string or a list of
        # them); a conditional needs one type, so each branch is encoded on its own.
        return Raw(f"jsondecode({condition} ? jsonencode({yes}) : jsonencode({no}))")
    return Raw(f"{condition} ? {yes} : {no}")


def _number_param(ctx: Context, value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"Ref"}
        and ctx.parameter_types.get(str(value["Ref"])) == "Number"
    )


def _fn_equals(ctx: Context, args: Any, depth: int) -> Any:
    """CloudFormation compares as text, while Terraform never treats 2 and "2", or true and
    "true", as equal: literals become strings and Number parameters go through tostring()."""
    left, right = _args(args, 2, "Fn::Equals")
    texts = []
    for side in (left, right):
        if isinstance(side, bool | int | float):
            text = hcl.quote(_scalar_text(side))
        else:
            text = hcl.inline(ctx.expr(side, depth))
            if _number_param(ctx, side):
                text = f"tostring({text})"
        texts.append(text)
    return Raw(f"{_wrap(texts[0])} == {_wrap(texts[1])}")


def _logical(operator: str, function: str) -> Any:
    def handler(ctx: Context, args: Any, depth: int) -> Any:
        if not isinstance(args, list) or not 2 <= len(args) <= 10:
            raise CfnError(f"'{function}' takes a list of 2 to 10 conditions")
        parts = [_wrap(hcl.inline(ctx.expr(arg, depth))) for arg in args]
        return Raw(f" {operator} ".join(parts))

    return handler


def _fn_not(ctx: Context, args: Any, depth: int) -> Any:
    (value,) = _args(args, 1, "Fn::Not")
    return Raw(f"!{_wrap(hcl.inline(ctx.expr(value, depth)))}")


def _fn_import_value(ctx: Context, args: Any, depth: int) -> Any:
    if isinstance(args, str):
        export = args
    else:
        rendered = ctx.expr(args, depth)
        export = rendered if isinstance(rendered, str) else hcl.inline(rendered)
    variable = ctx.placeholder_variable(
        f"import_{export}",
        f"Value of the CloudFormation export '{export}' (Fn::ImportValue)",
    )
    ctx.note(
        f"Fn::ImportValue '{export}' became {variable}: Terraform has no stack exports; pass the "
        "value in, or read it with a terraform_remote_state or data source"
    )
    return Raw(variable)


def _fn_to_json_string(ctx: Context, args: Any, depth: int) -> Any:
    return hcl.Call("jsonencode", ctx.expr(args, depth))


def _fn_length(ctx: Context, args: Any, depth: int) -> Any:
    if isinstance(args, list):
        return len(args)
    return Raw(f"length({ctx.text(args)})")


def _fn_transform(ctx: Context, args: Any, depth: int) -> Any:
    name = args.get("Name") if isinstance(args, dict) else None
    ctx.note(
        f"Fn::Transform ({name or 'a macro'}) runs inside CloudFormation and was not converted"
    )
    return None


_HANDLERS = {
    "Ref": _fn_ref,
    "Condition": _fn_condition,
    "Fn::GetAtt": _fn_get_att,
    "Fn::Sub": _fn_sub,
    "Fn::Join": _fn_join,
    "Fn::Select": _fn_select,
    "Fn::Split": _fn_split,
    "Fn::GetAZs": _fn_get_azs,
    "Fn::Base64": _fn_base64,
    "Fn::Cidr": _fn_cidr,
    "Fn::FindInMap": _fn_find_in_map,
    "Fn::If": _fn_if,
    "Fn::Equals": _fn_equals,
    "Fn::And": _logical("&&", "Fn::And"),
    "Fn::Or": _logical("||", "Fn::Or"),
    "Fn::Not": _fn_not,
    "Fn::ImportValue": _fn_import_value,
    "Fn::ToJsonString": _fn_to_json_string,
    "Fn::Length": _fn_length,
    "Fn::Transform": _fn_transform,
}


def json_value(ctx: Context, value: Any) -> Any:
    """A JSON document property (IAM policy, event pattern, state machine definition) as
    `jsonencode({...})`, or the string itself when it is already JSON text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return value
        _check_json_depth(value)
    function = intrinsic_name(value) if isinstance(value, dict) else None
    if function in _STRING_FUNCTIONS:
        # Ref, Sub and the like give text, which is already a JSON document.
        return ctx.expr(value)
    if function == "Fn::If":
        name, yes, no = _args(value[function], 3, "Fn::If")
        branches = [
            "null" if ctx.is_no_value(branch) else hcl.inline(json_value(ctx, branch))
            for branch in (yes, no)
        ]
        return Raw(f"{ctx.condition(name)} ? {branches[0]} : {branches[1]}")
    ctx.json_depth += 1
    try:
        return hcl.Call("jsonencode", ctx.expr(value))
    finally:
        ctx.json_depth -= 1


def _check_json_depth(value: Any) -> None:
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > 100:
            raise CfnError("A JSON document in the template is nested too deeply")
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)


_STRING_FUNCTIONS = {
    "Ref",
    "Fn::Sub",
    "Fn::Join",
    "Fn::GetAtt",
    "Fn::ImportValue",
    "Fn::ToJsonString",
    "Fn::Base64",
}
