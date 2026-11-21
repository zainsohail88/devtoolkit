"""CloudFormation template (already expanded from SAM, cleaned up from CDK) to Terraform files."""

from __future__ import annotations

import re
from typing import Any

from converters.cfn import hcl
from converters.cfn.awscc import AwsccConverter, awscc_type
from converters.cfn.builder import Builder, snippet
from converters.cfn.constants import (
    ARCHIVE_VERSION,
    AWS_VERSION,
    AWSCC_VERSION,
    LAYOUTS,
    PROVIDERS,
    TERRAFORM_VERSION,
)
from converters.cfn.context import Context
from converters.cfn.errors import CfnError
from converters.cfn.models import BLANK, Attr, Block, Comment, ParamInfo, Raw, ResourceInfo
from converters.cfn.naming import UniqueNames, identifier
from converters.cfn.resources.registry import REGISTRY

SSM_TYPE = re.compile(r"^AWS::SSM::Parameter::Value<(.+)>$")
LIST_TYPE = re.compile(r"^List<(.+)>$")


def variable_type(cfn_type: str) -> str:
    """The Terraform type of a CloudFormation parameter type."""
    inner = SSM_TYPE.match(cfn_type)
    if inner:
        cfn_type = inner.group(1)
    if cfn_type == "Number":
        return "number"
    if cfn_type == "List<Number>":
        return "list(number)"
    if cfn_type == "CommaDelimitedList" or LIST_TYPE.match(cfn_type):
        return "list(string)"
    return "string"


def parameter_default(value: Any, tf_type: str) -> Any:
    """A parameter default as a value of the variable's Terraform type."""
    if tf_type == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        return int(number) if number.is_integer() else number
    if tf_type.startswith("list("):
        items = value if isinstance(value, list) else str(value).split(",")
        items = [str(item).strip() for item in items if str(item).strip() != ""]
        if tf_type == "list(number)":
            return [parameter_default(item, "number") for item in items]
        return items
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if value is not None else None


def sentence(text: str) -> str:
    """'pick dev or prod' -> 'Pick dev or prod.'"""
    text = text.strip()
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "?", "!")) else text + "."


class Converter:
    """A CloudFormation template (expanded from SAM, cleaned up from CDK) to Terraform
    files: parameters, mappings, conditions, resources, outputs and imports."""

    def __init__(
        self,
        template: dict[str, Any],
        provider: str = "aws",
        stack_name: str | None = None,
        include_imports: bool = False,
        names: dict[str, str] | None = None,
        hints: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Convert `template` with `provider`. `names` are wanted Terraform names by logical
        id (from the CDK step) and `hints` facts for converters by logical id."""
        if provider not in PROVIDERS:
            raise CfnError(
                f"The provider '{provider}' is unknown. Use one of: {', '.join(PROVIDERS)}."
            )
        self.template = template
        self.ctx = Context(template, provider)
        self.stack_name = stack_name
        self.include_imports = include_imports
        self.wanted_names = names or {}
        self.hints = hints or {}
        self.variables: list[Block] = []
        self.locals: list[Any] = []
        self.resources: list[Block | Comment] = []
        self.outputs: list[Block] = []
        self.imports: list[Block] = []
        self.sensitive: set[str] = set()
        self.sensitive_locals: set[str] = set()
        self.report: list[dict[str, Any]] = []
        self.uses_archive = False

    # --- parameters -------------------------------------------------------------------

    def parameters(self) -> None:
        """Parameters as variables, with validation blocks."""
        labels = self._parameter_labels()
        for cfn_name, spec in (self.template.get("Parameters") or {}).items():
            name = self.ctx.variable_names.take(identifier(cfn_name))
            cfn_type = str(spec["Type"])
            tf_type = variable_type(cfn_type)
            ssm = SSM_TYPE.match(cfn_type)
            info = ParamInfo(name, cfn_type, tf_type, ssm=name if ssm else None)
            self.ctx.params[cfn_name] = info

            body: list[Any] = []
            description = spec.get("Description") or labels.get(cfn_name)
            if ssm:
                description = (
                    f"{description + ' ' if description else ''}(the name of the SSM parameter "
                    "that holds the value)"
                ).strip()
            if description:
                body.append(Attr("description", str(description)))
            body.append(Attr("type", Raw("string" if ssm else tf_type)))
            if "Default" in spec:
                body.append(
                    Attr(
                        "default", parameter_default(spec["Default"], "string" if ssm else tf_type)
                    )
                )
            if str(spec.get("NoEcho", "")).lower() == "true":
                body.append(Attr("sensitive", True))
                self.sensitive.add(name)
            if not ssm:
                validations = self._validations(name, spec, tf_type)
                if validations:
                    body.append(BLANK)
                    body.extend(validations)
            self.variables.append(Block("variable", [name], body))
            if ssm:
                self.ctx.data("aws_ssm_parameter", name, [Attr("name", Raw(f"var.{name}"))])

    def _parameter_labels(self) -> dict[str, str]:
        """Parameter labels from AWS::CloudFormation::Interface, for descriptions."""
        metadata = self.template.get("Metadata") or {}
        interface = (
            metadata.get("AWS::CloudFormation::Interface") if isinstance(metadata, dict) else None
        )
        labels = interface.get("ParameterLabels") if isinstance(interface, dict) else None
        if not isinstance(labels, dict):
            return {}
        return {
            str(k): str(v.get("default"))
            for k, v in labels.items()
            if isinstance(v, dict) and v.get("default")
        }

    def _validations(self, name: str, spec: dict[str, Any], tf_type: str) -> list[Block]:
        """Validation blocks from AllowedValues, AllowedPattern and the length and value limits."""
        ref = f"var.{name}"
        rules: list[tuple[str, str]] = []
        constraint = spec.get("ConstraintDescription")
        allowed = spec.get("AllowedValues")
        if isinstance(allowed, list) and allowed and not tf_type.startswith("list("):
            values = [parameter_default(v, tf_type) for v in allowed]
            rules.append(
                (
                    f"contains({hcl.inline(values)}, {ref})",
                    f"The value must be one of: {', '.join(str(v) for v in values)}.",
                )
            )
        if tf_type == "string":
            if spec.get("AllowedPattern"):
                pattern = str(spec["AllowedPattern"])
                rules.append(
                    (
                        f"can(regex({hcl.quote('^(?:' + pattern + ')$')}, {ref}))",
                        f"The value must match the pattern {pattern}.",
                    )
                )
            for key, operator, words in (
                ("MinLength", ">=", "at least"),
                ("MaxLength", "<=", "at most"),
            ):
                if spec.get(key) is not None:
                    limit = parameter_default(spec[key], "number")
                    rules.append(
                        (
                            f"length({ref}) {operator} {limit}",
                            f"The value must be {words} {limit} characters long.",
                        )
                    )
        if tf_type == "number":
            for key, operator, words in (
                ("MinValue", ">=", "at least"),
                ("MaxValue", "<=", "at most"),
            ):
                if spec.get(key) is not None:
                    limit = parameter_default(spec[key], "number")
                    rules.append(
                        (f"{ref} {operator} {limit}", f"The value must be {words} {limit}.")
                    )
        blocks = []
        for condition, message in rules:
            text = sentence(str(constraint)) if constraint else message
            blocks.append(
                Block(
                    "validation",
                    [],
                    [Attr("condition", Raw(condition)), Attr("error_message", text)],
                )
            )
        return blocks

    # --- mappings and conditions --------------------------------------------------------

    def mappings_and_conditions(self) -> None:
        """Mappings and conditions as local values."""
        for name, mapping in (self.template.get("Mappings") or {}).items():
            local = self.ctx.local_names.take(identifier(name))
            self.ctx.mappings[str(name)] = local
            self.locals.append(Attr(local, self.ctx.expr(mapping)))
        conditions = self.template.get("Conditions") or {}
        for name in conditions:
            self.ctx.conditions[str(name)] = self.ctx.local_names.take(identifier(name))
        rendered: dict[str, str] = {}
        for name, condition in conditions.items():
            value = self.ctx.expr(condition)
            rendered[self.ctx.conditions[str(name)]] = hcl.render(value)
            self.locals.append(Attr(self.ctx.conditions[str(name)], value))
        # A condition built from a NoEcho parameter is sensitive too, and so is anything
        # (an output with that Condition) that uses it.
        changed = True
        while changed:
            changed = False
            for local, text in rendered.items():
                if local not in self.sensitive_locals and self._uses_sensitive(text):
                    self.sensitive_locals.add(local)
                    changed = True

    # --- resources --------------------------------------------------------------------

    def register(self) -> None:
        """Decide every resource's Terraform type and name before converting any, so
        references can be resolved in any order."""
        names = UniqueNames()
        for logical_id, resource in self.template["Resources"].items():
            cfn_type = resource["Type"]
            wanted = identifier(self.wanted_names.get(logical_id) or logical_id)
            converter = REGISTRY.get(cfn_type) if self.ctx.provider == "aws" else None
            if converter is not None:
                info = ResourceInfo(
                    logical_id,
                    cfn_type,
                    converter.tf_type,
                    names.take(wanted),
                    ref=converter.ref,
                    attrs=dict(converter.attrs),
                    kind="aws",
                )
                info.conditional = bool(resource.get("Condition"))
                converter.prepare(info, resource)
            elif (tf_type := awscc_type(cfn_type)) is not None:
                info = ResourceInfo(logical_id, cfn_type, tf_type, names.take(wanted), kind="awscc")
            else:
                info = ResourceInfo(logical_id, cfn_type, "", wanted, kind="unsupported")
            # A prepare hook may have made the resource counted for its own reasons.
            info.conditional = info.conditional or bool(resource.get("Condition"))
            if info.tf_type:
                self.ctx.names_for(info.tf_type).reserve(info.name)
            self.ctx.resources[logical_id] = info

    def convert_resources(self) -> None:
        """Every resource, with its hand-written converter or with awscc."""
        awscc_types: set[str] = set()
        for logical_id, resource in self.template["Resources"].items():
            info = self.ctx.resources[logical_id]
            entry = {
                "logical_id": logical_id,
                "type": info.cfn_type,
                "address": None,
                "status": info.kind,
            }
            self.report.append(entry)
            if info.kind == "unsupported":
                self._unsupported(info, resource)
                continue
            builder = Builder(self.ctx, info, resource, hints=self.hints.get(logical_id, {}))
            converter = REGISTRY.get(info.cfn_type) if info.kind == "aws" else None
            if converter is not None:
                converter.convert(builder)
            else:
                awscc_types.add(info.cfn_type)
                AwsccConverter(builder).convert()
            builder.props.finish()
            block = self._resource_block(builder, resource)
            entry["address"] = info.address
            self.resources.append(block)
            self.resources.extend(builder.extra)
            if builder.hints.get("archive"):
                self.uses_archive = True
            if self.include_imports:
                self._import(info, resource)
        for cfn_type in sorted(awscc_types):
            reason = (
                "you chose the awscc provider"
                if self.ctx.provider == "awscc"
                else "the converter has no aws provider mapping for it yet"
            )
            self.ctx.note(
                f"{cfn_type} is converted with the awscc provider because {reason}. The awscc "
                f"arguments follow the CloudFormation schema, so check {awscc_type(cfn_type)} in "
                "the awscc provider docs."
            )

    def _resource_block(self, builder: Builder, resource: dict[str, Any]) -> Block:
        """The main resource block, with count, depends_on and lifecycle."""
        info = builder.info
        body: list[Any] = builder.main_count_items() + builder.props.items
        meta: list[Any] = []
        depends = resource.get("DependsOn")
        if depends:
            targets = depends if isinstance(depends, list) else [depends]
            addresses = []
            for target in targets:
                other = self.ctx.resources.get(str(target))
                if other is None:
                    raise CfnError(
                        f"'{info.logical_id}' has DependsOn '{target}', but the template has no "
                        "resource with that name."
                    )
                if other.kind != "unsupported":
                    addresses.append(Raw(other.address))
            if addresses:
                meta.append(Attr("depends_on", addresses))
        lifecycle = list(builder.lifecycle)
        policy = resource.get("DeletionPolicy")
        if policy in ("Retain", "RetainExceptOnCreate"):
            lifecycle.append(Attr("prevent_destroy", True))
            self.ctx.note(
                f"{info.logical_id} has DeletionPolicy {policy}, so the converter adds "
                "prevent_destroy. `terraform destroy` then fails, while CloudFormation would "
                "keep the resource. To let go of it, use a `removed` block with `destroy = "
                "false`."
            )
        elif policy == "Snapshot" and info.cfn_type not in (
            "AWS::RDS::DBInstance",
            "AWS::RDS::DBCluster",
        ):
            self.ctx.note(
                f"{info.logical_id} has DeletionPolicy Snapshot, but Terraform takes no snapshot "
                "on destroy. Take a snapshot by hand before you destroy it."
            )
        # prevent_destroy also blocks replacement, so a Retain update policy is covered.
        if resource.get("UpdateReplacePolicy") == "Snapshot" or (
            resource.get("UpdateReplacePolicy") == "Retain"
            and policy not in ("Retain", "RetainExceptOnCreate")
        ):
            self.ctx.note(
                f"{info.logical_id}: UpdateReplacePolicy {resource['UpdateReplacePolicy']} has no "
                "Terraform equivalent, so it was left out. Check `terraform plan` for "
                "replacements before you apply."
            )
        for attribute in ("CreationPolicy", "UpdatePolicy"):
            if resource.get(attribute):
                self.ctx.note(
                    f"{info.logical_id}: {attribute} only works in CloudFormation, so it was left "
                    "out. Handle signals or rolling updates another way if you rely on it."
                )
        metadata = resource.get("Metadata")
        if isinstance(metadata, dict) and "AWS::CloudFormation::Init" in metadata:
            self.ctx.note(
                f"{info.logical_id} uses AWS::CloudFormation::Init (cfn-init), which only runs in "
                "CloudFormation. Move that setup into user_data (cloud-init) or an AMI."
            )
        if lifecycle:
            meta.append(Block("lifecycle", [], lifecycle))
        if meta:
            body = body + ([BLANK] if body else []) + meta
        return Block("resource", [info.tf_type, info.name], body)

    def _unsupported(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """A resource without a Terraform resource: a note and a comment."""
        if info.cfn_type.startswith("Custom::") or info.cfn_type == (
            "AWS::CloudFormation::CustomResource"
        ):
            why = (
                "Custom resources call a Lambda function or SNS topic during stack operations, "
                "which only CloudFormation does. Rebuild that logic in Terraform or run it "
                "another way."
            )
        elif info.cfn_type.startswith("AWS::CloudFormation::"):
            why = (
                "This type only exists inside CloudFormation. Replace it with a Terraform "
                "feature, such as a module for a nested stack."
            )
        else:
            why = (
                "Neither the aws mappings nor the awscc provider covers this type. Write the "
                "Terraform resource by hand."
            )
        self.ctx.note(f"{info.logical_id} ({info.cfn_type}) was left out. {why}")
        self.resources.append(
            Comment(
                f"{info.logical_id} ({info.cfn_type}) was left out. See the notes for the "
                "reason.\n"
                f"CloudFormation properties: {snippet(resource.get('Properties') or {})}"
            )
        )

    def _import(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """An import block for the resource."""
        converter = REGISTRY.get(info.cfn_type) if info.kind == "aws" else None
        identifier_value: Any = None
        if converter is not None and converter.import_id:
            value = (resource.get("Properties") or {}).get(converter.import_id)
            if isinstance(value, str):
                identifier_value = value
        target = f"{info.address}[0]" if info.conditional else info.address
        body = [
            Attr("to", Raw(target)),
            Attr("id", identifier_value or f"TODO: the physical ID of {info.logical_id}"),
        ]
        self.imports.append(Block("import", [], body))

    def _uses_sensitive(self, text: str) -> bool:
        """Whether an expression reads a NoEcho parameter or a condition built from one."""
        return any(re.search(rf"\bvar\.{name}\b", text) for name in self.sensitive) or any(
            re.search(rf"\blocal\.{name}\b", text) for name in self.sensitive_locals
        )

    # --- outputs ----------------------------------------------------------------------

    def convert_outputs(self) -> None:
        """Outputs, with notes for exports."""
        for name, output in (self.template.get("Outputs") or {}).items():
            tf_name = self.ctx.output_names.take(identifier(name))
            value = self.ctx.expr(output["Value"])
            condition = output.get("Condition")
            if condition:
                value = Raw(f"{self.ctx.condition(condition)} ? {hcl.inline(value)} : null")
            body: list[Any] = []
            if output.get("Description"):
                body.append(Attr("description", str(output["Description"])))
            body.append(Attr("value", value))
            if self._uses_sensitive(hcl.render(value)):
                body.append(Attr("sensitive", True))
            export = output.get("Export")
            if isinstance(export, dict) and "Name" in export:
                export_name = self.ctx.expr(export["Name"])
                shown = export_name if isinstance(export_name, str) else hcl.inline(export_name)
                if shown.startswith('"') and shown.endswith('"'):
                    shown = shown[1:-1]
                self.ctx.note(
                    f"Output {name} is exported as '{shown}'. Terraform has no stack exports, so "
                    "other configurations read the output with terraform_remote_state."
                )
            self.outputs.append(Block("output", [tf_name], body))

    # --- files ------------------------------------------------------------------------

    def _versions(self) -> list[Block | Comment]:
        """The terraform block and the provider blocks for the providers in use."""
        uses_aws = any(t.startswith("aws_") for t in self._used_types()) or bool(
            self.ctx.data_sources
        )
        uses_awscc = any(t.startswith("awscc_") for t in self._used_types())
        providers: dict[str, Any] = {}
        if uses_aws or not uses_awscc:
            providers["aws"] = {"source": "hashicorp/aws", "version": AWS_VERSION}
        if uses_awscc:
            providers["awscc"] = {"source": "hashicorp/awscc", "version": AWSCC_VERSION}
        if self.uses_archive:
            providers["archive"] = {"source": "hashicorp/archive", "version": ARCHIVE_VERSION}
        terraform = Block(
            "terraform",
            [],
            [
                Attr("required_version", TERRAFORM_VERSION),
                BLANK,
                Block("required_providers", [], [Attr(k, v) for k, v in providers.items()]),
            ],
        )
        blocks: list[Block | Comment] = [terraform]
        comment = Comment(
            "Region and credentials come from the environment (AWS_REGION, AWS_PROFILE...),\n"
            "as with the AWS CLI. Set region here to pin one."
        )
        if "aws" in providers:
            blocks += [comment, Block("provider", ["aws"], [])]
        if uses_awscc:
            if "aws" not in providers:
                blocks.append(comment)
            blocks.append(Block("provider", ["awscc"], []))
        return blocks

    def _used_types(self) -> list[str]:
        """Every resource and data source type in use."""
        return [
            block.labels[0]
            for block in self.resources
            if isinstance(block, Block) and block.type == "resource"
        ] + [kind for kind, _ in self.ctx.data_sources]

    def _all_variables(self) -> list[Block]:
        """Parameter variables, stack_name when used, and placeholder variables."""
        blocks = list(self.variables)
        if self.ctx.stack_name_used:
            name = "stack_name"
            body: list[Any] = [
                Attr(
                    "description", "The stack name, used wherever the template reads AWS::StackName"
                ),
                Attr("type", Raw("string")),
            ]
            if self.stack_name:
                body.append(Attr("default", self.stack_name))
            blocks.insert(0, Block("variable", [name], body))
        blocks.extend(self.ctx.extra_variables.values())
        return blocks

    def files(self, layout: str) -> list[dict[str, str]]:
        """The Terraform files, one per kind or a single main.tf."""
        if layout not in LAYOUTS:
            raise CfnError(f"The layout '{layout}' is unknown. Use one of: {', '.join(LAYOUTS)}.")
        variables = self._all_variables()
        local_block = [Block("locals", [], self.locals)] if self.locals else []
        data = list(self.ctx.data_sources.values())
        main = data + self.resources
        import_blocks: list[Block | Comment] = []
        if self.imports:
            import_blocks = [
                Comment(
                    "These import blocks adopt the resources the stack already created.\n"
                    "Replace each TODO with the physical ID of the resource. To list them, run:\n"
                    "  aws cloudformation describe-stack-resources --stack-name <stack>\n"
                    "Most types import by that ID. The Import section in the Terraform docs of\n"
                    "each resource type says which ID it takes. Set DeletionPolicy: Retain on\n"
                    "every resource, then delete the stack once `terraform plan` shows no "
                    "changes."
                )
            ] + self.imports
        if layout == "single":
            blocks = self._versions() + variables + local_block + main + self.outputs
            return [{"path": "main.tf", "content": hcl.render_file(blocks + import_blocks)}]
        files = [{"path": "versions.tf", "content": hcl.render_file(self._versions())}]
        if variables:
            files.append({"path": "variables.tf", "content": hcl.render_file(variables)})
        if local_block:
            files.append({"path": "locals.tf", "content": hcl.render_file(local_block)})
        files.append({"path": "main.tf", "content": hcl.render_file(main)})
        if self.outputs:
            files.append({"path": "outputs.tf", "content": hcl.render_file(self.outputs)})
        if import_blocks:
            files.append({"path": "imports.tf", "content": hcl.render_file(import_blocks)})
        return files

    def run(self, layout: str = "files") -> dict[str, Any]:
        """Convert the template: files, notes and where each resource went."""
        self.parameters()
        self.mappings_and_conditions()
        self.register()
        self.convert_resources()
        self.convert_outputs()
        return {"files": self.files(layout), "notes": self.ctx.notes, "resources": self.report}
