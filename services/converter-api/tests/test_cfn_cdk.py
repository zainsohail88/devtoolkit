"""Templates synthesized by the AWS CDK (converters/cfn/cdk.py)."""

from __future__ import annotations

import json
from pathlib import Path

from converters.cfn.cdk import drop_tag, path_name, prepare_cdk, references
from converters.cfn.pipeline import convert_template

FIXTURE = Path(__file__).parent / "fixtures" / "cfn" / "cdk.json"


def meta(path: str, extra: dict | None = None) -> dict:
    out = {"aws:cdk:path": path}
    out.update(extra or {})
    return out


def test_fixture():
    result = convert_template(FIXTURE.read_text(), stack_name="web")
    assert result["source"] == "cdk"
    assert result["summary"] == {"resources": 6, "aws": 6, "awscc": 0, "unsupported": 0}
    files = {f["path"]: f["content"] for f in result["files"]}
    main = files["main.tf"]
    assert 'resource "aws_s3_bucket" "site_bucket" {\n  force_destroy = true\n}' in main
    assert "aws-cdk:auto-delete-objects" not in main
    assert "custom_s3_auto_delete" not in main  # the helper function and role are gone
    assert 'resource "aws_cloudwatch_log_group" "api_handler_log_retention"' in main
    assert "retention_in_days = 14" in main
    assert 'source_dir  = "${path.module}/cdk.out/asset.3b1f' in main
    assert 'resource "aws_iam_role_policy" "api_handler_service_role_default_policy"' in main
    assert main.count("Principal") == 2  # the provider role's bucket policy statement is gone
    assert "bootstrap" not in files.get("variables.tf", "").lower()
    assert "locals.tf" not in files  # CDKMetadataAvailable is unused once CDKMetadata is gone
    notes = "\n".join(result["notes"])
    assert "force_destroy" in notes and "log group" in notes and "cdk.out" in notes


def test_path_names():
    assert path_name("Stack/Api/Handler/Resource") == "Api_Handler"
    assert path_name("Stack/Table/Default") == "Table"
    assert path_name("Stack/Resource") is None
    assert path_name("Stack/Custom::Thing/Role") == "Custom_Thing_Role"


def testreferences():
    names = {"A"}
    assert references({"Ref": "A"}, names)
    assert references({"Fn::GetAtt": ["A", "Arn"]}, names)
    assert references({"Fn::GetAtt": "A.Arn"}, names)
    assert references({"Fn::Sub": "${A.Arn}/x"}, names)
    assert references({"Fn::Sub": ["${A}", {}]}, names)
    assert not references({"Fn::Sub": "${AB}"}, names)
    assert not references({"Fn::Sub": [1, {}]}, names)
    assert references([{"x": {"Ref": "A"}}], names)
    assert not references("A", names)


def test_restrict_default_security_group_and_shared_providers():
    template = {
        "Resources": {
            "Vpc": {
                "Type": "AWS::EC2::VPC",
                "Properties": {"CidrBlock": "10.0.0.0/16"},
                "Metadata": meta("S/Vpc/Resource"),
            },
            "Restrict": {
                "Type": "Custom::VpcRestrictDefaultSG",
                "Properties": {
                    "ServiceToken": {"Fn::GetAtt": "Provider.Arn"},
                    "DefaultSecurityGroupId": {"Fn::GetAtt": ["Vpc", "DefaultSecurityGroup"]},
                },
                "Metadata": meta("S/Vpc/RestrictDefaultSecurityGroupCustomResource/Default"),
            },
            "Provider": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Role": {"Fn::GetAtt": "ProviderRole.Arn"},
                    "Handler": "i.h",
                    "Runtime": "nodejs22.x",
                    "Code": {"ZipFile": "x"},
                },
                "Metadata": meta("S/Custom::VpcRestrictDefaultSGCustomResourceProvider/Handler"),
            },
            "ProviderRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {"AssumeRolePolicyDocument": {}},
                "Metadata": meta("S/Custom::VpcRestrictDefaultSGCustomResourceProvider/Role"),
            },
            "UsesRole": {
                "Type": "AWS::SNS::Topic",
                "Properties": {"TopicName": {"Fn::GetAtt": ["ProviderRole", "Arn"]}},
                "DependsOn": ["Restrict"],
            },
            "Unnamed": {"Type": "AWS::SQS::Queue", "Metadata": {"aws:cdk:path": "S/Resource"}},
            "NoMeta": {"Type": "AWS::SQS::Queue", "Metadata": "x"},
        }
    }
    notes: list[str] = []
    out, names, _ = prepare_cdk(template, notes)
    resources = out["Resources"]
    assert "Restrict" not in resources and "Provider" not in resources
    assert "ProviderRole" in resources  # still referenced by UsesRole
    assert "DependsOn" not in resources["UsesRole"]
    assert names == {
        "Vpc": "Vpc",
        "ProviderRole": "Custom_VpcRestrictDefaultSGCustomResourceProvider_Role",
    }
    assert any("aws_default_security_group" in n for n in notes)


def test_auto_delete_with_an_external_bucket_and_kept_conditions():
    template = {
        "Conditions": {
            "CDKMetadataAvailable": {"Fn::Equals": ["a", "a"]},
            "Other": {"Fn::Equals": ["a", "b"]},
        },
        "Resources": {
            "AutoDelete": {
                "Type": "Custom::S3AutoDeleteObjects",
                "Properties": {"ServiceToken": "arn:literal", "BucketName": "external-bucket"},
            },
            "Queue": {
                "Type": "AWS::SQS::Queue",
                "Condition": "CDKMetadataAvailable",
                "Properties": {"DelaySeconds": {"Fn::If": ["CDKMetadataAvailable", 1, 2]}},
            },
            "Tagged": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "Tags": [
                        {"Key": "aws-cdk:auto-delete-objects", "Value": "true"},
                        {"Key": "k", "Value": "v"},
                    ]
                },
            },
            "Policy": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {"Bucket": "b", "PolicyDocument": "{}"},
            },
            "Meta": {"Type": "AWS::CDK::Metadata", "Condition": "CDKMetadataAvailable"},
        },
        "Parameters": {"BootstrapVersion": {"Type": "String"}, "Keep": {"Type": "String"}},
        "Rules": {"CheckBootstrapVersion": {}, "Mine": {}},
    }
    notes: list[str] = []
    out, _, _ = prepare_cdk(template, notes)
    assert "CDKMetadataAvailable" in out["Conditions"]  # still used by Queue
    assert out["Parameters"] == {"Keep": {"Type": "String"}}
    assert out["Rules"] == {"Mine": {}}
    assert "Meta" not in out["Resources"] and "AutoDelete" not in out["Resources"]
    resource = out["Resources"]["Tagged"]
    drop_tag(resource, "aws-cdk:auto-delete-objects")
    assert resource["Properties"]["Tags"] == [{"Key": "k", "Value": "v"}]
    result = convert_template(json.dumps(template))
    assert any("Rules of the template were not converted" in n for n in result["notes"])


def test_unused_cdk_condition_is_removed_and_empty_policy_dropped():
    template = {
        "Conditions": {"CDKMetadataAvailable": {"Fn::Equals": ["a", "a"]}},
        "Resources": {
            "Bucket": {"Type": "AWS::S3::Bucket", "Metadata": meta("S/Bucket/Resource")},
            "AutoDelete": {
                "Type": "Custom::S3AutoDeleteObjects",
                "Properties": {
                    "ServiceToken": {"Fn::GetAtt": ["Provider", "Arn"]},
                    "BucketName": {"Ref": "Bucket"},
                },
            },
            "Provider": {"Type": "AWS::Lambda::Function", "Properties": {}},
            "OnlyProvider": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {
                    "Bucket": {"Ref": "Bucket"},
                    "PolicyDocument": {
                        "Statement": [{"Principal": {"AWS": {"Fn::GetAtt": ["Provider", "Arn"]}}}]
                    },
                },
                "DependsOn": "Provider",
            },
            "Layer": {
                "Type": "AWS::Lambda::LayerVersion",
                "Properties": {"Content": {"S3Bucket": "b", "S3Key": "k"}},
                "Metadata": meta(
                    "S/Layer/Resource",
                    {"aws:asset:path": "asset.1", "aws:asset:property": "Content"},
                ),
            },
            "Image": {
                "Type": "AWS::Lambda::Function",
                "Properties": {"Code": {"ImageUri": "x"}},
                "Metadata": meta(
                    "S/Image/Resource",
                    {"aws:asset:path": "asset.2", "aws:asset:property": "Code.ImageUri"},
                ),
            },
            "Meta": {"Type": "AWS::CDK::Metadata", "Condition": "CDKMetadataAvailable"},
        },
    }
    notes: list[str] = []
    out, names, hints = prepare_cdk(template, notes)
    assert "Conditions" not in out
    assert "OnlyProvider" not in out["Resources"]
    assert hints == {"Bucket": {"force_destroy": True}, "Layer": {"code_path": "cdk.out/asset.1"}}
    assert "Content" not in out["Resources"]["Layer"]["Properties"]
    assert out["Resources"]["Image"]["Properties"]["Code"] == {"ImageUri": "x"}
    assert names["Layer"] == "Layer"


def test_condition_used_only_in_fn_if_and_nothing_to_remove():
    template = {
        "Conditions": {"CDKMetadataAvailable": {"Fn::Equals": ["a", "a"]}},
        "Resources": {
            "Queue": {
                "Type": "AWS::SQS::Queue",
                "Properties": {"DelaySeconds": {"Fn::If": ["CDKMetadataAvailable", 1, 2]}},
                "Metadata": meta("S/Queue/Resource"),
            }
        },
    }
    out, names, hints = prepare_cdk(template, [])
    assert "CDKMetadataAvailable" in out["Conditions"]
    assert names == {"Queue": "Queue"} and hints == {}
