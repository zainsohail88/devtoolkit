"""Regression tests for problems found in review: HCL injection, input limits, and
expressions that were valid HCL but meant something else."""

from __future__ import annotations

import json
import re

import pytest
import yaml

from converters.cfn import CfnError, convert_template
from converters.cfn.context import Context, json_value
from converters.cfn.naming import UniqueNames
from converters.cfn.parse import load_template

BUCKET = {"Bucket": {"Type": "AWS::S3::Bucket"}}
NO_VALUE = {"Ref": "AWS::NoValue"}
FALSE = {"C": {"Fn::Equals": ["a", "b"]}}


def files(template: dict) -> dict[str, str]:
    result = convert_template(yaml.safe_dump(template, sort_keys=False))
    return {f["path"]: re.sub(r" +", " ", f["content"]) for f in result["files"]}


def expression(value, template: dict | None = None) -> str:
    template = dict(template or {})
    template.setdefault("Resources", dict(BUCKET))
    template["Outputs"] = {"Out": {"Value": value}}
    out = files(template)["outputs.tf"]
    return out.split("value = ", 1)[1].rsplit("\n}", 1)[0]


# --- injection ---------------------------------------------------------------------------

INJECTION = '"\n}\nresource "terraform_data" "pwn" {\n'


@pytest.mark.parametrize(
    ("section", "name"),
    [
        ("Resources", "U" + INJECTION),
        ("Resources", "Bad-Name"),
        ("Parameters", "a.b"),
        ("Conditions", "x y"),
        ("Mappings", "${x}"),
        ("Outputs", "${x}"),
    ],
)
def test_names_must_be_alphanumeric(section, name):
    template: dict = {"Resources": dict(BUCKET)}
    entry = {
        "Resources": {"Type": "AWS::S3::Bucket"},
        "Parameters": {"Type": "String"},
        "Conditions": {"Fn::Equals": ["a", "a"]},
        "Mappings": {"k": {"v": 1}},
        "Outputs": {"Value": "x"},
    }[section]
    template.setdefault(section, {})[name] = entry
    with pytest.raises(CfnError, match="letters and digits only"):
        load_template(json.dumps(template))


def test_get_att_attribute_names_are_checked():
    with pytest.raises(CfnError, match="is not valid"):
        expression({"Fn::GetAtt": ["Bucket", 'Arn"} {"']})


def test_sam_definition_uri_is_escaped():
    template = {
        "Transform": "AWS::Serverless-2016-10-31",
        "Resources": {
            "Api": {
                "Type": "AWS::Serverless::Api",
                "Properties": {"StageName": "s", "DefinitionUri": 'a"${file("x")}.yaml'},
            }
        },
    }
    main = files(template)["main.tf"]
    assert 'file("${path.module}/a\\"$${file(\\"x\\")}.yaml")' in main
    assert "terraform_data" not in main


# --- limits ------------------------------------------------------------------------------


def test_non_finite_numbers_and_huge_cidr_counts_are_rejected():
    with pytest.raises(CfnError, match="Invalid number"):
        load_template("Resources: {B: {Type: X, Properties: {V: 1.0e400}}}")
    with pytest.raises(CfnError, match="Invalid number"):
        load_template('{"Resources": {"B": {"Type": "X", "Properties": {"V": 1e400}}}}')
    with pytest.raises(CfnError, match="at most 256"):
        expression({"Fn::Cidr": ["::/0", 1_000_000, 1]})


def test_many_colliding_names_stay_linear():
    names = UniqueNames()
    taken = [names.take("a") for _ in range(20_000)]
    assert taken[-1] == "a_20000" and len(set(taken)) == 20_000


def test_json_strings_too_deep_or_recursive():
    ctx = Context({"Resources": {}})
    assert json_value(ctx, "[" * 100_000) == "[" * 100_000
    with pytest.raises(CfnError, match="nested too deeply"):
        json_value(ctx, "[" * 150 + "]" * 150)


# --- template strings --------------------------------------------------------------------


def test_template_strings_escape_across_pieces():
    template = {"Parameters": {"N": {"Type": "String"}}}
    assert expression({"Fn::Sub": "$${N}"}, template) == '"${"$"}${var.n}"'
    assert expression({"Fn::Join": ["", ["$", "{path.cwd}", {"Ref": "N"}]]}, template) == (
        '"$${path.cwd}${var.n}"'
    )
    assert expression({"Fn::Join": ["", ["100%", {"Ref": "N"}]]}, template) == (
        '"100${"%"}${var.n}"'
    )
    assert expression({"Fn::Join": ["-", ["a", True, 1.5]]}) == '"a-true-1.5"'
    assert expression({"Fn::Join": ["-", ["a", NO_VALUE, "b"]]}) == '"a-b"'
    assert expression({"Fn::Join": ["-", []]}) == '""'


def test_join_with_optional_items_filters_them():
    value = {"Fn::Join": ["-", ["jobs", {"Fn::If": ["C", "prod", NO_VALUE]}]]}
    assert expression(value, {"Conditions": FALSE}) == (
        'join("-", [for item in ["jobs", local.c ? "prod" : null] : item if item != null])'
    )


# --- conditions and NoValue --------------------------------------------------------------


def test_equals_compares_as_text():
    template = {
        "Parameters": {"N": {"Type": "Number"}, "Flag": {"Type": "String"}},
        "Conditions": {
            "IsTwo": {"Fn::Equals": [{"Ref": "N"}, 2]},
            "On": {"Fn::Equals": [{"Ref": "Flag"}, True]},
        },
        "Resources": BUCKET,
    }
    locals_tf = files(template)["locals.tf"]
    assert 'is_two = tostring(var.n) == "2"' in locals_tf
    assert 'on = var.flag == "true"' in locals_tf


def test_mappings_drop_optional_keys():
    value = {"A": "1", "B": {"Fn::If": ["C", "2", NO_VALUE]}, "N": NO_VALUE}
    assert expression(value, {"Conditions": FALSE}) == (
        'merge({ A = "1" }, local.c ? { B = "2" } : {})'
    )


def test_if_inside_json_documents():
    template = {
        "Conditions": FALSE,
        "Resources": {
            "P": {
                "Type": "AWS::SQS::QueuePolicy",
                "Properties": {
                    "Queues": ["https://q"],
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Action": {"Fn::If": ["C", "sqs:A", ["sqs:A", "sqs:B"]]},
                                "Resource": "*",
                            }
                        ]
                    },
                },
            },
            "Q": {
                "Type": "AWS::SQS::Queue",
                "Properties": {
                    "RedrivePolicy": {
                        "Fn::If": [
                            "C",
                            {"deadLetterTargetArn": "arn", "maxReceiveCount": 3},
                            NO_VALUE,
                        ]
                    }
                },
            },
        },
    }
    main = files(template)["main.tf"]
    assert (
        'Action = jsondecode(local.c ? jsonencode("sqs:A") : jsonencode(["sqs:A", "sqs:B"]))'
        in main
    )
    assert (
        'redrive_policy = local.c ? jsonencode({ deadLetterTargetArn = "arn", '
        "maxReceiveCount = 3 }) : null" in main
    )


def test_awscc_lists_drop_optional_items():
    template = {
        "Conditions": FALSE,
        "Resources": {
            "Q": {
                "Type": "AWS::Batch::JobQueue",
                "Properties": {
                    "Priority": 1,
                    "ComputeEnvironmentOrder": [
                        {"Order": 1, "ComputeEnvironment": "a"},
                        {"Fn::If": ["C", {"Order": 2, "ComputeEnvironment": "b"}, NO_VALUE]},
                        NO_VALUE,
                    ],
                },
            }
        },
    }
    main = files(template)["main.tf"]
    assert "compute_environment_order = [for item in [" in main
    assert 'local.c ? { order = 2, compute_environment = "b" } : null' in main


# --- names, sensitivity, SAM ---------------------------------------------------------------


def test_extra_resources_never_collide_with_converted_ones():
    template = {
        "Resources": {
            "Alerts": {
                "Type": "AWS::SNS::Topic",
                "Properties": {"Subscription": [{"Protocol": "email", "Endpoint": "a@b.c"}]},
            },
            "AlertsSubscription": {
                "Type": "AWS::SNS::Subscription",
                "Properties": {
                    "Protocol": "email",
                    "Endpoint": "x@y.z",
                    "TopicArn": {"Ref": "Alerts"},
                },
            },
        }
    }
    main = files(template)["main.tf"]
    names = re.findall(r'resource "aws_sns_topic_subscription" "(\w+)"', main)
    assert len(names) == 2 and len(set(names)) == 2


def test_outputs_using_a_condition_built_from_a_secret_are_sensitive():
    template = {
        "Parameters": {"Pw": {"Type": "String", "NoEcho": "true"}},
        "Conditions": {
            "HasPw": {"Fn::Not": [{"Fn::Equals": [{"Ref": "Pw"}, ""]}]},
            "AlsoPw": {"Fn::And": [{"Condition": "HasPw"}, {"Fn::Equals": ["a", "a"]}]},
        },
        "Resources": BUCKET,
        "Outputs": {
            "Arn": {"Condition": "AlsoPw", "Value": {"Fn::GetAtt": ["Bucket", "Arn"]}},
            "Plain": {"Value": {"Ref": "Bucket"}},
        },
    }
    outputs = files(template)["outputs.tf"]
    arn, plain = outputs.split('output "plain"')
    assert "sensitive = true" in arn and "sensitive" not in plain


def test_sam_vpc_policy_api_globals_and_stage_references_in_sub():
    template = {
        "Transform": "AWS::Serverless-2016-10-31",
        "Globals": {
            "Api": {"EndpointConfiguration": "REGIONAL", "BinaryMediaTypes": ["image/png"]}
        },
        "Resources": {
            "Fn": {
                "Type": "AWS::Serverless::Function",
                "Properties": {
                    "Handler": "a.b",
                    "Runtime": "python3.13",
                    "InlineCode": "x",
                    "VpcConfig": {"SubnetIds": ["s"], "SecurityGroupIds": ["g"]},
                    "Events": {
                        "Get": {"Type": "Api", "Properties": {"Path": "/", "Method": "get"}}
                    },
                },
            },
            "MyApi": {"Type": "AWS::Serverless::Api", "Properties": {"StageName": "v1"}},
        },
        "Outputs": {
            "Url": {"Value": {"Fn::Sub": "https://${MyApi}.execute-api/${MyApi.Stage}/"}},
            "Other": {"Value": {"Fn::Sub": ["${MyApi.Stage}-${X}", {"X": "y"}]}},
        },
    }
    out = files(template)
    main = out["main.tf"]
    assert "AWSLambdaVPCAccessExecutionRole" in main
    rest = main.split('resource "aws_api_gateway_rest_api" "serverless_rest_api"')[1]
    assert 'types = ["REGIONAL"]' in rest.split("\n}\n")[0]
    assert '"image/png"' in rest.split("\n}\n")[0]
    outputs = out["outputs.tf"]
    assert "aws_api_gateway_stage.my_apiv1_stage.stage_name" in outputs
    assert ".stage}" not in outputs
