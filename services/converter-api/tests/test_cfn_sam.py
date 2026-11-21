"""AWS SAM expansion (converters/cfn/sam.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from converters.cfn.errors import CfnError
from converters.cfn.models import Expression
from converters.cfn.pipeline import convert_template
from converters.cfn.sam import expand_sam
from converters.cfn.sam_policies import PolicyTemplates

FIXTURE = Path(__file__).parent / "fixtures" / "cfn" / "sam.yaml"
TRANSFORM = "AWS::Serverless-2016-10-31"


def expand(resources: dict, extra: dict | None = None) -> tuple[dict, dict, list[str]]:
    notes: list[str] = []
    template = {"Transform": TRANSFORM, "Resources": resources}
    template.update(extra or {})
    out, hints = expand_sam(template, notes)
    return out, hints, notes


def function(props: dict | None = None) -> dict:
    properties = {"Handler": "a.b"}
    properties.update(props or {})
    return {"Type": "AWS::Serverless::Function", "Properties": properties}


def with_attributes(resource: dict, attributes: dict) -> dict:
    out = dict(resource)
    out.update(attributes)
    return out


def test_fixture_converts_to_aws_resources_only():
    result = convert_template(FIXTURE.read_text(), stack_name="demo")
    assert result["source"] == "sam"
    assert result["summary"]["awscc"] == 0 and result["summary"]["unsupported"] == 0
    assert not [n for n in result["notes"] if "was left out" in n]


def test_function_role_and_globals():
    out, hints, notes = expand(
        {
            "Fn": function(
                {
                    "CodeUri": "./src/",
                    "Environment": {"Variables": {"B": "2"}},
                    "Layers": ["own"],
                    "Tags": {"team": "a"},
                    "Tracing": "Active",
                    "Unknown": 1,
                },
            )
        },
        {
            "Globals": {
                "Function": {
                    "Runtime": "python3.13",
                    "Environment": {"Variables": {"A": "1"}},
                    "Layers": ["global"],
                    "Tags": {"Ref": "AWS::NoValue"},
                }
            }
        },
    )
    fn = out["Resources"]["Fn"]
    assert fn["Type"] == "AWS::Lambda::Function"
    props = fn["Properties"]
    assert props["Runtime"] == "python3.13"
    assert props["Environment"] == {"Variables": {"A": "1", "B": "2"}}
    assert props["Layers"] == ["global", "own"]
    assert props["Tags"] == [{"Key": "team", "Value": "a"}]
    assert props["TracingConfig"] == {"Mode": "Active"}
    assert props["Role"] == {"Fn::GetAtt": ["FnRole", "Arn"]}
    assert hints == {"Fn": {"code_path": "src"}}
    role = out["Resources"]["FnRole"]["Properties"]
    assert len(role["ManagedPolicyArns"]) == 2  # basic execution + X-Ray
    assert "Globals" not in out and "Transform" not in out
    assert any("SAM property 'Unknown' yet" in n for n in notes)


@pytest.mark.parametrize(
    ("props", "code", "hint"),
    [
        ({"ImageUri": "repo:tag"}, {"ImageUri": "repo:tag"}, None),
        ({"InlineCode": "x"}, {"ZipFile": "x"}, None),
        (
            {"CodeUri": {"Bucket": "b", "Key": "k", "Version": "v"}},
            {"S3Bucket": "b", "S3Key": "k", "S3ObjectVersion": "v"},
            None,
        ),
        ({"CodeUri": "s3://b/path/k.zip"}, {"S3Bucket": "b", "S3Key": "path/k.zip"}, None),
        ({"CodeUri": "."}, None, "."),
        ({"CodeUri": "./"}, None, "."),
    ],
)
def test_code_locations(props, code, hint):
    out, hints, _ = expand({"Fn": function(props)})
    assert out["Resources"]["Fn"]["Properties"].get("Code") == code
    assert hints.get("Fn", {}).get("code_path") == hint


def test_computed_code_uri_is_noted():
    _, _, notes = expand({"Fn": function({"CodeUri": {"Ref": "Path"}})})
    assert any("CodeUri comes from a function" in n for n in notes)


def test_dead_letter_queue_and_role_options():
    out, _, _ = expand(
        {
            "Sqs": function({"DeadLetterQueue": {"Type": "SQS", "TargetArn": "arn:q"}}),
            "Sns": function(
                {
                    "DeadLetterQueue": {"Type": "SNS", "TargetArn": "arn:t"},
                    "PermissionsBoundary": "arn:boundary",
                    "RolePath": "/apps/",
                    "AssumeRolePolicyDocument": {"Statement": []},
                },
            ),
        }
    )
    sqs_role = out["Resources"]["SqsRole"]["Properties"]
    assert sqs_role["Policies"][0]["PolicyDocument"]["Statement"][0]["Action"] == [
        "sqs:SendMessage"
    ]
    sns_role = out["Resources"]["SnsRole"]["Properties"]
    assert sns_role["Policies"][0]["PolicyDocument"]["Statement"][0]["Action"] == ["sns:Publish"]
    assert sns_role["PermissionsBoundary"] == "arn:boundary"
    assert sns_role["Path"] == "/apps/"
    assert sns_role["AssumeRolePolicyDocument"] == {"Statement": []}


def test_explicit_role_skips_the_generated_role():
    out, _, notes = expand(
        {"Fn": function({"Role": "arn:role", "Policies": ["AmazonS3ReadOnlyAccess"]})}
    )
    assert "FnRole" not in out["Resources"]
    assert out["Resources"]["Fn"]["Properties"]["Role"] == "arn:role"
    assert any("sets both Role and Policies" in n for n in notes)


def test_policies():
    out, _, notes = expand(
        {
            "Fn": function(
                {
                    "Policies": [
                        "AmazonS3ReadOnlyAccess",
                        "arn:aws:iam::aws:policy/Custom",
                        {"Ref": "PolicyArn"},
                        {"Statement": {"Effect": "Allow", "Action": "s3:*", "Resource": "*"}},
                        {"DynamoDBCrudPolicy": {"TableName": "t"}},
                        {"NoSuchTemplate": {}},
                        {"CloudWatchPutMetricPolicy": None},
                        42,
                    ]
                }
            )
        }
    )
    role = out["Resources"]["FnRole"]["Properties"]
    managed = role["ManagedPolicyArns"]
    assert {"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/AmazonS3ReadOnlyAccess"} in managed
    assert "arn:aws:iam::aws:policy/Custom" in managed
    assert {"Ref": "PolicyArn"} in managed
    names = [p["PolicyName"] for p in role["Policies"]]
    assert names == ["FnRolePolicy3", "FnRolePolicyTemplates"]
    statements = role["Policies"][1]["PolicyDocument"]["Statement"]
    assert statements[0]["Action"][0] == "dynamodb:GetItem"
    assert statements[1]["Action"] == ["cloudwatch:PutMetricData"]
    assert any("the SAM policy template 'NoSuchTemplate' is unknown" in n for n in notes)
    assert any("a Policies entry has a form the converter cannot read" in n for n in notes)


@pytest.mark.parametrize("name", PolicyTemplates().names())
def test_every_policy_template_converts(name):
    params = {
        "TableName": {"Ref": "T"},
        "StreamName": "s",
        "BucketName": "b",
        "QueueName": "q",
        "TopicName": "t",
        "FunctionName": "f",
        "StateMachineName": "m",
        "EventBusName": "e",
        "ParameterName": "p",
        "SecretArn": "arn:secret",
        "KeyId": "k",
        "LogGroupName": "l",
    }
    resources = {
        "T": {"Type": "AWS::DynamoDB::Table", "Properties": {"TableName": "t"}},
        "Fn": function({"InlineCode": "x", "Runtime": "python3.13", "Policies": [{name: params}]}),
    }
    result = convert_template(
        yaml.safe_dump({"Transform": TRANSFORM, "Resources": resources}), stack_name="demo"
    )
    main = next(f["content"] for f in result["files"] if f["path"] == "main.tf")
    assert "_policy_templates" in main


def test_polling_and_rule_events():
    out, _, _ = expand(
        {
            "Fn": function(
                {
                    "Events": {
                        "Queue": {"Type": "SQS", "Properties": {"Queue": "arn:q", "BatchSize": 5}},
                        "Stream": {
                            "Type": "Kinesis",
                            "Properties": {"Stream": "arn:k", "StartingPosition": "LATEST"},
                        },
                        "Every": {
                            "Type": "Schedule",
                            "Properties": {
                                "Schedule": "rate(1 hour)",
                                "Enabled": False,
                                "Name": "n",
                                "Input": "{}",
                            },
                        },
                        "Bus": {
                            "Type": "EventBridgeRule",
                            "Properties": {
                                "Pattern": {"source": ["x"]},
                                "EventBusName": "b",
                                "State": "ENABLED",
                                "RuleName": "r",
                                "Description": "d",
                                "InputPath": "$.x",
                            },
                        },
                        "Later": {
                            "Type": "ScheduleV2",
                            "Properties": {
                                "ScheduleExpression": "rate(1 day)",
                                "Name": "s",
                                "Input": "{}",
                            },
                        },
                    }
                }
            )
        }
    )
    resources = out["Resources"]
    assert resources["FnQueue"]["Properties"] == {
        "EventSourceArn": "arn:q",
        "FunctionName": {"Fn::GetAtt": ["Fn", "Arn"]},
        "BatchSize": 5,
    }
    every = resources["FnEvery"]["Properties"]
    assert (
        every["State"] == "DISABLED"
        and every["Name"] == "n"
        and every["Targets"][0]["Input"] == "{}"
    )
    bus = resources["FnBus"]["Properties"]
    assert (
        bus["EventBusName"] == "b"
        and bus["Name"] == "r"
        and bus["Targets"][0]["InputPath"] == "$.x"
    )
    assert resources["FnEveryPermission"]["Properties"]["Principal"] == "events.amazonaws.com"
    later = resources["FnLater"]
    assert later["Type"] == "AWS::Scheduler::Schedule"
    assert later["Properties"]["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert later["Properties"]["Target"]["Input"] == "{}"
    assert resources["FnLaterRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0][
        "Principal"
    ] == {"Service": ["scheduler.amazonaws.com"]}
    managed = resources["FnRole"]["Properties"]["ManagedPolicyArns"]
    assert len(managed) == 3  # basic, SQS, Kinesis


def test_sns_s3_and_logs_events():
    out, _, notes = expand(
        {
            "Bucket": {"Type": "AWS::S3::Bucket", "DependsOn": "Other"},
            "Other": {"Type": "AWS::SNS::Topic"},
            "Fn": function(
                {
                    "Events": {
                        "Topic": {
                            "Type": "SNS",
                            "Properties": {
                                "Topic": "arn:t",
                                "FilterPolicy": {"a": ["b"]},
                                "SqsSubscription": True,
                            },
                        },
                        "Upload": {
                            "Type": "S3",
                            "Properties": {
                                "Bucket": {"Ref": "Bucket"},
                                "Events": ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"],
                                "Filter": {"S3Key": {}},
                            },
                        },
                        "Elsewhere": {
                            "Type": "S3",
                            "Properties": {"Bucket": "external", "Events": "s3:ObjectCreated:*"},
                        },
                        "Logs": {"Type": "CloudWatchLogs", "Properties": {"LogGroupName": "/x"}},
                    }
                }
            ),
        }
    )
    resources = out["Resources"]
    assert resources["FnTopic"]["Properties"]["FilterPolicy"] == {"a": ["b"]}
    bucket = resources["Bucket"]
    configs = bucket["Properties"]["NotificationConfiguration"]["LambdaConfigurations"]
    assert [c["Event"] for c in configs] == ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"]
    assert bucket["DependsOn"] == ["Other", "FnUploadPermission"]
    assert resources["FnUploadPermission"]["Properties"]["SourceAccount"] == {
        "Ref": "AWS::AccountId"
    }
    assert resources["FnLogs"]["DependsOn"] == ["FnLogsPermission"]
    assert any("SqsSubscription" in n for n in notes)
    assert any(
        "FnElsewhere: the bucket of the S3 event lives outside this template" in n for n in notes
    )


def test_api_events_build_the_rest_api_body():
    out, _, notes = expand(
        {
            "Fn": function(
                {
                    "Events": {
                        "Get": {
                            "Type": "Api",
                            "Properties": {"Path": "/items/{id}", "Method": "GET"},
                        },
                        "Any": {
                            "Type": "Api",
                            "Properties": {
                                "Path": "/all",
                                "Method": "any",
                                "Auth": {"Authorizer": "X"},
                            },
                        },
                        "Mine": {
                            "Type": "Api",
                            "Properties": {
                                "Path": "/mine",
                                "Method": "post",
                                "RestApiId": {"Ref": "MyApi"},
                            },
                        },
                        "Given": {
                            "Type": "Api",
                            "Properties": {
                                "Path": "/g",
                                "Method": "get",
                                "RestApiId": {"Ref": "Defined"},
                            },
                        },
                    }
                }
            ),
            "MyApi": {
                "Type": "AWS::Serverless::Api",
                "Properties": {
                    "StageName": "v1",
                    "Name": "mine",
                    "EndpointConfiguration": "REGIONAL",
                    "Tags": {"a": "b"},
                    "MethodSettings": [{"HttpMethod": "*"}],
                    "Cors": "'*'",
                    "Description": "d",
                    "Odd": 1,
                },
            },
            "Defined": {
                "Type": "AWS::Serverless::Api",
                "Properties": {
                    "StageName": "prod",
                    "DefinitionBody": {"swagger": "2.0"},
                    "EndpointConfiguration": {"Types": ["EDGE"]},
                },
            },
            "FromFile": {
                "Type": "AWS::Serverless::Api",
                "Properties": {"StageName": "s", "DefinitionUri": "./api.yaml"},
            },
            "FromS3": {
                "Type": "AWS::Serverless::Api",
                "Properties": {"StageName": "s", "DefinitionUri": "s3://b/api.yaml"},
            },
        },
        {
            "Outputs": {
                "Stage": {"Value": {"Ref": "MyApi.Stage"}},
                "Url": {"Value": {"Ref": "ServerlessRestApi"}},
            }
        },
    )
    resources = out["Resources"]
    implicit = resources["ServerlessRestApi"]["Properties"]
    assert implicit["Name"] == {"Ref": "AWS::StackName"}
    paths = implicit["Body"]["paths"]
    assert set(paths) == {"/items/{id}", "/all"}
    assert "get" in paths["/items/{id}"] and "x-amazon-apigateway-any-method" in paths["/all"]
    assert set(resources["MyApi"]["Properties"]["Body"]["paths"]) == {"/mine"}
    assert resources["MyApi"]["Properties"]["EndpointConfiguration"] == {"Types": ["REGIONAL"]}
    assert resources["MyApi"]["Properties"]["Tags"] == [{"Key": "a", "Value": "b"}]
    assert resources["MyApiv1Stage"]["Properties"]["MethodSettings"] == [{"HttpMethod": "*"}]
    assert resources["Defined"]["Properties"]["Body"] == {"swagger": "2.0"}
    assert resources["FromFile"]["Properties"]["Body"] == Expression(
        'yamldecode(file("${path.module}/api.yaml"))'
    )
    assert resources["FromS3"]["Properties"]["BodyS3Location"] == {"Bucket": "b", "Key": "api.yaml"}
    assert resources["ServerlessRestApiProdStage"]["Properties"]["StageName"] == "Prod"
    get_arn = resources["FnGetPermission"]["Properties"]["SourceArn"]["Fn::Sub"][0]
    assert get_arn.endswith("/*/GET/items/*")
    any_arn = resources["FnAnyPermission"]["Properties"]["SourceArn"]["Fn::Sub"][0]
    assert any_arn.endswith("/*/*/all")
    assert out["Outputs"]["Stage"]["Value"] == {"Ref": "MyApiv1Stage"}
    assert any("Auth setting of the API event was left out" in n for n in notes)
    assert any("MyApi: the SAM API setting 'Cors'" in n for n in notes)
    assert any("MyApi: the converter has no mapping for the SAM property 'Odd'" in n for n in notes)


def test_http_api_events():
    out, _, notes = expand(
        {
            "Fn": function(
                {
                    "Events": {
                        "Root": {"Type": "HttpApi"},
                        "Post": {
                            "Type": "HttpApi",
                            "Properties": {
                                "Path": "/p",
                                "Method": "POST",
                                "PayloadFormatVersion": "1.0",
                                "TimeoutInMillis": 5000,
                                "Auth": {"Authorizer": "Jwt"},
                            },
                        },
                        "Own": {
                            "Type": "HttpApi",
                            "Properties": {"Path": "/o", "Method": "get", "ApiId": {"Ref": "Http"}},
                        },
                    }
                }
            ),
            "Http": {
                "Type": "AWS::Serverless::HttpApi",
                "Properties": {
                    "StageName": "live",
                    "CorsConfiguration": {"AllowOrigins": ["*"]},
                    "Tags": {"a": "b"},
                    "Auth": {"Authorizers": {}},
                    "StageVariables": {"x": "y"},
                    "Description": "d",
                    "Odd": 1,
                },
            },
            "Cors": {
                "Type": "AWS::Serverless::HttpApi",
                "Properties": {"CorsConfiguration": True, "DefinitionUri": "api.yaml"},
            },
            "S3": {"Type": "AWS::Serverless::HttpApi", "Properties": {"DefinitionUri": "s3://b/k"}},
            "Body": {
                "Type": "AWS::Serverless::HttpApi",
                "Properties": {"DefinitionBody": {"openapi": "3.0.1"}},
            },
        }
    )
    resources = out["Resources"]
    implicit = resources["ServerlessHttpApi"]["Properties"]
    assert implicit["ProtocolType"] == "HTTP"
    paths = implicit["Body"]["paths"]
    assert (
        paths["$default"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"][
            "payloadFormatVersion"
        ]
        == "2.0"
    )
    post = paths["/p"]["post"]["x-amazon-apigateway-integration"]
    assert post["payloadFormatVersion"] == "1.0" and post["timeoutInMillis"] == 5000
    assert resources["FnRootPermission"]["Properties"]["SourceArn"]["Fn::Sub"][0].endswith("}/*")
    assert set(resources["Http"]["Properties"]["Body"]["paths"]) == {"/o"}
    assert resources["Http"]["Properties"]["CorsConfiguration"] == {"AllowOrigins": ["*"]}
    assert resources["HttpliveStage"]["Properties"]["StageVariables"] == {"x": "y"}
    assert resources["ServerlessHttpApiApiGatewayDefaultStage"]["Properties"]["AutoDeploy"] is True
    assert resources["Cors"]["Properties"]["Body"] == Expression(
        'yamldecode(file("${path.module}/api.yaml"))'
    )
    assert resources["S3"]["Properties"]["BodyS3Location"] == {"Bucket": "b", "Key": "k"}
    assert resources["Body"]["Properties"]["Body"] == {"openapi": "3.0.1"}
    assert any("CorsConfiguration is a single value" in n for n in notes)
    assert any("Http: the SAM HTTP API setting 'Auth'" in n for n in notes)
    assert any("Http: the converter has no mapping for the SAM property 'Odd'" in n for n in notes)
    assert any("FnPost: the Auth setting of the API event was left out" in n for n in notes)


def test_url_alias_and_unconverted_function_settings():
    out, _, notes = expand(
        {
            "Fn": function(
                {
                    "AutoPublishAlias": "live",
                    "FunctionUrlConfig": {
                        "AuthType": "NONE",
                        "Cors": {"AllowOrigins": ["*"]},
                        "InvokeMode": "BUFFERED",
                    },
                    "DeploymentPreference": {"Type": "Linear10PercentEvery1Minute"},
                    "Events": {
                        "Queue": {"Type": "SQS", "Properties": {"Queue": "arn:q"}},
                        "Odd": {"Type": "IoTRule"},
                    },
                },
            ),
            "Private": function({"FunctionUrlConfig": {}}),
        },
        {
            "Outputs": {
                "Alias": {"Value": {"Ref": "Fn.Alias"}},
                "Version": {"Value": {"Ref": "Fn.Version"}},
            }
        },
    )
    resources = out["Resources"]
    assert resources["FnAliaslive"]["Properties"]["FunctionVersion"] == {
        "Fn::GetAtt": ["FnVersion", "Version"]
    }
    assert resources["FnQueue"]["Properties"]["FunctionName"] == {"Ref": "FnAliaslive"}
    url = resources["FnUrl"]["Properties"]
    assert url["TargetFunctionArn"] == {"Ref": "FnAliaslive"} and url["InvokeMode"] == "BUFFERED"
    assert resources["FnUrlPublicPermissions"]["Properties"]["FunctionUrlAuthType"] == "NONE"
    assert resources["PrivateUrl"]["Properties"]["AuthType"] == "AWS_IAM"
    assert "PrivateUrlPublicPermissions" not in resources
    assert out["Outputs"] == {
        "Alias": {"Value": {"Ref": "FnAliaslive"}},
        "Version": {"Value": {"Ref": "FnVersion"}},
    }
    assert any("the converter leaves out the SAM setting DeploymentPreference" in n for n in notes)
    assert any("the SAM event type 'IoTRule' was left out" in n for n in notes)


@pytest.mark.parametrize(
    ("resources", "message"),
    [
        ({"Fn": function({"Events": ["x"]})}, "'Events' of 'Fn' must be a mapping"),
        ({"Fn": function({"Events": {"E": "x"}})}, "event 'E' of 'Fn' must be a mapping"),
    ],
)
def test_invalid_sam(resources, message):
    with pytest.raises(CfnError, match=message):
        expand(resources)
    with pytest.raises(CfnError, match="'Globals' section must be a mapping"):
        expand({"Fn": function()}, {"Globals": [1]})


def test_simple_table_layer_and_other_types():
    out, hints, notes = expand(
        {
            "Plain": {"Type": "AWS::Serverless::SimpleTable"},
            "Custom": {
                "Type": "AWS::Serverless::SimpleTable",
                "Properties": {
                    "PrimaryKey": {"Name": "n", "Type": "Number"},
                    "ProvisionedThroughput": {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1},
                    "TableName": "t",
                    "Tags": {"a": "b"},
                    "Odd": 1,
                },
            },
            "S3Layer": {
                "Type": "AWS::Serverless::LayerVersion",
                "Properties": {
                    "ContentUri": {"Bucket": "b", "Key": "k", "Version": "1"},
                    "LayerName": "l",
                },
            },
            "UriLayer": {
                "Type": "AWS::Serverless::LayerVersion",
                "Properties": {"ContentUri": "s3://b/k", "RetentionPolicy": "Retain", "Odd": 1},
            },
            "LocalLayer": {
                "Type": "AWS::Serverless::LayerVersion",
                "Properties": {"ContentUri": "layer/"},
            },
            "App": {"Type": "AWS::Serverless::Application"},
        }
    )
    resources = out["Resources"]
    assert resources["Plain"]["Properties"]["KeySchema"] == [
        {"AttributeName": "id", "KeyType": "HASH"}
    ]
    assert resources["Plain"]["Properties"]["BillingMode"] == "PAY_PER_REQUEST"
    custom = resources["Custom"]["Properties"]
    assert custom["AttributeDefinitions"] == [{"AttributeName": "n", "AttributeType": "N"}]
    assert "BillingMode" not in custom and custom["TableName"] == "t"
    assert resources["S3Layer"]["Properties"]["Content"] == {
        "S3Bucket": "b",
        "S3Key": "k",
        "S3ObjectVersion": "1",
    }
    assert resources["UriLayer"]["Properties"]["Content"] == {"S3Bucket": "b", "S3Key": "k"}
    assert resources["UriLayer"]["DeletionPolicy"] == "Retain"
    assert hints["LocalLayer"] == {"code_path": "layer"}
    assert "App" not in resources
    assert any("App (AWS::Serverless::Application) is a SAM type" in n for n in notes)
    assert any(
        "Custom: the converter has no mapping for the SAM property 'Odd'" in n for n in notes
    )
    assert any("UriLayer: the converter has no mapping for the SAM property" in n for n in notes)


def test_state_machines():
    out, _, notes = expand(
        {
            "Flow": {
                "Type": "AWS::Serverless::StateMachine",
                "Properties": {
                    "Definition": {
                        "States": {
                            "A": {
                                "Resource": "${Fn}",
                                "Static": "${Plain}",
                                "Other": "${Unused}",
                                "List": ["${Plain}"],
                            }
                        }
                    },
                    "DefinitionSubstitutions": {"Fn": {"Fn::GetAtt": ["X", "Arn"]}, "Plain": "p"},
                    "Name": "flow",
                    "Type": "EXPRESS",
                    "Logging": {"Level": "ALL"},
                    "Tracing": {"Enabled": True},
                    "Tags": {"a": "b"},
                    "PermissionsBoundary": "arn:b",
                    "Policies": ["AWSXrayWriteOnlyAccess"],
                    "Events": {
                        "Every": {
                            "Type": "Schedule",
                            "Properties": {"Schedule": "rate(1 day)", "Input": "{}"},
                        },
                        "Bus": {
                            "Type": "EventBridgeRule",
                            "Properties": {"Pattern": {"source": ["x"]}},
                        },
                        "Later": {
                            "Type": "ScheduleV2",
                            "Properties": {"ScheduleExpression": "rate(1 day)"},
                        },
                        "Http": {"Type": "Api"},
                    },
                    "Odd": 1,
                },
            },
            "FromFile": {
                "Type": "AWS::Serverless::StateMachine",
                "Properties": {
                    "DefinitionUri": "flow.asl.json",
                    "DefinitionSubstitutions": {"A": "b"},
                    "Role": "arn:role",
                },
            },
            "FromS3": {
                "Type": "AWS::Serverless::StateMachine",
                "Properties": {"DefinitionUri": "s3://b/k", "DefinitionSubstitutions": {"A": "b"}},
            },
        }
    )
    resources = out["Resources"]
    flow = resources["Flow"]["Properties"]
    state = flow["Definition"]["States"]["A"]
    assert state["Resource"] == {"Fn::Sub": ["${Fn}", {"Fn": {"Fn::GetAtt": ["X", "Arn"]}}]}
    assert state["Static"] == "p" and state["Other"] == "${Unused}" and state["List"] == ["p"]
    assert flow["StateMachineName"] == "flow" and flow["StateMachineType"] == "EXPRESS"
    assert flow["RoleArn"] == {"Fn::GetAtt": ["FlowRole", "Arn"]}
    assert resources["FlowRole"]["Properties"]["PermissionsBoundary"] == "arn:b"
    assert resources["FlowEvery"]["Properties"]["Targets"][0]["Input"] == "{}"
    assert (
        resources["FlowEveryRole"]["Properties"]["Policies"][0]["PolicyName"]
        == "FlowEveryStartExecution"
    )
    assert resources["FlowLater"]["Type"] == "AWS::Scheduler::Schedule"
    assert resources["FromFile"]["Properties"]["Definition"] == Expression(
        'yamldecode(file("${path.module}/flow.asl.json"))'
    )
    assert resources["FromFile"]["Properties"]["RoleArn"] == "arn:role"
    assert "FromFileRole" not in resources
    assert resources["FromS3"]["Properties"]["DefinitionS3Location"] == {"Bucket": "b", "Key": "k"}
    assert resources["FromS3"]["Properties"]["DefinitionSubstitutions"] == {"A": "b"}
    assert any("FlowHttp: the SAM state machine event type 'Api'" in n for n in notes)
    assert any(
        "FromFile: DefinitionSubstitutions cannot be applied to a definition file" in n
        for n in notes
    )
    assert any("Flow: the converter has no mapping for the SAM property 'Odd'" in n for n in notes)


def test_connectors():
    out, _, notes = expand(
        {
            "Fn": with_attributes(
                function(),
                {
                    "Connectors": {
                        "ToTable": {
                            "Properties": {
                                "Destination": {"Id": "Table"},
                                "Permissions": ["Read", "Write"],
                            }
                        },
                        "ToMany": {
                            "Properties": {
                                "Destination": [
                                    {"Id": "Bucket"},
                                    {"Id": "Queue"},
                                    {"Id": "Topic"},
                                    {"Id": "Flow"},
                                    {"Id": "Missing"},
                                ],
                                "Permissions": ["Write"],
                            }
                        },
                    },
                },
            ),
            "Table": {"Type": "AWS::Serverless::SimpleTable"},
            "Bucket": {"Type": "AWS::S3::Bucket"},
            "Queue": {"Type": "AWS::SQS::Queue"},
            "Topic": {"Type": "AWS::SNS::Topic"},
            "Flow": {"Type": "AWS::Serverless::StateMachine", "Properties": {"Definition": {}}},
            "Api": {"Type": "AWS::Serverless::Api", "Properties": {"StageName": "s"}},
            "Rule": {"Type": "AWS::Events::Rule"},
            "ApiToFn": {
                "Type": "AWS::Serverless::Connector",
                "Properties": {
                    "Source": {"Id": "Api"},
                    "Destination": {"Id": "Fn"},
                    "Permissions": ["Write"],
                },
            },
            "TopicToFn": {
                "Type": "AWS::Serverless::Connector",
                "Properties": {
                    "Source": {"Id": "Topic"},
                    "Destination": {"Id": "Fn"},
                    "Permissions": ["Write"],
                },
            },
            "RuleToFn": {
                "Type": "AWS::Serverless::Connector",
                "Properties": {
                    "Source": {"Id": "Rule"},
                    "Destination": {"Id": "Fn"},
                    "Permissions": ["Write"],
                },
            },
            "QueueToTopic": {
                "Type": "AWS::Serverless::Connector",
                "Properties": {
                    "Source": {"Id": "Queue"},
                    "Destination": {"Id": "Topic"},
                    "Permissions": ["Write"],
                },
            },
            "FnToNothing": {
                "Type": "AWS::Serverless::Connector",
                "Properties": {
                    "Source": "Fn",
                    "Destination": {"Ref": "Topic"},
                    "Permissions": ["Read"],
                },
            },
        }
    )
    resources = out["Resources"]
    policies = {
        p["PolicyName"]: p["PolicyDocument"]["Statement"][0]
        for p in resources["FnRole"]["Properties"]["Policies"]
    }
    table = policies["FnToTablePolicy"]
    assert "dynamodb:GetItem" in table["Action"] and "dynamodb:PutItem" in table["Action"]
    assert table["Resource"][1] == {
        "Fn::Sub": ["${arn}/index/*", {"arn": {"Fn::GetAtt": ["Table", "Arn"]}}]
    }
    assert "s3:PutObject" in policies["FnToManyPolicyDestination0"]["Action"]
    assert policies["FnToManyPolicyDestination2"]["Resource"] == [{"Ref": "Topic"}]
    assert policies["FnToManyPolicyDestination3"]["Resource"] == [{"Ref": "Flow"}]
    assert (
        resources["ApiToFnWriteLambdaPermission"]["Properties"]["Principal"]
        == "apigateway.amazonaws.com"
    )
    assert resources["TopicToFnWriteLambdaPermission"]["Properties"]["SourceArn"] == {
        "Ref": "Topic"
    }
    assert resources["RuleToFnWriteLambdaPermission"]["Properties"]["SourceArn"] == {
        "Fn::GetAtt": ["Rule", "Arn"]
    }
    assert any("FnToMany: one end of the connector lives outside this template" in n for n in notes)
    assert any(
        "QueueToTopic: the connector from AWS::SQS::Queue to AWS::SNS::Topic" in n for n in notes
    )
    assert any(
        "FnToNothing: the connector from AWS::Lambda::Function to AWS::SNS::Topic" in n
        for n in notes
    )


def test_other_transforms_are_kept():
    out, _, _ = expand({"B": {"Type": "AWS::S3::Bucket"}})
    assert "Transform" not in out
    notes: list[str] = []
    out, _ = expand_sam(
        {
            "Transform": [TRANSFORM, "AWS::LanguageExtensions"],
            "Resources": {"B": {"Type": "AWS::S3::Bucket"}},
        },
        notes,
    )
    assert out["Transform"] == ["AWS::LanguageExtensions"]


def test_resource_attributes_computed_tags_and_odd_definition_uri():
    out, _, _ = expand(
        {
            "Fn": with_attributes(
                function({"Tags": {"Fn::If": ["C", {"a": "b"}, {"Ref": "AWS::NoValue"}]}}),
                {
                    "Condition": "C",
                    "DeletionPolicy": "Retain",
                },
            ),
            "Api": {
                "Type": "AWS::Serverless::Api",
                "Properties": {"StageName": "s", "DefinitionUri": {"Bucket": "b", "Key": "k"}},
            },
        },
        {"Conditions": {"C": {"Fn::Equals": ["a", "a"]}}},
    )
    resources = out["Resources"]
    assert resources["Fn"]["Properties"]["Tags"] == {
        "Fn::If": ["C", {"a": "b"}, {"Ref": "AWS::NoValue"}]
    }
    assert (
        resources["FnRole"]["Condition"] == "C"
        and resources["FnRole"]["DeletionPolicy"] == "Retain"
    )
    assert resources["Api"]["Properties"]["BodyS3Location"] == {"Bucket": "b", "Key": "k"}


def test_vpc_function_and_aliases_in_sub():
    out, _, _ = expand(
        {"Fn": function({"VpcConfig": {"SubnetIds": ["s"]}, "AutoPublishAlias": "live"})},
        {
            "Outputs": {
                "Text": {"Value": {"Fn::Sub": "${Fn.Alias} ${Fn.Arn}"}},
                "List": {"Value": {"Fn::Sub": ["${Fn.Version}-${x}", {"x": {"Ref": "Fn.Alias"}}]}},
            }
        },
    )
    managed = out["Resources"]["FnRole"]["Properties"]["ManagedPolicyArns"]
    assert {
        "Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/service-role/"
        "AWSLambdaVPCAccessExecutionRole"
    } in managed
    assert out["Outputs"]["Text"]["Value"] == {"Fn::Sub": "${FnAliaslive} ${Fn.Arn}"}
    assert out["Outputs"]["List"]["Value"] == {
        "Fn::Sub": ["${FnVersion}-${x}", {"x": {"Ref": "FnAliaslive"}}]
    }
