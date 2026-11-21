"""The state of one template's conversion: names, notes, the data sources and variables
that references create, and where every parameter, condition, mapping and resource goes.
Context also resolves the names expressions refer to (see ExpressionConverter)."""

from __future__ import annotations

import re
from typing import Any

from converters.cfn.constants import PSEUDO_DATA
from converters.cfn.errors import CfnError
from converters.cfn.expressions import ExpressionConverter
from converters.cfn.models import Attr, Block, ParamInfo, Raw, ResourceInfo
from converters.cfn.naming import UniqueNames, identifier, snake

ATTRIBUTE = re.compile(r"^[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*$")


class Context(ExpressionConverter):
    """Everything the conversion of one template accumulates."""

    def __init__(self, template: dict[str, Any], provider: str = "aws") -> None:
        """Empty state for converting `template` with `provider`."""
        super().__init__()
        self.template = template
        self.provider = provider
        self.notes: list[str] = []
        self.note_set: set[str] = set()
        self.params: dict[str, ParamInfo] = {}
        self.resources: dict[str, ResourceInfo] = {}
        self.conditions: dict[str, str] = {}  # CloudFormation condition name -> local name
        self.mappings: dict[str, str] = {}  # CloudFormation mapping name -> local name
        self.data_sources: dict[tuple[str, str], Block] = {}
        self.extra_variables: dict[str, Block] = {}
        self.variable_names = UniqueNames()
        self.local_names = UniqueNames()
        self.output_names = UniqueNames()
        # Terraform names per resource or data source type, so the extra resources a
        # converter adds never collide with each other or with converted resources.
        self.type_names: dict[str, UniqueNames] = {}
        # Always there for AWS::StackName, so no parameter takes the name.
        self.variable_names.reserve("stack_name")
        self.stack_name_used = False
        self.parameter_types = {
            name: str(spec.get("Type"))
            for name, spec in (template.get("Parameters") or {}).items()
            if isinstance(spec, dict)
        }

    def note(self, message: str) -> None:
        """Record something the user must look at. Duplicates are dropped."""
        if message not in self.note_set:
            self.note_set.add(message)
            self.notes.append(message)

    def names_for(self, kind: str) -> UniqueNames:
        """The pool of names for one resource or data source type."""
        return self.type_names.setdefault(kind, UniqueNames())

    def data(self, kind: str, name: str, body: list[Any] | None = None) -> str:
        """Register `data "<kind>" "<name>"` once and return its address."""
        if (kind, name) not in self.data_sources:
            self.data_sources[(kind, name)] = Block("data", [kind, name], body or [])
        return f"data.{kind}.{name}"

    def placeholder_variable(self, wanted: str, description: str) -> str:
        """A variable standing in for a value Terraform has no way to compute (another
        stack's export, an attribute of a resource that was not converted). Returns
        `var.<name>`."""
        entry = f"{wanted}\0{description}"
        if entry not in self.extra_variables:
            name = self.variable_names.take(identifier(wanted))
            self.extra_variables[entry] = Block(
                "variable",
                [name],
                [Attr("description", description), Attr("type", Raw("string"))],
            )
        return f"var.{self.extra_variables[entry].labels[0]}"

    def parameter_type(self, name: str) -> str:
        """The CloudFormation type of a parameter, or ""."""
        return self.parameter_types.get(name, "")

    def mapping_local(self, name: Any) -> str:
        """`local.<name>` for a mapping of the template."""
        if not isinstance(name, str) or name not in self.mappings:
            raise CfnError(
                f"'Fn::FindInMap' points to the mapping '{name}', but the template has no mapping "
                "with that name."
            )
        return f"local.{self.mappings[name]}"

    def condition(self, name: Any) -> str:
        """`local.<name>` for a condition of the template."""
        if not isinstance(name, str) or name not in self.conditions:
            raise CfnError(
                f"The template uses the condition '{name}', but Conditions has no entry with that "
                "name."
            )
        return f"local.{self.conditions[name]}"

    def ref(self, name: Any) -> Any:
        """The value of `Ref name`: a variable, a pseudo parameter or a resource."""
        if not isinstance(name, str):
            raise CfnError("'Ref' needs the name of a parameter or resource as a string.")
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
                "AWS::StackId has no Terraform equivalent, so the converter uses the stack name "
                "(var.stack_name) instead. Check that a name works where the template expects a "
                "stack ID."
            )
            return Raw("var.stack_name")
        if name == "AWS::NotificationARNs":
            self.note(
                "AWS::NotificationARNs has no Terraform equivalent, so the converter uses an "
                "empty list. Add the SNS topic ARNs by hand if you need them."
            )
            return []
        if name in self.params:
            return Raw(self.param_expression(name))
        if name in self.resources:
            info = self.resources[name]
            if info.kind == "unsupported":
                return Raw(self.unresolved(info, "Ref", "The Ref value"))
            return Raw(info.expression(info.ref))
        raise CfnError(
            f"'Ref' points to '{name}', but the template has no parameter or resource with that "
            "name."
        )

    def param_expression(self, name: str) -> str:
        """The expression for a parameter: its variable, or the SSM value it names."""
        info = self.params[name]
        if info.ssm is not None:
            value = f"data.aws_ssm_parameter.{info.ssm}.insecure_value"
            if info.tf_type.startswith("list("):
                return f'split(",", {value})'
            return value
        return f"var.{info.name}"

    def get_att(self, args: Any) -> Any:
        """The value of `Fn::GetAtt [resource, attribute]`."""
        if isinstance(args, str):
            resource, _, attribute = args.partition(".")
            args = [resource, attribute]
        if not isinstance(args, list) or len(args) != 2:
            raise CfnError(
                "'Fn::GetAtt' needs a resource and an attribute, written as [resource, "
                "attribute] or 'Resource.Attribute'."
            )
        logical_id, attribute = args
        if not isinstance(logical_id, str) or logical_id not in self.resources:
            raise CfnError(
                f"'Fn::GetAtt' points to '{logical_id}', but the template has no resource with "
                "that name."
            )
        if not isinstance(attribute, str):
            # An attribute name chosen at deploy time (e.g. !GetAtt [R, !Ref Param]).
            self.note(
                f"Fn::GetAtt on '{logical_id}' builds its attribute name at deploy time, so the "
                "value is null. Set it by hand."
            )
            return None
        info = self.resources[logical_id]
        if info.kind == "unsupported":
            return Raw(self.unresolved(info, attribute, f"The '{attribute}' attribute"))
        if attribute in info.attrs:
            return Raw(info.expression(info.attrs[attribute]))
        if not ATTRIBUTE.match(attribute):
            raise CfnError(
                f"'Fn::GetAtt' on '{logical_id}' asks for the attribute '{attribute}', but that "
                "name is invalid. Use letters and digits, with dots between parts."
            )
        tf_attribute = ".".join(snake(part) for part in attribute.split("."))
        if info.kind == "aws":
            self.note(
                f"'{logical_id}.{attribute}' is converted to '{info.address}.{tf_attribute}'. "
                "Check in the Terraform docs that this attribute exists."
            )
        return Raw(info.expression(tf_attribute))

    def unresolved(self, info: ResourceInfo, attribute: str, what: str) -> str:
        """A placeholder variable for an attribute of a resource that was not converted."""
        wanted = f"{info.logical_id}_{attribute}" if attribute != "Ref" else info.logical_id
        return self.placeholder_variable(
            wanted,
            f"{what} of {info.logical_id} ({info.cfn_type}). The converter left that resource "
            "out, so set this value by hand.",
        )
