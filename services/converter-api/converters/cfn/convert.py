"""CloudFormation template (already expanded from SAM, cleaned up from CDK) to Terraform files."""

from __future__ import annotations

import re
from typing import Any

from converters.cfn import hcl
from converters.cfn.awscc import awscc_type, convert_awscc
from converters.cfn.builder import Builder, snippet
from converters.cfn.context import Context, ParamInfo, ResourceInfo
from converters.cfn.hcl import Raw
from converters.cfn.naming import UniqueNames, identifier
from converters.cfn.parse import CfnError
from converters.cfn.resources import REGISTRY

PROVIDERS = ("aws", "awscc")
LAYOUTS = ("files", "single")
# 6.40 or later: DynamoDB global indexes use key_schema blocks, added in 6.30.
AWS_VERSION = "~> 6.40"
AWSCC_VERSION = "~> 1.0"
ARCHIVE_VERSION = "~> 2.0"
TERRAFORM_VERSION = ">= 1.5"

_SSM_TYPE = re.compile(r"^AWS::SSM::Parameter::Value<(.+)>$")
_LIST_TYPE = re.compile(r"^List<(.+)>$")


def _variable_type(cfn_type: str) -> str:
    inner = _SSM_TYPE.match(cfn_type)
    if inner:
        cfn_type = inner.group(1)
    if cfn_type == "Number":
        return "number"
    if cfn_type == "List<Number>":
        return "list(number)"
    if cfn_type == "CommaDelimitedList" or _LIST_TYPE.match(cfn_type):
        return "list(string)"
    return "string"


def _default(value: Any, tf_type: str) -> Any:
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
            return [_default(item, "number") for item in items]
        return items
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if value is not None else None


def _sentence(text: str) -> str:
    """'pick dev or prod' -> 'Pick dev or prod.'"""
    text = text.strip()
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "?", "!")) else text + "."


class Converter:
    def __init__(
        self,
        template: dict[str, Any],
        provider: str = "aws",
        stack_name: str | None = None,
        include_imports: bool = False,
        names: dict[str, str] | None = None,
        hints: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if provider not in PROVIDERS:
            raise CfnError(f"Unknown provider '{provider}'. Use one of: {', '.join(PROVIDERS)}")
        self.template = template
        self.ctx = Context(template, provider)
        self.stack_name = stack_name
        self.include_imports = include_imports
        self.wanted_names = names or {}
        self.hints = hints or {}
        self.variables: list[hcl.Block] = []
        self.locals: list[Any] = []
        self.resources: list[hcl.Block | hcl.Comment] = []
        self.outputs: list[hcl.Block] = []
        self.imports: list[hcl.Block] = []
        self.sensitive: set[str] = set()
        self.sensitive_locals: set[str] = set()
        self.report: list[dict[str, Any]] = []
        self.uses_archive = False

    # --- parameters -------------------------------------------------------------------

    def parameters(self) -> None:
        labels = self._parameter_labels()
        for cfn_name, spec in (self.template.get("Parameters") or {}).items():
            name = self.ctx.variable_names.take(identifier(cfn_name))
            cfn_type = str(spec["Type"])
            tf_type = _variable_type(cfn_type)
            ssm = _SSM_TYPE.match(cfn_type)
            info = ParamInfo(name, cfn_type, tf_type, ssm=name if ssm else None)
            self.ctx.params[cfn_name] = info

            body: list[Any] = []
            description = spec.get("Description") or labels.get(cfn_name)
            if ssm:
                description = (
                    f"{description + ' ' if description else ''}"
                    f"(name of the SSM parameter that holds the value)"
                ).strip()
            if description:
                body.append(hcl.Attr("description", str(description)))
            body.append(hcl.Attr("type", Raw("string" if ssm else tf_type)))
            if "Default" in spec:
                body.append(
                    hcl.Attr("default", _default(spec["Default"], "string" if ssm else tf_type))
                )
            if str(spec.get("NoEcho", "")).lower() == "true":
                body.append(hcl.Attr("sensitive", True))
                self.sensitive.add(name)
            if not ssm:
                validations = self._validations(name, spec, tf_type)
                if validations:
                    body.append(hcl.BLANK)
                    body.extend(validations)
            self.variables.append(hcl.Block("variable", [name], body))
            if ssm:
                self.ctx.data("aws_ssm_parameter", name, [hcl.Attr("name", Raw(f"var.{name}"))])

    def _parameter_labels(self) -> dict[str, str]:
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

    def _validations(self, name: str, spec: dict[str, Any], tf_type: str) -> list[hcl.Block]:
        ref = f"var.{name}"
        rules: list[tuple[str, str]] = []
        constraint = spec.get("ConstraintDescription")
        allowed = spec.get("AllowedValues")
        if isinstance(allowed, list) and allowed and not tf_type.startswith("list("):
            values = [_default(v, tf_type) for v in allowed]
            rules.append(
                (
                    f"contains({hcl.inline(values)}, {ref})",
                    f"Must be one of: {', '.join(str(v) for v in values)}.",
                )
            )
        if tf_type == "string":
            if spec.get("AllowedPattern"):
                pattern = str(spec["AllowedPattern"])
                rules.append(
                    (
                        f"can(regex({hcl.quote('^(?:' + pattern + ')$')}, {ref}))",
                        f"Must match the pattern {pattern}.",
                    )
                )
            for key, operator, words in (
                ("MinLength", ">=", "at least"),
                ("MaxLength", "<=", "at most"),
            ):
                if spec.get(key) is not None:
                    limit = _default(spec[key], "number")
                    rules.append(
                        (
                            f"length({ref}) {operator} {limit}",
                            f"Must be {words} {limit} characters.",
                        )
                    )
        if tf_type == "number":
            for key, operator, words in (
                ("MinValue", ">=", "at least"),
                ("MaxValue", "<=", "at most"),
            ):
                if spec.get(key) is not None:
                    limit = _default(spec[key], "number")
                    rules.append((f"{ref} {operator} {limit}", f"Must be {words} {limit}."))
        blocks = []
        for condition, message in rules:
            text = _sentence(str(constraint)) if constraint else message
            blocks.append(
                hcl.Block(
                    "validation",
                    [],
                    [hcl.Attr("condition", Raw(condition)), hcl.Attr("error_message", text)],
                )
            )
        return blocks

    # --- mappings and conditions --------------------------------------------------------

    def mappings_and_conditions(self) -> None:
        for name, mapping in (self.template.get("Mappings") or {}).items():
            local = self.ctx.local_names.take(identifier(name))
            self.ctx.mappings[str(name)] = local
            self.locals.append(hcl.Attr(local, self.ctx.expr(mapping)))
        conditions = self.template.get("Conditions") or {}
        for name in conditions:
            self.ctx.conditions[str(name)] = self.ctx.local_names.take(identifier(name))
        rendered: dict[str, str] = {}
        for name, condition in conditions.items():
            value = self.ctx.expr(condition)
            rendered[self.ctx.conditions[str(name)]] = hcl.render(value)
            self.locals.append(hcl.Attr(self.ctx.conditions[str(name)], value))
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
            mapping = REGISTRY.get(cfn_type) if self.ctx.provider == "aws" else None
            if mapping is not None:
                info = ResourceInfo(
                    logical_id,
                    cfn_type,
                    mapping.tf_type,
                    names.take(wanted),
                    ref=mapping.ref,
                    attrs=dict(mapping.attrs),
                    kind="aws",
                )
                info.conditional = bool(resource.get("Condition"))
                if mapping.prepare is not None:
                    mapping.prepare(info, resource)
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
            if info.kind == "aws":
                REGISTRY[info.cfn_type].convert(builder)
            else:
                awscc_types.add(info.cfn_type)
                convert_awscc(builder)
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
                "the awscc provider was requested"
                if self.ctx.provider == "awscc"
                else "no hand-written aws mapping yet"
            )
            self.ctx.note(
                f"{cfn_type} uses the awscc provider ({reason}): it mirrors CloudFormation's "
                f"schema, so check {awscc_type(cfn_type)} in the awscc docs"
            )

    def _resource_block(self, builder: Builder, resource: dict[str, Any]) -> hcl.Block:
        info = builder.info
        body: list[Any] = builder.count_items() + builder.props.items
        meta: list[Any] = []
        depends = resource.get("DependsOn")
        if depends:
            targets = depends if isinstance(depends, list) else [depends]
            addresses = []
            for target in targets:
                other = self.ctx.resources.get(str(target))
                if other is None:
                    raise CfnError(f"'{info.logical_id}' DependsOn unknown resource '{target}'")
                if other.kind != "unsupported":
                    addresses.append(Raw(other.address))
            if addresses:
                meta.append(hcl.Attr("depends_on", addresses))
        lifecycle = list(builder.lifecycle)
        policy = resource.get("DeletionPolicy")
        if policy in ("Retain", "RetainExceptOnCreate"):
            lifecycle.append(hcl.Attr("prevent_destroy", True))
            self.ctx.note(
                f"{info.logical_id} has DeletionPolicy {policy}: it became prevent_destroy, which "
                "makes `terraform destroy` fail instead of leaving the resource behind; use a "
                "`removed` block with `destroy = false` to let go of it"
            )
        elif policy == "Snapshot" and info.cfn_type not in (
            "AWS::RDS::DBInstance",
            "AWS::RDS::DBCluster",
        ):
            self.ctx.note(
                f"{info.logical_id} has DeletionPolicy Snapshot: Terraform takes no snapshot "
                "on destroy; take one by hand first"
            )
        # prevent_destroy also blocks replacement, so a Retain update policy is covered.
        if resource.get("UpdateReplacePolicy") == "Snapshot" or (
            resource.get("UpdateReplacePolicy") == "Retain"
            and policy not in ("Retain", "RetainExceptOnCreate")
        ):
            self.ctx.note(
                f"{info.logical_id} has UpdateReplacePolicy "
                f"{resource['UpdateReplacePolicy']}: Terraform has no equivalent"
            )
        for attribute in ("CreationPolicy", "UpdatePolicy"):
            if resource.get(attribute):
                self.ctx.note(
                    f"{info.logical_id} has a {attribute}: CloudFormation-only behaviour, not "
                    "converted"
                )
        metadata = resource.get("Metadata")
        if isinstance(metadata, dict) and "AWS::CloudFormation::Init" in metadata:
            self.ctx.note(
                f"{info.logical_id} uses AWS::CloudFormation::Init (cfn-init): not available "
                "outside CloudFormation; move it into user_data (cloud-init) or an AMI"
            )
        if lifecycle:
            meta.append(hcl.Block("lifecycle", [], lifecycle))
        if meta:
            body = body + ([hcl.BLANK] if body else []) + meta
        return hcl.Block("resource", [info.tf_type, info.name], body)

    def _unsupported(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        if info.cfn_type.startswith("Custom::") or info.cfn_type == (
            "AWS::CloudFormation::CustomResource"
        ):
            why = "custom resources run a Lambda function or SNS topic during stack operations"
        elif info.cfn_type.startswith("AWS::CloudFormation::"):
            why = "it only exists inside CloudFormation"
        else:
            why = "neither the aws mappings nor the awscc provider cover this type"
        self.ctx.note(f"{info.logical_id} ({info.cfn_type}) was not converted: {why}")
        self.resources.append(
            hcl.Comment(
                f"{info.logical_id} ({info.cfn_type}) was not converted, see the notes.\n"
                f"Properties: {snippet(resource.get('Properties') or {})}"
            )
        )

    def _import(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        mapping = REGISTRY.get(info.cfn_type) if info.kind == "aws" else None
        identifier_value: Any = None
        if mapping and mapping.import_id:
            value = (resource.get("Properties") or {}).get(mapping.import_id)
            if isinstance(value, str):
                identifier_value = value
        target = f"{info.address}[0]" if info.conditional else info.address
        body = [
            hcl.Attr("to", Raw(target)),
            hcl.Attr("id", identifier_value or f"TODO: physical ID of {info.logical_id}"),
        ]
        self.imports.append(hcl.Block("import", [], body))

    def _uses_sensitive(self, text: str) -> bool:
        return any(re.search(rf"\bvar\.{name}\b", text) for name in self.sensitive) or any(
            re.search(rf"\blocal\.{name}\b", text) for name in self.sensitive_locals
        )

    # --- outputs ----------------------------------------------------------------------

    def convert_outputs(self) -> None:
        for name, output in (self.template.get("Outputs") or {}).items():
            tf_name = self.ctx.output_names.take(identifier(name))
            value = self.ctx.expr(output["Value"])
            condition = output.get("Condition")
            if condition:
                value = Raw(f"{self.ctx.condition(condition)} ? {hcl.inline(value)} : null")
            body: list[Any] = []
            if output.get("Description"):
                body.append(hcl.Attr("description", str(output["Description"])))
            body.append(hcl.Attr("value", value))
            if self._uses_sensitive(hcl.render(value)):
                body.append(hcl.Attr("sensitive", True))
            export = output.get("Export")
            if isinstance(export, dict) and "Name" in export:
                export_name = self.ctx.expr(export["Name"])
                shown = export_name if isinstance(export_name, str) else hcl.inline(export_name)
                if shown.startswith('"') and shown.endswith('"'):
                    shown = shown[1:-1]
                self.ctx.note(
                    f"Output {name} was exported as '{shown}': Terraform has no exports; other "
                    "configurations can read it with terraform_remote_state"
                )
            self.outputs.append(hcl.Block("output", [tf_name], body))

    # --- files ------------------------------------------------------------------------

    def _versions(self) -> list[hcl.Block | hcl.Comment]:
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
        terraform = hcl.Block(
            "terraform",
            [],
            [
                hcl.Attr("required_version", TERRAFORM_VERSION),
                hcl.BLANK,
                hcl.Block("required_providers", [], [hcl.Attr(k, v) for k, v in providers.items()]),
            ],
        )
        blocks: list[hcl.Block | hcl.Comment] = [terraform]
        comment = hcl.Comment(
            "Region and credentials come from the environment (AWS_REGION, AWS_PROFILE...),\n"
            "as with the AWS CLI. Set region here to pin one."
        )
        if "aws" in providers:
            blocks += [comment, hcl.Block("provider", ["aws"], [])]
        if uses_awscc:
            if "aws" not in providers:
                blocks.append(comment)
            blocks.append(hcl.Block("provider", ["awscc"], []))
        return blocks

    def _used_types(self) -> list[str]:
        return [
            block.labels[0]
            for block in self.resources
            if isinstance(block, hcl.Block) and block.type == "resource"
        ] + [kind for kind, _ in self.ctx.data_sources]

    def _all_variables(self) -> list[hcl.Block]:
        blocks = list(self.variables)
        if self.ctx.stack_name_used:
            name = "stack_name"
            body: list[Any] = [
                hcl.Attr("description", "Name of the stack (replaces AWS::StackName)"),
                hcl.Attr("type", Raw("string")),
            ]
            if self.stack_name:
                body.append(hcl.Attr("default", self.stack_name))
            blocks.insert(0, hcl.Block("variable", [name], body))
        blocks.extend(self.ctx.extra_variables.values())
        return blocks

    def files(self, layout: str) -> list[dict[str, str]]:
        if layout not in LAYOUTS:
            raise CfnError(f"Unknown layout '{layout}'. Use one of: {', '.join(LAYOUTS)}")
        variables = self._all_variables()
        local_block = [hcl.Block("locals", [], self.locals)] if self.locals else []
        data = list(self.ctx.data_sources.values())
        main = data + self.resources
        import_blocks: list[hcl.Block | hcl.Comment] = []
        if self.imports:
            import_blocks = [
                hcl.Comment(
                    "Adopt the resources the stack already created instead of creating new\n"
                    "ones. Replace each TODO with the resource's physical ID:\n"
                    "  aws cloudformation describe-stack-resources --stack-name <stack>\n"
                    "Most types import by that ID; check the Import section of each\n"
                    "resource's docs. Set DeletionPolicy: Retain on every resource and\n"
                    "delete the stack only after `terraform plan` shows no changes."
                ),
                *self.imports,
            ]
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
        self.parameters()
        self.mappings_and_conditions()
        self.register()
        self.convert_resources()
        self.convert_outputs()
        return {"files": self.files(layout), "notes": self.ctx.notes, "resources": self.report}
