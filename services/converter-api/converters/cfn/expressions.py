"""CloudFormation values and intrinsic functions as Terraform expressions.

ExpressionConverter turns any CloudFormation value (a literal, a list, a mapping, or an
intrinsic such as `{"Fn::Sub": "..."}`) into an HCL value for hcl.render(). It resolves names
(parameters, resources, conditions, mappings) through methods its subclass, Context,
provides.
"""

from __future__ import annotations

import ipaddress
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from converters.cfn import hcl
from converters.cfn.constants import (
    MAX_CIDR_COUNT,
    MAX_EXPRESSION_DEPTH,
    MAX_JSON_DEPTH,
    STRING_FUNCTIONS,
)
from converters.cfn.errors import CfnError
from converters.cfn.models import Attr, Call, Expression, Raw

NO_VALUE = {"Ref": "AWS::NoValue"}
SUB_VARIABLE = re.compile(r"\$\{([^}]*)\}")
TRAILING_MARKERS = re.compile(r"[$%]+$")
# Operands that need no parentheses: a reference, a number, a quoted string, a call with
# simple arguments, or a negation of one of those.
SIMPLE_EXPRESSION = re.compile(
    r'^!?(?:[A-Za-z_][A-Za-z0-9_.\[\]"*-]*|-?[0-9.]+|"(?:[^"\\]|\\.)*"'
    r"|[A-Za-z_][A-Za-z0-9_]*\([^()]*\))$"
)
LOGICAL_OPERATORS = {"Fn::And": "&&", "Fn::Or": "||"}


def intrinsic_name(value: dict[str, Any]) -> str | None:
    """The function name when `value` is an intrinsic function call, else None."""
    if len(value) != 1:
        return None
    (name,) = value
    if name == "Ref" or (isinstance(name, str) and name.startswith("Fn::")):
        return name
    if name == "Condition" and isinstance(value[name], str):
        return name
    return None


def wrap_operand(text: str) -> str:
    """Parentheses around a compound expression used as an operand of an operator."""
    if SIMPLE_EXPRESSION.match(text):
        return text
    return f"({text})"


def scalar_text(value: Any) -> str:
    """A literal as CloudFormation turns it into text: true, 1.5, abc."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return hcl.render(value)
    return str(value)


def arguments(args: Any, counts: tuple[int, ...], function: str) -> list[Any]:
    """The argument list of an intrinsic, checked to hold one of `counts` values."""
    if not isinstance(args, list) or len(args) not in counts:
        expected = " or ".join(str(count) for count in counts)
        raise CfnError(f"'{function}' takes a list of {expected} values")
    return args


def template(pieces: list[Any]) -> Any:
    """Literal strings and HCL values as one value: the text itself when every piece is
    literal, the value when there is only one, otherwise a template string.

    Literal runs are joined before they are escaped, so text split across pieces ("$" then
    "{x}") never forms an interpolation, and a "$" or "%" right before an interpolation is
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
        markers = TRAILING_MARKERS.search(piece)
        if markers and index + 1 < len(merged):
            out.append(hcl.template_text(piece[: markers.start()]))
            out.append("${" + hcl.quote(markers.group()) + "}")
        else:
            out.append(hcl.template_text(piece))
    return Raw('"' + "".join(out) + '"')


def check_json_depth(value: Any) -> None:
    """Reject a JSON document nested deeper than MAX_JSON_DEPTH."""
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise CfnError("A JSON document in the template is nested too deeply")
        if isinstance(node, dict):
            stack.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)


class ExpressionConverter(ABC):
    """Converts CloudFormation values to HCL values. Name lookups (Ref, Fn::GetAtt,
    conditions, mappings) and side effects (notes, data sources, variables) are left to the
    subclass: see Context."""

    def __init__(self) -> None:
        """The table of intrinsic functions, and no JSON document in progress."""
        # Above 0 while a JSON document (jsonencode) is being converted.
        self.json_depth = 0
        self.functions: dict[str, Callable[[Any, int], Any]] = {
            "Ref": self.fn_ref,
            "Condition": self.fn_condition,
            "Fn::GetAtt": self.fn_get_att,
            "Fn::Sub": self.fn_sub,
            "Fn::Join": self.fn_join,
            "Fn::Select": self.fn_select,
            "Fn::Split": self.fn_split,
            "Fn::GetAZs": self.fn_get_azs,
            "Fn::Base64": self.fn_base64,
            "Fn::Cidr": self.fn_cidr,
            "Fn::FindInMap": self.fn_find_in_map,
            "Fn::If": self.fn_if,
            "Fn::Equals": self.fn_equals,
            "Fn::And": self.fn_and,
            "Fn::Or": self.fn_or,
            "Fn::Not": self.fn_not,
            "Fn::ImportValue": self.fn_import_value,
            "Fn::ToJsonString": self.fn_to_json_string,
            "Fn::Length": self.fn_length,
            "Fn::Transform": self.fn_transform,
        }

    # --- what the subclass provides -----------------------------------------------------

    @abstractmethod
    def note(self, message: str) -> None:
        """Record something the user must look at."""

    @abstractmethod
    def data(self, kind: str, name: str, body: list[Any]) -> str:
        """Register a data source and return its address."""

    @abstractmethod
    def placeholder_variable(self, wanted: str, description: str) -> str:
        """A variable standing in for a value Terraform has no way to compute."""

    @abstractmethod
    def ref(self, name: Any) -> Any:
        """The value of `Ref name`."""

    @abstractmethod
    def get_att(self, args: Any) -> Any:
        """The value of `Fn::GetAtt args`."""

    @abstractmethod
    def condition(self, name: Any) -> str:
        """The expression of a named condition."""

    @abstractmethod
    def mapping_local(self, name: Any) -> str:
        """The local value holding a named mapping."""

    @abstractmethod
    def parameter_type(self, name: str) -> str:
        """The CloudFormation type of a parameter, or "" when there is no such parameter."""

    # --- values -------------------------------------------------------------------------

    def expr(self, value: Any, depth: int = 0) -> Any:
        """A CloudFormation value as an HCL value. Mapping keys are kept as written."""
        if depth > MAX_EXPRESSION_DEPTH:
            raise CfnError("Template expressions are nested too deeply")
        if isinstance(value, Expression):
            return Raw(value.text)
        if isinstance(value, dict):
            function = intrinsic_name(value)
            if function is not None:
                return self.call(function, value[function], depth + 1)
            return self.mapping(value, depth)
        if isinstance(value, list):
            return self.sequence(value, depth)
        return value

    def text(self, value: Any) -> str:
        """An expression's source text on one line."""
        return hcl.inline(self.expr(value))

    def is_no_value(self, value: Any) -> bool:
        """Whether `value` is `!Ref AWS::NoValue`."""
        return isinstance(value, dict) and value == NO_VALUE

    def mapping(self, value: dict[Any, Any], depth: int) -> Any:
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
        return Raw(f"merge({', '.join([hcl.inline(fixed)] + optional)})")

    def sequence(self, items: list[Any], depth: int) -> Any:
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

    def json(self, value: Any) -> Any:
        """A JSON document property (IAM policy, event pattern, state machine definition)
        as `jsonencode({...})`, or the string itself when it is already JSON text."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                return value
            check_json_depth(value)
        function = intrinsic_name(value) if isinstance(value, dict) else None
        if function in STRING_FUNCTIONS:
            # Ref, Sub and the like give text, which is already a JSON document.
            return self.expr(value)
        if function == "Fn::If":
            name, yes, no = arguments(value[function], (3,), "Fn::If")
            branches = [
                "null" if self.is_no_value(branch) else hcl.inline(self.json(branch))
                for branch in (yes, no)
            ]
            return Raw(f"{self.condition(name)} ? {branches[0]} : {branches[1]}")
        self.json_depth += 1
        try:
            return Call("jsonencode", self.expr(value))
        finally:
            self.json_depth -= 1

    def call(self, function: str, args: Any, depth: int) -> Any:
        """One intrinsic function."""
        handler = self.functions.get(function)
        if handler is None:
            self.note(f"Intrinsic function '{function}' is not supported and became null")
            return None
        return handler(args, depth)

    # --- intrinsic functions -------------------------------------------------------------

    def fn_ref(self, args: Any, depth: int) -> Any:
        """Ref."""
        return self.ref(args)

    def fn_get_att(self, args: Any, depth: int) -> Any:
        """Fn::GetAtt."""
        return self.get_att(args)

    def fn_condition(self, args: Any, depth: int) -> Any:
        """`Condition: Name` inside Fn::And, Fn::Or and Fn::Not."""
        return Raw(self.condition(args))

    def fn_sub(self, args: Any, depth: int) -> Any:
        """Fn::Sub as a template string."""
        variables: dict[str, Any] = {}
        if isinstance(args, list):
            if len(args) != 2 or not isinstance(args[1], dict):
                raise CfnError("'Fn::Sub' takes a string or [string, {variables}]")
            args, variables = args
        if not isinstance(args, str):
            raise CfnError("'Fn::Sub' needs a string")
        pieces: list[Any] = []
        position = 0
        for match in SUB_VARIABLE.finditer(args):
            if match.start() > position:
                pieces.append(args[position : match.start()])
            name = match.group(1).strip()
            position = match.end()
            if name.startswith("!"):
                pieces.append("${" + name[1:] + "}")
            elif name in variables:
                pieces.append(self.expr(variables[name], depth + 1))
            elif "." in name and not name.startswith("AWS::"):
                resource, _, attribute = name.partition(".")
                pieces.append(self.get_att([resource, attribute]))
            else:
                pieces.append(self.ref(name))
        if position < len(args):
            pieces.append(args[position:])
        return template(pieces)

    def fn_join(self, args: Any, depth: int) -> Any:
        """Fn::Join: a template string when every item is one value, otherwise join()."""
        delimiter, items = arguments(args, (2,), "Fn::Join")
        if isinstance(items, list):
            # CloudFormation leaves out AWS::NoValue items, and their delimiter with them.
            items = [item for item in items if not self.is_no_value(item)]
            optional = any(self.optional_item(item)[0] for item in items)
            listed = any(
                isinstance(item, dict) and intrinsic_name(item) in ("Fn::GetAZs", "Fn::Split")
                for item in items
            )
            if isinstance(delimiter, str) and not optional and not listed:
                pieces: list[Any] = []
                for index, item in enumerate(items):
                    if index:
                        pieces.append(delimiter)
                    if isinstance(item, str | int | float):
                        pieces.append(scalar_text(item))
                    else:
                        pieces.append(self.expr(item, depth + 1))
                return template(pieces)
        return Raw(f"join({hcl.inline(self.expr(delimiter, depth))}, {self.text(items)})")

    def fn_select(self, args: Any, depth: int) -> Any:
        """Fn::Select: the item itself for a literal list and index, otherwise element()."""
        index, items = arguments(args, (2,), "Fn::Select")
        if isinstance(items, list) and isinstance(index, int | str) and str(index).isdigit():
            position = int(index)
            if position >= len(items):
                raise CfnError(f"'Fn::Select' index {position} is out of range")
            return self.expr(items[position], depth)
        return Raw(f"element({self.text(items)}, {hcl.inline(self.expr(index, depth))})")

    def fn_split(self, args: Any, depth: int) -> Any:
        """Fn::Split: the list itself for literal text, otherwise split()."""
        delimiter, source = arguments(args, (2,), "Fn::Split")
        if isinstance(delimiter, str) and isinstance(source, str):
            return source.split(delimiter)
        return Raw(f"split({hcl.inline(self.expr(delimiter, depth))}, {self.text(source)})")

    def fn_get_azs(self, args: Any, depth: int) -> Any:
        """Fn::GetAZs: the availability zones of the current region."""
        if args not in ("", None) and args != {"Ref": "AWS::Region"}:
            self.note("Fn::GetAZs for another region became the zones of the current region")
        address = self.data("aws_availability_zones", "available", [Attr("state", "available")])
        return Raw(f"{address}.names")

    def fn_base64(self, args: Any, depth: int) -> Any:
        """Fn::Base64."""
        return Raw(f"base64encode({hcl.inline(self.expr(args, depth))})")

    def fn_cidr(self, args: Any, depth: int) -> Any:
        """Fn::Cidr: the blocks themselves for a literal network, otherwise cidrsubnet()."""
        block, count, bits = arguments(args, (3,), "Fn::Cidr")
        try:
            count_value = int(count)
            bits_value = int(bits)
        except (TypeError, ValueError):
            count_value = bits_value = -1
        if count_value > MAX_CIDR_COUNT:
            raise CfnError(f"'Fn::Cidr' makes at most {MAX_CIDR_COUNT} blocks")
        if isinstance(block, str) and count_value > 0 and bits_value > 0:
            return self.literal_cidr(block, count_value, bits_value)
        block_text = hcl.inline(self.expr(block, depth))
        count_text = hcl.inline(self.expr(count, depth))
        bits_text = hcl.inline(self.expr(bits, depth))
        size = f'(strcontains({block_text}, ":") ? 128 : 32)'
        newbits = f'{size} - {wrap_operand(bits_text)} - tonumber(split("/", {block_text})[1])'
        return Raw(f"[for i in range({count_text}) : cidrsubnet({block_text}, {newbits}, i)]")

    def literal_cidr(self, block: str, count: int, bits: int) -> list[str]:
        """The first `count` blocks of `block` with `bits` host bits each."""
        try:
            network = ipaddress.ip_network(block, strict=False)
            subnets: list[str] = []
            for subnet in network.subnets(new_prefix=network.max_prefixlen - bits):
                subnets.append(str(subnet))
                if len(subnets) == count:
                    break
            return subnets
        except ValueError as exc:
            raise CfnError(f"'Fn::Cidr' is unable to split '{block}' that way") from exc

    def fn_find_in_map(self, args: Any, depth: int) -> Any:
        """Fn::FindInMap, with lookup() when a DefaultValue is given."""
        args = arguments(args, (3, 4), "Fn::FindInMap")
        local = self.mapping_local(args[0])
        first = hcl.inline(self.expr(args[1], depth))
        second = hcl.inline(self.expr(args[2], depth))
        if len(args) == 4:
            default = args[3]
            if isinstance(default, dict) and "DefaultValue" in default:
                default = default["DefaultValue"]
            default_text = hcl.inline(self.expr(default, depth))
            return Raw(f"lookup(lookup({local}, {first}, {{}}), {second}, {default_text})")
        return Raw(f"{local}[{first}][{second}]")

    def fn_if(self, args: Any, depth: int) -> Any:
        """Fn::If as a conditional expression."""
        name, when_true, when_false = arguments(args, (3,), "Fn::If")
        condition = self.condition(name)
        yes = hcl.inline(self.expr(when_true, depth))
        no = hcl.inline(self.expr(when_false, depth))
        if self.json_depth and not (isinstance(when_true, str) and isinstance(when_false, str)):
            # Inside a JSON document the branches may differ in shape (a string or a list of
            # them); a conditional needs one type, so each branch is encoded on its own.
            return Raw(f"jsondecode({condition} ? jsonencode({yes}) : jsonencode({no}))")
        return Raw(f"{condition} ? {yes} : {no}")

    def fn_equals(self, args: Any, depth: int) -> Any:
        """Fn::Equals. CloudFormation compares as text, while Terraform treats 2 and "2", or
        true and "true", as different: literals become strings and Number parameters go
        through tostring()."""
        texts = []
        for side in arguments(args, (2,), "Fn::Equals"):
            if isinstance(side, bool | int | float):
                text = hcl.quote(scalar_text(side))
            else:
                text = hcl.inline(self.expr(side, depth))
                if self.is_number_parameter(side):
                    text = f"tostring({text})"
            texts.append(text)
        return Raw(f"{wrap_operand(texts[0])} == {wrap_operand(texts[1])}")

    def is_number_parameter(self, value: Any) -> bool:
        """Whether `value` is a Ref to a parameter of type Number."""
        return (
            isinstance(value, dict)
            and set(value) == {"Ref"}
            and self.parameter_type(str(value["Ref"])) == "Number"
        )

    def fn_and(self, args: Any, depth: int) -> Any:
        """Fn::And."""
        return self.logical(args, depth, "Fn::And")

    def fn_or(self, args: Any, depth: int) -> Any:
        """Fn::Or."""
        return self.logical(args, depth, "Fn::Or")

    def logical(self, args: Any, depth: int, function: str) -> Any:
        """Fn::And or Fn::Or: the conditions joined by `&&` or `||`."""
        if not isinstance(args, list) or not 2 <= len(args) <= 10:
            raise CfnError(f"'{function}' takes a list of 2 to 10 conditions")
        parts = [wrap_operand(hcl.inline(self.expr(arg, depth))) for arg in args]
        return Raw(f" {LOGICAL_OPERATORS[function]} ".join(parts))

    def fn_not(self, args: Any, depth: int) -> Any:
        """Fn::Not."""
        (value,) = arguments(args, (1,), "Fn::Not")
        return Raw(f"!{wrap_operand(hcl.inline(self.expr(value, depth)))}")

    def fn_import_value(self, args: Any, depth: int) -> Any:
        """Fn::ImportValue as a variable: Terraform has no stack exports."""
        if isinstance(args, str):
            export = args
        else:
            rendered = self.expr(args, depth)
            export = rendered if isinstance(rendered, str) else hcl.inline(rendered)
        variable = self.placeholder_variable(
            f"import_{export}",
            f"Value of the CloudFormation export '{export}' (Fn::ImportValue)",
        )
        self.note(
            f"Fn::ImportValue '{export}' became {variable}: Terraform has no stack exports; "
            "pass the value in or read it with a terraform_remote_state or data source"
        )
        return Raw(variable)

    def fn_to_json_string(self, args: Any, depth: int) -> Any:
        """Fn::ToJsonString (AWS::LanguageExtensions)."""
        return Call("jsonencode", self.expr(args, depth))

    def fn_length(self, args: Any, depth: int) -> Any:
        """Fn::Length (AWS::LanguageExtensions)."""
        if isinstance(args, list):
            return len(args)
        return Raw(f"length({self.text(args)})")

    def fn_transform(self, args: Any, depth: int) -> Any:
        """Fn::Transform runs a macro inside CloudFormation: reported, becomes null."""
        name = args.get("Name") if isinstance(args, dict) else None
        self.note(
            f"Fn::Transform ({name or 'a macro'}) runs inside CloudFormation and was not converted"
        )
        return None
