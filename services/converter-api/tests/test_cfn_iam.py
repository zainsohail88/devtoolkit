"""IAM mappings: roles, policies, instance profiles, users and groups."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from converters.cfn.pipeline import convert_template

FIXTURES = Path(__file__).parent / "fixtures" / "cfn"
TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}
DOC = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}],
}


def convert(
    resources: dict,
    parameters: dict | None = None,
    conditions: dict | None = None,
    outputs: dict | None = None,
) -> tuple[str, list[str], dict]:
    template: dict = {"Resources": resources}
    for section, value in (
        ("Parameters", parameters),
        ("Conditions", conditions),
        ("Outputs", outputs),
    ):
        if value is not None:
            template[section] = value
    result = convert_template(json.dumps(template), stack_name="demo")
    files = {f["path"]: f["content"] for f in result["files"]}
    return files["main.tf"], result["notes"], files


@pytest.mark.parametrize("name", ["iam", "messaging", "observability", "data"])
def test_fixtures_convert_every_property(name):
    result = convert_template((FIXTURES / f"{name}.yaml").read_text(), stack_name="demo")
    assert not [n for n in result["notes"] if "not converted" in n]
    assert result["summary"]["awscc"] == 0 and result["summary"]["unsupported"] == 0


def test_role_with_inline_and_managed_policies():
    main, notes, files = convert(
        {
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "AssumeRolePolicyDocument": TRUST,
                    "MaxSessionDuration": "3600",
                    "PermissionsBoundary": "arn:aws:iam::aws:policy/PowerUserAccess",
                    "Policies": [
                        {"PolicyName": "a", "PolicyDocument": DOC},
                        {"PolicyName": "a", "PolicyDocument": DOC},
                        {"PolicyName": {"Fn::Sub": "${AWS::StackName}-p"}, "PolicyDocument": DOC},
                        {"PolicyName": {"Ref": "AWS::StackName"}, "PolicyDocument": DOC},
                        {"PolicyDocument": DOC},
                    ],
                    "ManagedPolicyArns": [
                        "arn:aws:iam::aws:policy/ReadOnlyAccess",
                        "arn:aws:iam::aws:policy/ReadOnlyAccess",
                        {"Fn::Sub": ["arn:${P}:iam::aws:policy/X", {"P": "aws"}]},
                        {"Fn::ImportValue": "shared-policy"},
                    ],
                },
            }
        },
        outputs={"Id": {"Value": {"Fn::GetAtt": ["Role", "RoleId"]}}},
    )
    assert 'resource "aws_iam_role" "role"' in main
    assert "max_session_duration = 3600" in main
    assert 'permissions_boundary = "arn:aws:iam::aws:policy/PowerUserAccess"' in main
    assert 'resource "aws_iam_role_policy" "role_a"' in main
    assert 'resource "aws_iam_role_policy" "role_a_2"' in main
    assert 'resource "aws_iam_role_policy" "role_aws_stack_name_p"' in main
    assert 'resource "aws_iam_role_policy" "role_policy_4"' in main
    assert 'resource "aws_iam_role_policy" "role_policy_5"' in main
    assert "role = aws_iam_role.role.name" in main
    assert 'resource "aws_iam_role_policy_attachment" "role_read_only_access"' in main
    assert 'resource "aws_iam_role_policy_attachment" "role_read_only_access_2"' in main
    assert 'resource "aws_iam_role_policy_attachment" "role_x"' in main
    assert 'resource "aws_iam_role_policy_attachment" "role_policy_4"' in main
    assert "inline_policy" not in main and "managed_policy_arns" not in main
    assert "aws_iam_role.role.unique_id" in files["outputs.tf"]
    assert not [n for n in notes if "not converted" in n]


def test_role_policies_computed_by_functions_are_reported():
    main, notes, _ = convert(
        {
            "Role": {
                "Type": "AWS::IAM::Role",
                "Condition": "On",
                "Properties": {
                    "AssumeRolePolicyDocument": TRUST,
                    "Policies": [{"Fn::If": ["On", {"PolicyName": "x"}, {"PolicyName": "y"}]}],
                    "ManagedPolicyArns": {"Fn::If": ["On", ["arn:a"], []]},
                },
            }
        },
        conditions={"On": {"Fn::Equals": ["a", "a"]}},
    )
    assert "Policies[0] was not converted" in main
    assert "ManagedPolicyArns was not converted" in main
    assert any("inline policy 1 is computed by a function" in n for n in notes)
    assert any("ManagedPolicyArns is computed by a function" in n for n in notes)
    assert "count = local.on ? 1 : 0" in main


def test_role_policies_as_a_single_mapping():
    main, _, _ = convert(
        {
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "AssumeRolePolicyDocument": TRUST,
                    "Policies": {"PolicyName": "only", "PolicyDocument": DOC},
                },
            }
        }
    )
    assert 'resource "aws_iam_role_policy" "role_only"' in main


def test_policy_attached_to_roles_users_and_groups():
    main, notes, _ = convert(
        {
            "Role": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": TRUST}},
            "Policy": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyName": "p",
                    "PolicyDocument": DOC,
                    "Roles": [{"Ref": "Role"}, "literal-role"],
                    "Users": ["alice"],
                    "Groups": [{"Fn::Sub": "${AWS::StackName}-admins"}, {"Fn::ImportValue": "g"}],
                },
            },
        }
    )
    assert 'resource "aws_iam_role_policy" "policy"' in main
    assert "role = aws_iam_role.role.name" in main
    assert 'resource "aws_iam_role_policy" "policy_literal_role"' in main
    assert 'resource "aws_iam_user_policy" "policy_alice"' in main
    assert 'user = "alice"' in main
    assert 'resource "aws_iam_group_policy" "policy_aws_stack_name_admins"' in main
    assert 'resource "aws_iam_group_policy" "policy_group_5"' in main
    assert not [n for n in notes if "not converted" in n]


def test_policy_type_follows_the_first_principal():
    main, _, _ = convert(
        {
            "Policy": {
                "Type": "AWS::IAM::Policy",
                "Properties": {"PolicyDocument": DOC, "Users": ["bob"]},
            }
        },
        outputs={"P": {"Value": {"Ref": "Policy"}}},
    )
    assert 'resource "aws_iam_user_policy" "policy"' in main
    assert "name =" not in main


def test_policy_without_readable_principals_gets_a_variable():
    main, notes, files = convert(
        {
            "Policy": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyName": "p",
                    "PolicyDocument": DOC,
                    "Roles": {"Fn::Split": [",", "a,b"]},
                },
            }
        }
    )
    assert 'resource "aws_iam_role_policy" "policy"' in main
    assert "role = var.policy_role" in main
    assert "Roles was not converted" in main
    assert 'variable "policy_role"' in files["variables.tf"]
    assert any("lists no role, user or group" in n for n in notes)


def test_managed_policy_and_attachments():
    main, _, files = convert(
        {
            "Managed": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "PolicyDocument": DOC,
                    "Roles": [{"Ref": "AWS::StackName"}],
                    "Users": [{"Fn::Join": ["", ["u"]]}],
                    "Groups": ["ops"],
                },
            }
        },
        outputs={
            "Arn": {"Value": {"Ref": "Managed"}},
            "Id": {"Value": {"Fn::GetAtt": ["Managed", "PolicyId"]}},
        },
    )
    assert 'resource "aws_iam_policy" "managed"' in main
    assert 'resource "aws_iam_role_policy_attachment" "managed_aws_stack_name"' in main
    assert 'resource "aws_iam_user_policy_attachment" "managed_u"' in main
    assert 'resource "aws_iam_group_policy_attachment" "managed_ops"' in main
    assert "policy_arn = aws_iam_policy.managed.arn" in main
    assert "aws_iam_policy.managed.policy_id" in files["outputs.tf"]


def test_instance_profile_takes_its_one_role():
    main, notes, files = convert(
        {
            "Profile": {"Type": "AWS::IAM::InstanceProfile", "Properties": {"Roles": ["r"]}},
            "Two": {"Type": "AWS::IAM::InstanceProfile", "Properties": {"Roles": ["a", "b"]}},
        },
        outputs={"Arn": {"Value": {"Fn::GetAtt": ["Profile", "Arn"]}}},
    )
    assert 'role = "r"' in main
    assert any("Two.Roles' was not converted" in n for n in notes)
    assert "aws_iam_instance_profile.profile.arn" in files["outputs.tf"]


def test_user_and_group():
    main, notes, _ = convert(
        {
            "User": {
                "Type": "AWS::IAM::User",
                "Properties": {
                    "Groups": ["ops"],
                    "PermissionsBoundary": "arn:aws:iam::aws:policy/PowerUserAccess",
                    "Policies": [{"PolicyName": "p", "PolicyDocument": DOC}],
                    "ManagedPolicyArns": ["arn:aws:iam::aws:policy/ReadOnlyAccess"],
                    "LoginProfile": {"Password": "x"},
                },
            },
            "Group": {
                "Type": "AWS::IAM::Group",
                "Properties": {
                    "Policies": [{"PolicyName": "p", "PolicyDocument": DOC}],
                    "ManagedPolicyArns": ["arn:aws:iam::aws:policy/ReadOnlyAccess"],
                },
            },
        }
    )
    assert 'name                 = "${var.stack_name}-User"' in main
    assert 'resource "aws_iam_user_group_membership" "user_groups"' in main
    assert 'resource "aws_iam_user_policy" "user_p"' in main
    assert 'resource "aws_iam_user_policy_attachment" "user_read_only_access"' in main
    assert 'name = "${var.stack_name}-Group"' in main
    assert 'resource "aws_iam_group_policy" "group_p"' in main
    assert 'resource "aws_iam_group_policy_attachment" "group_read_only_access"' in main
    assert any("User.LoginProfile' was not converted" in n for n in notes)
    assert any("Group had no GroupName" in n for n in notes)


def squash(text: str) -> str:
    """HCL without the alignment spaces before '='."""
    return re.sub(r" +=", " =", text)


ON = {"On": {"Fn::Equals": [{"Ref": "Env"}, "prod"]}}
ENV = {"Env": {"Type": "String"}}


def _if(value: object) -> dict:
    return {"Fn::If": ["On", value, {"Ref": "AWS::NoValue"}]}


def _unless(value: object) -> dict:
    return {"Fn::If": ["On", {"Ref": "AWS::NoValue"}, value]}


def test_optional_managed_policies_and_inline_policies_are_counted():
    main, notes, _ = convert(
        {
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "AssumeRolePolicyDocument": TRUST,
                    "ManagedPolicyArns": [
                        "arn:aws:iam::aws:policy/ReadOnlyAccess",
                        _if("arn:aws:iam::aws:policy/AdministratorAccess"),
                        {"Ref": "AWS::NoValue"},
                    ],
                    "Policies": [
                        _unless({"PolicyName": "dev", "PolicyDocument": DOC}),
                        {"Ref": "AWS::NoValue"},
                    ],
                },
            }
        },
        parameters=ENV,
        conditions=ON,
    )
    main = squash(main)
    admin = main.split('"aws_iam_role_policy_attachment" "role_administrator_access"')[1]
    assert admin.startswith(" {\n  count = local.on ? 1 : 0")
    assert 'policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"' in admin
    assert "null" not in main
    dev = main.split('"aws_iam_role_policy" "role_dev"')[1]
    assert dev.startswith(" {\n  count = !local.on ? 1 : 0")
    assert main.count("aws_iam_role_policy_attachment") == 2
    assert not any("not converted" in n or "computed by a function" in n for n in notes)


def test_policy_with_optional_principals():
    main, _, files = convert(
        {
            "Role": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": TRUST}},
            "User": {"Type": "AWS::IAM::User"},
            "Policy": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyName": "p",
                    "PolicyDocument": DOC,
                    "Roles": [_if({"Ref": "Role"}), {"Ref": "AWS::NoValue"}],
                    "Users": [{"Ref": "User"}],
                },
            },
            "OnlyOptional": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyName": "q",
                    "PolicyDocument": DOC,
                    "Roles": [_if({"Ref": "Role"})],
                },
            },
            "Managed": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {"PolicyDocument": DOC, "Groups": [_if("admins")]},
            },
        },
        parameters=ENV,
        conditions=ON,
        outputs={"Ref": {"Value": {"Ref": "OnlyOptional"}}},
    )
    main = squash(main)
    # The principal that is always there gets the main resource.
    policy = main.split('resource "aws_iam_user_policy" "policy" {')[1].split("\n}")[0]
    assert "count" not in policy and "user = aws_iam_user.user.name" in policy
    role = main.split('resource "aws_iam_role_policy" "policy_role" {')[1].split("\n}")[0]
    assert "count = local.on ? 1 : 0" in role and "role = aws_iam_role.role.name" in role
    only = main.split('resource "aws_iam_role_policy" "only_optional" {')[1].split("\n}")[0]
    assert "count = local.on ? 1 : 0" in only
    assert "aws_iam_role_policy.only_optional[0].id" in files["outputs.tf"]
    group = main.split('"aws_iam_group_policy_attachment" "managed_admins" {')[1]
    assert group.startswith("\n  count = local.on ? 1 : 0") and 'group = "admins"' in group
    assert "null" not in main
