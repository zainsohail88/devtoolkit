"""Templates synthesized by the AWS CDK (`cdk synth`): remove the bookkeeping of the CDK,
name Terraform resources after their construct paths and point Lambda code at the cdk.out
asset folders.

The converter never runs CDK code: the user runs `cdk synth` and sends the template it
writes.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from converters.cfn.cdk_constants import (
    AUTO_DELETE,
    AUTO_DELETE_TAG,
    BOOTSTRAP_PARAMETER,
    BOOTSTRAP_RULE,
    HANDLED,
    LOG_RETENTION,
    METADATA_CONDITION,
    METADATA_TYPE,
    NON_WORD,
    PATH_SUFFIXES,
    SUB_NAME,
)


def path_name(path: str) -> str | None:
    """'MyStack/Api/Handler/Resource' -> 'Api_Handler'."""
    parts = [p for p in path.split("/")[1:] if p]
    while parts and parts[-1] in PATH_SUFFIXES:
        parts.pop()
    if not parts:
        return None
    return "_".join(NON_WORD.sub("_", part).strip("_") for part in parts if part.strip("_"))


def references(value: Any, names: set[str]) -> bool:
    """Whether `value` refers (Ref, Fn::GetAtt, Fn::Sub) to any of `names`."""
    if isinstance(value, dict):
        if set(value) == {"Ref"}:
            return value["Ref"] in names
        if set(value) == {"Fn::GetAtt"}:
            target = value["Fn::GetAtt"]
            first = target[0] if isinstance(target, list) and target else str(target).split(".")[0]
            return first in names
        if set(value) == {"Fn::Sub"}:
            text = value["Fn::Sub"][0] if isinstance(value["Fn::Sub"], list) else value["Fn::Sub"]
            if isinstance(text, str) and any(
                re.search(r"\$\{" + re.escape(n) + r"[.}]", text) for n in names
            ):
                return True
        return any(references(v, names) for v in value.values())
    if isinstance(value, list):
        return any(references(v, names) for v in value)
    return False


def get_att_target(resource: dict[str, Any], prop: str) -> str | None:
    """The logical id in a `Fn::GetAtt` held by property `prop` of `resource`."""
    value = (resource.get("Properties") or {}).get(prop)
    if isinstance(value, dict) and "Fn::GetAtt" in value:
        target = value["Fn::GetAtt"]
        return target[0] if isinstance(target, list) else str(target).split(".")[0]
    return None


def mentions_condition(value: Any, name: str) -> bool:
    """Whether `value` uses condition `name` (a Condition attribute or Fn::If)."""
    if isinstance(value, dict):
        if value.get("Condition") == name:
            return True
        if (
            "Fn::If" in value
            and isinstance(value["Fn::If"], list)
            and value["Fn::If"][:1] == [name]
        ):
            return True
        return any(mentions_condition(v, name) for v in value.values())
    if isinstance(value, list):
        return any(mentions_condition(v, name) for v in value)
    return False


def uses_condition(template: dict[str, Any], name: str) -> bool:
    """Whether a resource, output or other condition of `template` uses condition `name`."""
    others = {k: v for k, v in (template.get("Conditions") or {}).items() if k != name}
    return (
        mentions_condition(template["Resources"], name)
        or mentions_condition(template.get("Outputs") or {}, name)
        or mentions_condition(others, name)
    )


def referenced_ids(value: Any) -> set[str]:
    """Every logical id `value` refers to through Ref, Fn::GetAtt or Fn::Sub, in one walk."""
    found: set[str] = set()
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if set(node) == {"Ref"} and isinstance(node["Ref"], str):
                found.add(node["Ref"])
            elif set(node) == {"Fn::GetAtt"}:
                target = node["Fn::GetAtt"]
                first = target[0] if isinstance(target, list) and target else str(target)
                found.add(str(first).split(".")[0])
            elif set(node) == {"Fn::Sub"}:
                args = node["Fn::Sub"]
                text = args[0] if isinstance(args, list) and args else args
                if isinstance(text, str):
                    found.update(m.split(".")[0] for m in SUB_NAME.findall(text))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def drop_tag(resource: dict[str, Any], key: str) -> None:
    """Remove the tag `key` from a resource with a [{Key, Value}] tag list."""
    props = resource.get("Properties") or {}
    tags = props.get("Tags")
    if isinstance(tags, list):
        kept = [t for t in tags if not (isinstance(t, dict) and t.get("Key") == key)]
        if kept:
            props["Tags"] = kept
        else:
            props.pop("Tags")


def clean_references(template: dict[str, Any], removed: set[str]) -> None:
    """DependsOn entries and bucket policy statements that pointed at removed resources."""
    if not removed:
        return
    empty_policies = []
    for logical_id, resource in template["Resources"].items():
        depends = resource.get("DependsOn")
        if depends is not None:
            kept = [
                d for d in (depends if isinstance(depends, list) else [depends]) if d not in removed
            ]
            if kept:
                resource["DependsOn"] = kept
            else:
                resource.pop("DependsOn")
        if resource.get("Type") == "AWS::S3::BucketPolicy":
            document = (resource.get("Properties") or {}).get("PolicyDocument")
            if isinstance(document, dict) and isinstance(document.get("Statement"), list):
                document["Statement"] = [
                    s for s in document["Statement"] if not references(s, removed)
                ]
                if not document["Statement"]:
                    empty_policies.append(logical_id)
    for logical_id in empty_policies:
        del template["Resources"][logical_id]


def drop_entry(template: dict[str, Any], section: str, name: str) -> None:
    """Remove `name` from a top-level section, and the section once it is empty."""
    entries = template.get(section) or {}
    if name in entries:
        del entries[name]
        if not entries:
            template.pop(section)


class CdkTemplate:
    """One synthesized template being cleaned up for the converter."""

    def __init__(self, template: dict[str, Any], notes: list[str]) -> None:
        """Work on a copy of `template`; notes go to `notes`."""
        self.template = copy.deepcopy(template)
        self.notes = notes
        self.resources: dict[str, dict[str, Any]] = self.template["Resources"]
        self.hints: dict[str, dict[str, Any]] = {}
        self.names: dict[str, str] = {}
        self.removed: set[str] = set()

    def prepare(self) -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
        """Returns (template, wanted Terraform names by logical id, converter hints)."""
        self.drop_bookkeeping()
        for logical_id, resource in list(self.resources.items()):
            if resource.get("Type") in HANDLED:
                self.replace_custom_resource(logical_id, resource)
        self.remove_unused()
        self.name_resources()
        if METADATA_CONDITION in (self.template.get("Conditions") or {}) and not uses_condition(
            self.template, METADATA_CONDITION
        ):
            drop_entry(self.template, "Conditions", METADATA_CONDITION)
        return self.template, self.names, self.hints

    def drop_bookkeeping(self) -> None:
        """CDK bookkeeping: version metadata and the bootstrap version check."""
        for logical_id, resource in self.resources.items():
            if resource.get("Type") == METADATA_TYPE:
                self.removed.add(logical_id)
        drop_entry(self.template, "Parameters", BOOTSTRAP_PARAMETER)
        drop_entry(self.template, "Rules", BOOTSTRAP_RULE)
        self.notes.append(
            "CDK template: CDK metadata and the bootstrap version check were left out; resource "
            "names come from the construct paths"
        )

    def replace_custom_resource(self, logical_id: str, resource: dict[str, Any]) -> None:
        """A custom resource the CDK adds for a feature Terraform has built in: remove it
        and its provider function and role, and keep the feature another way."""
        kind = resource.get("Type")
        props = resource.get("Properties") or {}
        provider = get_att_target(resource, "ServiceToken")
        self.removed.add(logical_id)
        if provider:
            self.removed.add(provider)
            role = get_att_target(self.resources.get(provider, {}), "Role")
            if role:
                self.removed.add(role)
        if kind == AUTO_DELETE:
            bucket = props.get("BucketName")
            bucket_id = bucket.get("Ref") if isinstance(bucket, dict) else None
            if isinstance(bucket_id, str) and bucket_id in self.resources:
                self.hints.setdefault(bucket_id, {})["force_destroy"] = True
                drop_tag(self.resources[bucket_id], AUTO_DELETE_TAG)
            self.notes.append(
                f"{logical_id} (CDK autoDeleteObjects) became force_destroy = true on the bucket"
            )
        elif kind == LOG_RETENTION:
            group = props.get("LogGroupName")
            replacement = {"LogGroupName": group, "RetentionInDays": props.get("RetentionInDays")}
            self.resources[logical_id] = {
                "Type": "AWS::Logs::LogGroup",
                "Properties": replacement,
                "Metadata": resource.get("Metadata") or {},
            }
            self.removed.discard(logical_id)
            self.notes.append(
                f"{logical_id} (CDK logRetention) became a log group with that retention; import "
                "the existing log group or Terraform will fail to create it"
            )
        else:
            self.notes.append(
                f"{logical_id} (CDK restrictDefaultSecurityGroup) was left out: add an "
                "aws_default_security_group for the VPC with no rules to get the same effect"
            )

    def remove_unused(self) -> None:
        """Remove what was marked, except a provider role or function another resource
        still uses. References from bucket policies and DependsOn do not count:
        clean_references removes those."""
        still_used: set[str] = set()
        for logical_id, resource in self.resources.items():
            if logical_id in self.removed or resource.get("Type") == "AWS::S3::BucketPolicy":
                continue
            stripped = {k: v for k, v in resource.items() if k not in ("DependsOn", "Metadata")}
            still_used |= referenced_ids(stripped)
        self.removed = {
            logical_id
            for logical_id in self.removed
            if logical_id not in still_used
            or self.resources[logical_id].get("Type") == METADATA_TYPE
        }
        for logical_id in self.removed:
            self.resources.pop(logical_id, None)
        clean_references(self.template, self.removed)

    def name_resources(self) -> None:
        """Terraform names from the construct paths, and code_path hints for assets."""
        for logical_id, resource in self.resources.items():
            metadata = resource.get("Metadata")
            if not isinstance(metadata, dict):
                continue
            path = metadata.get("aws:cdk:path")
            if isinstance(path, str):
                name = path_name(path)
                if name:
                    self.names[logical_id] = name
            asset = metadata.get("aws:asset:path")
            prop = metadata.get("aws:asset:property")
            if isinstance(asset, str) and prop in ("Code", "Content"):
                self.hints.setdefault(logical_id, {})["code_path"] = f"cdk.out/{asset}"
                (resource.get("Properties") or {}).pop(prop, None)
        if any("code_path" in h for h in self.hints.values()):
            self.notes.append(
                "CDK assets: Lambda code is read from cdk.out/asset.*; run `cdk synth` and keep "
                "cdk.out next to the Terraform files"
            )


def prepare_cdk(
    template: dict[str, Any], notes: list[str]
) -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
    """Returns (template, wanted Terraform names by logical id, converter hints)."""
    return CdkTemplate(template, notes).prepare()
