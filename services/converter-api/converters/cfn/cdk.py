"""Templates synthesized by the AWS CDK (`cdk synth`): remove CDK's own bookkeeping, name
Terraform resources after their construct paths, and point Lambda code at the cdk.out
asset folders.

We never run CDK code: the user runs `cdk synth` and sends the template it writes.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# Helper Lambda functions the CDK adds to implement a feature through a custom resource,
# recognised by their construct path, and what replaces them in Terraform.
_AUTO_DELETE = "Custom::S3AutoDeleteObjects"
_LOG_RETENTION = "Custom::LogRetention"
_RESTRICT_DEFAULT_SG = "Custom::VpcRestrictDefaultSG"
_HANDLED = (_AUTO_DELETE, _LOG_RETENTION, _RESTRICT_DEFAULT_SG)
_BOOTSTRAP_PARAMETER = "BootstrapVersion"
_BOOTSTRAP_RULE = "CheckBootstrapVersion"
_AUTO_DELETE_TAG = "aws-cdk:auto-delete-objects"
_PATH_SUFFIXES = ("Resource", "Default")
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")


def _path_name(path: str) -> str | None:
    """'MyStack/Api/Handler/Resource' -> 'Api_Handler'."""
    parts = [p for p in path.split("/")[1:] if p]
    while parts and parts[-1] in _PATH_SUFFIXES:
        parts.pop()
    if not parts:
        return None
    return "_".join(_NON_WORD.sub("_", part).strip("_") for part in parts if part.strip("_"))


def _references(value: Any, names: set[str]) -> bool:
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
        return any(_references(v, names) for v in value.values())
    if isinstance(value, list):
        return any(_references(v, names) for v in value)
    return False


def _service_token(resource: dict[str, Any]) -> str | None:
    token = (resource.get("Properties") or {}).get("ServiceToken")
    if isinstance(token, dict) and "Fn::GetAtt" in token:
        target = token["Fn::GetAtt"]
        return target[0] if isinstance(target, list) else str(target).split(".")[0]
    return None


def _role_of(resource: dict[str, Any]) -> str | None:
    role = (resource.get("Properties") or {}).get("Role")
    if isinstance(role, dict) and "Fn::GetAtt" in role:
        target = role["Fn::GetAtt"]
        return target[0] if isinstance(target, list) else str(target).split(".")[0]
    return None


def prepare_cdk(
    template: dict[str, Any], notes: list[str]
) -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
    """Returns (template, wanted Terraform names by logical id, converter hints)."""
    template = copy.deepcopy(template)
    resources: dict[str, dict[str, Any]] = template["Resources"]
    hints: dict[str, dict[str, Any]] = {}
    removed: set[str] = set()

    # CDK bookkeeping: version metadata and the bootstrap version check.
    for logical_id, resource in list(resources.items()):
        if resource.get("Type") == "AWS::CDK::Metadata":
            removed.add(logical_id)
    parameters = template.get("Parameters") or {}
    if _BOOTSTRAP_PARAMETER in parameters:
        del parameters[_BOOTSTRAP_PARAMETER]
        if not parameters:
            template.pop("Parameters")
    rules = template.get("Rules") or {}
    if _BOOTSTRAP_RULE in rules:
        del rules[_BOOTSTRAP_RULE]
        if not rules:
            template.pop("Rules")
    notes.append(
        "CDK template: CDK metadata and the bootstrap version check were left out; resource "
        "names come from the construct paths"
    )

    # Custom resources the CDK adds for features Terraform has built in.
    for logical_id, resource in list(resources.items()):
        kind = resource.get("Type")
        if kind not in _HANDLED:
            continue
        props = resource.get("Properties") or {}
        provider = _service_token(resource)
        removed.add(logical_id)
        if provider:
            removed.add(provider)
            role = _role_of(resources.get(provider, {}))
            if role:
                removed.add(role)
        if kind == _AUTO_DELETE:
            bucket = props.get("BucketName")
            bucket_id = bucket.get("Ref") if isinstance(bucket, dict) else None
            if isinstance(bucket_id, str) and bucket_id in resources:
                hints.setdefault(bucket_id, {})["force_destroy"] = True
                _drop_tag(resources[bucket_id], _AUTO_DELETE_TAG)
            notes.append(
                f"{logical_id} (CDK autoDeleteObjects) became force_destroy = true on the bucket"
            )
        elif kind == _LOG_RETENTION:
            group = props.get("LogGroupName")
            replacement = {"LogGroupName": group, "RetentionInDays": props.get("RetentionInDays")}
            resources[logical_id] = {
                "Type": "AWS::Logs::LogGroup",
                "Properties": replacement,
                "Metadata": resource.get("Metadata") or {},
            }
            removed.discard(logical_id)
            notes.append(
                f"{logical_id} (CDK logRetention) became a log group with that retention; import "
                "the existing log group or Terraform will fail to create it"
            )
        else:
            notes.append(
                f"{logical_id} (CDK restrictDefaultSecurityGroup) was left out: add an "
                "aws_default_security_group for the VPC with no rules to get the same effect"
            )

    # A provider role or function another resource still uses stays.
    for logical_id in list(removed):
        others = {k: v for k, v in resources.items() if k not in removed}
        if logical_id in resources and resources[logical_id].get("Type") != "AWS::CDK::Metadata":
            if _references(list(others.values()), {logical_id}) and not _only_in_policies(
                others, logical_id
            ):
                removed.discard(logical_id)
    for logical_id in removed:
        resources.pop(logical_id, None)
    _clean_references(template, removed)

    names: dict[str, str] = {}
    for logical_id, resource in resources.items():
        metadata = resource.get("Metadata")
        if not isinstance(metadata, dict):
            continue
        path = metadata.get("aws:cdk:path")
        if isinstance(path, str):
            name = _path_name(path)
            if name:
                names[logical_id] = name
        asset = metadata.get("aws:asset:path")
        prop = metadata.get("aws:asset:property")
        if isinstance(asset, str) and prop in ("Code", "Content"):
            hints.setdefault(logical_id, {})["code_path"] = f"cdk.out/{asset}"
            (resource.get("Properties") or {}).pop(prop, None)
    if any("code_path" in h for h in hints.values()):
        notes.append(
            "CDK assets: Lambda code is read from cdk.out/asset.*; run `cdk synth` and keep "
            "cdk.out next to the Terraform files"
        )

    conditions = template.get("Conditions") or {}
    if "CDKMetadataAvailable" in conditions and not _uses_condition(
        template, "CDKMetadataAvailable"
    ):
        del conditions["CDKMetadataAvailable"]
        if not conditions:
            template.pop("Conditions")
    return template, names, hints


def _uses_condition(template: dict[str, Any], name: str) -> bool:
    def walk(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("Condition") == name:
                return True
            if (
                "Fn::If" in value
                and isinstance(value["Fn::If"], list)
                and value["Fn::If"][:1] == [name]
            ):
                return True
            return any(walk(v) for v in value.values())
        if isinstance(value, list):
            return any(walk(v) for v in value)
        return False

    others = {k: v for k, v in (template.get("Conditions") or {}).items() if k != name}
    return walk(template["Resources"]) or walk(template.get("Outputs") or {}) or walk(others)


def _only_in_policies(resources: dict[str, dict[str, Any]], logical_id: str) -> bool:
    """True when the only references to `logical_id` are in bucket policy statements and
    DependsOn, which _clean_references removes."""
    for resource in resources.values():
        stripped = {k: v for k, v in resource.items() if k not in ("DependsOn", "Metadata")}
        if resource.get("Type") == "AWS::S3::BucketPolicy":
            continue
        if _references(stripped, {logical_id}):
            return False
    return True


def _drop_tag(resource: dict[str, Any], key: str) -> None:
    props = resource.get("Properties") or {}
    tags = props.get("Tags")
    if isinstance(tags, list):
        kept = [t for t in tags if not (isinstance(t, dict) and t.get("Key") == key)]
        if kept:
            props["Tags"] = kept
        else:
            props.pop("Tags")


def _clean_references(template: dict[str, Any], removed: set[str]) -> None:
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
                    s for s in document["Statement"] if not _references(s, removed)
                ]
                if not document["Statement"]:
                    empty_policies.append(logical_id)
    for logical_id in empty_policies:
        del template["Resources"][logical_id]
