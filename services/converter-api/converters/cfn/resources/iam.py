"""IAM: roles, policies, instance profiles, users and groups.

Inline policies and managed policy attachments become separate resources
(aws_iam_role_policy, aws_iam_role_policy_attachment...): the provider deprecated the
inline_policy and managed_policy_arns arguments.
"""

from __future__ import annotations

from typing import Any

from converters.cfn.builder import Builder, snippet
from converters.cfn.context import ResourceInfo, intrinsic_name, json_value
from converters.cfn.hcl import Attr, Raw
from converters.cfn.naming import UniqueNames, identifier
from converters.cfn.resources import resource
from converters.cfn.resources.messaging import counted_refs, main_index, only_when, optional

# kind -> (inline policy type, attachment type, argument naming the principal)
_PRINCIPALS = {
    "role": ("aws_iam_role_policy", "aws_iam_role_policy_attachment", "role"),
    "user": ("aws_iam_user_policy", "aws_iam_user_policy_attachment", "user"),
    "group": ("aws_iam_group_policy", "aws_iam_group_policy_attachment", "group"),
}
# The properties of AWS::IAM::Policy and AWS::IAM::ManagedPolicy that list principals.
_TARGET_PROPS = (("role", "Roles"), ("user", "Users"), ("group", "Groups"))
_NO_VALUE = {"Ref": "AWS::NoValue"}


def _literal_text(value: Any) -> str | None:
    """The text of a literal string, a Fn::Sub template, or the literal parts of a Fn::Join
    (the CDK writes managed policy ARNs that way), for naming."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and intrinsic_name(value) == "Fn::Sub":
        args = value["Fn::Sub"]
        text = args[0] if isinstance(args, list) and args else args
        return text if isinstance(text, str) else None
    if isinstance(value, dict) and intrinsic_name(value) == "Fn::Join":
        args = value["Fn::Join"]
        if isinstance(args, list) and len(args) == 2 and isinstance(args[1], list):
            text = "".join(part for part in args[1] if isinstance(part, str))
            return text or None
    return None


def _target_name(value: Any) -> str | None:
    """A readable name for a principal: its literal name or the referenced logical id."""
    if isinstance(value, dict) and intrinsic_name(value) == "Ref":
        return str(value["Ref"])
    return _literal_text(value)


def _inline_policies(b: Builder, kind: str) -> None:
    """Inline policies ([{PolicyName, PolicyDocument}]) of the role, user or group itself."""
    policies = b.props.take("Policies")
    if policies is None:
        return
    tf_type, _, argument = _PRINCIPALS[kind]
    suffixes = UniqueNames()
    entries = policies if isinstance(policies, list) else [policies]
    for index, item in enumerate(entries, 1):
        if b.ctx.is_no_value(item):
            continue
        # An `!If [Cond, {...}, !Ref AWS::NoValue]` entry becomes a counted resource.
        when, policy = b.ctx.optional_item(item)
        if not isinstance(policy, dict) or intrinsic_name(policy) is not None:
            b.ctx.note(
                f"{b.logical_id}: inline policy {index} is computed by a function and was not "
                f"converted; add it as a {tf_type} by hand"
            )
            b.props.comment(f"Policies[{index - 1}] was not converted: {snippet(item)}")
            continue
        name = policy.get("PolicyName")
        label = _literal_text(name) or f"policy_{index}"
        suffix = suffixes.take("_" + identifier(label, f"policy_{index}"))
        items: list[Any] = [Attr(argument, b.self_ref("name"))]
        if name is not None:
            items.insert(0, Attr("name", b.ctx.expr(name)))
        items.append(Attr("policy", json_value(b.ctx, policy.get("PolicyDocument", {}))))
        b.add_resource(tf_type, suffix, items, when=when)


def _managed_policies(b: Builder, kind: str) -> None:
    """ManagedPolicyArns: one attachment each. Separate resources rather than for_each, so
    ARNs of policies created in the same configuration don't block the plan."""
    arns = b.props.take("ManagedPolicyArns")
    if arns is None:
        return
    _, tf_type, argument = _PRINCIPALS[kind]
    if not isinstance(arns, list):
        b.ctx.note(
            f"{b.logical_id}: ManagedPolicyArns is computed by a function and was not "
            "converted; attach the policies by hand"
        )
        b.props.comment(f"ManagedPolicyArns was not converted: {snippet(arns)}")
        return
    suffixes = UniqueNames()
    for index, item in enumerate(arns, 1):
        if b.ctx.is_no_value(item):
            continue
        when, arn = b.ctx.optional_item(item)
        text = _literal_text(arn)
        label = text.rsplit("/", 1)[-1] if text and "/" in text else f"policy_{index}"
        suffix = suffixes.take("_" + identifier(label, f"policy_{index}"))
        b.add_resource(
            tf_type,
            suffix,
            [Attr(argument, b.self_ref("name")), Attr("policy_arn", b.ctx.expr(arn))],
            when=when,
        )


@resource(
    "AWS::IAM::Role",
    "aws_iam_role",
    ref="name",
    attrs={"Arn": "arn", "RoleId": "unique_id"},
    import_id="RoleName",
)
def iam_role(b: Builder) -> None:
    p = b.props
    p.attr("name", "RoleName")
    p.attr("description", "Description")
    p.attr("path", "Path")
    p.json("assume_role_policy", "AssumeRolePolicyDocument")
    p.number("max_session_duration", "MaxSessionDuration")
    p.attr("permissions_boundary", "PermissionsBoundary")
    p.tags()
    _inline_policies(b, "role")
    _managed_policies(b, "role")


def _policy_targets(resource: dict[str, Any]) -> list[tuple[str, Any]]:
    """(kind, principal) for every role, user and group listed literally. A principal may be
    `!If [Cond, x, !Ref AWS::NoValue]`: it then gets a counted resource."""
    properties = resource.get("Properties") or {}
    targets: list[tuple[str, Any]] = []
    for kind, cfn_name in _TARGET_PROPS:
        values = properties.get(cfn_name)
        if isinstance(values, list):
            targets.extend((kind, value) for value in values if value != _NO_VALUE)
    return targets


def _take_targets(b: Builder) -> None:
    """Drop the principal lists that were expanded. A computed list is left for finish(),
    which keeps it as a comment and reports it."""
    for _, cfn_name in _TARGET_PROPS:
        if isinstance(b.props.peek(cfn_name), list):
            b.props.drop(cfn_name)


def _prepare_policy(info: ResourceInfo, resource: dict[str, Any]) -> None:
    """The main resource's type follows its principal (role, user or group): the first one
    that is always there. When every principal is optional, the main resource is counted."""
    targets = _policy_targets(resource)
    if targets:
        kind, principal = targets[main_index([value for _, value in targets])]
        info.tf_type = _PRINCIPALS[kind][0]
        if optional(principal):
            counted_refs(info)


@resource("AWS::IAM::Policy", "aws_iam_role_policy", prepare=_prepare_policy)
def iam_policy(b: Builder) -> None:
    """An inline policy for each listed role, user and group. The first principal that is
    always there gets the main resource; each further one gets its own copy."""
    p = b.props
    targets = _policy_targets(b.resource)
    _take_targets(b)
    name = p.take("PolicyName")
    policy = json_value(b.ctx, p.take("PolicyDocument", {}))
    principals: list[tuple[str, Any, str | None, str | None]] = []
    for kind, target in targets:
        when, value = b.ctx.optional_item(target)
        principals.append((kind, b.ctx.expr(value), _target_name(value), when))
    main = main_index([target for _, target in targets])
    if not principals:
        variable = b.ctx.placeholder_variable(
            f"{b.logical_id}_role", f"Role that {b.logical_id} (AWS::IAM::Policy) is attached to"
        )
        b.ctx.note(
            f"{b.logical_id} lists no role, user or group Terraform can read; it is attached "
            f"to {variable} instead"
        )
        principals = [("role", Raw(variable), None, None)]
    suffixes = UniqueNames()
    for index, (kind, value, label, when) in enumerate(principals):
        items: list[Any] = [Attr(_PRINCIPALS[kind][2], value), Attr("policy", policy)]
        if name is not None:
            items.insert(0, Attr("name", b.ctx.expr(name)))
        if index == main:
            p.items.extend(items)
            continue
        fallback = f"{kind}_{index + 1}"
        suffix = suffixes.take("_" + identifier(label or fallback, fallback))
        b.add_resource(_PRINCIPALS[kind][0], suffix, items, when=when)
    only_when(b, principals[main][3])


@resource(
    "AWS::IAM::ManagedPolicy",
    "aws_iam_policy",
    ref="arn",
    attrs={"PolicyArn": "arn", "PolicyId": "policy_id"},
)
def iam_managed_policy(b: Builder) -> None:
    p = b.props
    p.attr("name", "ManagedPolicyName")
    p.attr("description", "Description")
    p.attr("path", "Path")
    p.json("policy", "PolicyDocument")
    targets = _policy_targets(b.resource)
    _take_targets(b)
    suffixes = UniqueNames()
    for index, (kind, item) in enumerate(targets, 1):
        _, tf_type, argument = _PRINCIPALS[kind]
        when, target = b.ctx.optional_item(item)
        label = _target_name(target) or f"{kind}_{index}"
        suffix = suffixes.take("_" + identifier(label, f"{kind}_{index}"))
        b.add_resource(
            tf_type,
            suffix,
            [Attr(argument, b.ctx.expr(target)), Attr("policy_arn", b.self_ref("arn"))],
            when=when,
        )


@resource(
    "AWS::IAM::InstanceProfile",
    "aws_iam_instance_profile",
    ref="name",
    attrs={"Arn": "arn"},
    import_id="InstanceProfileName",
)
def iam_instance_profile(b: Builder) -> None:
    p = b.props
    p.attr("name", "InstanceProfileName")
    p.attr("path", "Path")
    roles = p.peek("Roles")
    if isinstance(roles, list) and len(roles) == 1:
        # IAM allows one role per instance profile, as does CloudFormation.
        p.drop("Roles")
        p.set("role", b.ctx.expr(roles[0]))


@resource(
    "AWS::IAM::User",
    "aws_iam_user",
    ref="name",
    attrs={"Arn": "arn"},
    import_id="UserName",
)
def iam_user(b: Builder) -> None:
    p = b.props
    b.name_attr("name", "UserName")
    p.attr("path", "Path")
    p.attr("permissions_boundary", "PermissionsBoundary")
    p.tags()
    groups = p.take("Groups")
    if groups is not None:
        b.add_resource(
            "aws_iam_user_group_membership",
            "_groups",
            [Attr("user", b.self_ref("name")), Attr("groups", b.ctx.expr(groups))],
        )
    _inline_policies(b, "user")
    _managed_policies(b, "user")


@resource(
    "AWS::IAM::Group",
    "aws_iam_group",
    ref="name",
    attrs={"Arn": "arn"},
    import_id="GroupName",
)
def iam_group(b: Builder) -> None:
    p = b.props
    b.name_attr("name", "GroupName")
    p.attr("path", "Path")
    _inline_policies(b, "group")
    _managed_policies(b, "group")
