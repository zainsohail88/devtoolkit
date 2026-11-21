"""SQS, SNS, EventBridge, Kinesis and Step Functions mappings."""

from __future__ import annotations

import json
import re

from converters.cfn.pipeline import convert_template
from converters.cfn.resources.messaging import sub_json

DOC = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
ROLE = "arn:aws:iam::123456789012:role/r"


def convert(
    resources: dict,
    parameters: dict | None = None,
    conditions: dict | None = None,
    outputs: dict | None = None,
) -> tuple[str, list[str]]:
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
    return files["main.tf"] + files.get("outputs.tf", ""), result["notes"]


def test_queue_reference_attributes():
    main, notes = convert(
        {"Q": {"Type": "AWS::SQS::Queue", "Properties": {"VisibilityTimeout": "30"}}},
        outputs={
            "Url": {"Value": {"Ref": "Q"}},
            "Arn": {"Value": {"Fn::GetAtt": ["Q", "Arn"]}},
            "Name": {"Value": {"Fn::GetAtt": ["Q", "QueueName"]}},
            "Url2": {"Value": {"Fn::GetAtt": ["Q", "QueueUrl"]}},
        },
    )
    assert "visibility_timeout_seconds = 30" in main
    for text in ("aws_sqs_queue.q.url", "aws_sqs_queue.q.arn", "aws_sqs_queue.q.name"):
        assert text in main
    assert notes == []


def test_queue_policy_for_several_queues_and_a_computed_list():
    main, notes = convert(
        {
            "A": {"Type": "AWS::SQS::Queue"},
            "Policy": {
                "Type": "AWS::SQS::QueuePolicy",
                "Properties": {
                    "Queues": [{"Ref": "A"}, "https://sqs/b", {"Ref": "A"}],
                    "PolicyDocument": DOC,
                },
            },
            "Computed": {
                "Type": "AWS::SQS::QueuePolicy",
                "Properties": {"Queues": {"Fn::Split": [",", "a,b"]}, "PolicyDocument": DOC},
            },
        }
    )
    assert 'resource "aws_sqs_queue_policy" "policy"' in main
    assert "queue_url = aws_sqs_queue.a.url" in main
    assert 'resource "aws_sqs_queue_policy" "policy_target_2"' in main
    assert 'resource "aws_sqs_queue_policy" "policy_a"' in main
    assert "Queues was not converted" in main
    assert any("Computed.Queues' was not converted" in n for n in notes)


def test_topic_subscriptions():
    main, notes = convert(
        {
            "One": {
                "Type": "AWS::SNS::Topic",
                "Properties": {"Subscription": [{"Protocol": "email", "Endpoint": "a@b.c"}]},
            },
            "Computed": {
                "Type": "AWS::SNS::Topic",
                "Condition": "On",
                "Properties": {"Subscription": {"Fn::If": ["On", [], []]}},
            },
            "Plain": {"Type": "AWS::SNS::Topic"},
            "Policy": {
                "Type": "AWS::SNS::TopicPolicy",
                "Properties": {"Topics": [{"Ref": "One"}], "PolicyDocument": DOC},
            },
        },
        conditions={"On": {"Fn::Equals": [1, 1]}},
        outputs={"Name": {"Value": {"Fn::GetAtt": ["One", "TopicName"]}}},
    )
    assert 'resource "aws_sns_topic_subscription" "one_subscription"' in main
    assert "topic_arn = aws_sns_topic.one.arn" in main
    assert "Subscription was not converted" in main
    assert any("Computed: Subscription is computed" in n for n in notes)
    assert 'resource "aws_sns_topic" "plain" {}' in main
    assert "arn = aws_sns_topic.one.arn" in main
    assert "aws_sns_topic.one.name" in main


def test_subscription_region_and_policies():
    main, _ = convert(
        {
            "Sub": {
                "Type": "AWS::SNS::Subscription",
                "Properties": {
                    "TopicArn": "arn:aws:sns:us-east-1:1:t",
                    "Protocol": "lambda",
                    "Endpoint": "arn:aws:lambda:us-east-1:1:function:f",
                    "Region": "us-east-1",
                    "SubscriptionRoleArn": ROLE,
                    "DeliveryPolicy": {"healthyRetryPolicy": {"numRetries": 3}},
                    "ReplayPolicy": {"PointType": "Timestamp"},
                },
            }
        }
    )
    assert 'region                = "us-east-1"' in main
    assert "delivery_policy = jsonencode({" in main
    assert "replay_policy = jsonencode({" in main


def test_rule_targets_of_every_kind():
    main, notes = convert(
        {
            "Rule": {
                "Type": "AWS::Events::Rule",
                "Properties": {
                    "ScheduleExpression": "rate(5 minutes)",
                    "EventBusName": {"Ref": "AWS::NoValue"},
                    "Targets": [
                        {
                            "Id": {"Fn::Sub": "${AWS::StackName}-t"},
                            "Arn": "arn:aws:execute-api:us-east-1:1:api/stage/GET/",
                            "HttpParameters": {
                                "HeaderParameters": {"X-Key": "v"},
                                "PathParameterValues": ["a"],
                                "QueryStringParameters": {"q": "1"},
                            },
                        },
                        {
                            "Id": "batch",
                            "Arn": "arn:aws:batch:us-east-1:1:job-queue/q",
                            "RoleArn": ROLE,
                            "BatchParameters": {
                                "JobDefinition": "def",
                                "JobName": "job",
                                "ArrayProperties": {"Size": 2},
                                "RetryStrategy": {"Attempts": 3},
                            },
                        },
                        {
                            "Id": "ecs",
                            "Arn": "arn:aws:ecs:us-east-1:1:cluster/c",
                            "RoleArn": ROLE,
                            "EcsParameters": {
                                "TaskDefinitionArn": "arn:aws:ecs:us-east-1:1:task-definition/t:1",
                                "TaskCount": "1",
                                "LaunchType": "FARGATE",
                                "PlatformVersion": "LATEST",
                                "Group": "g",
                                "EnableECSManagedTags": True,
                                "EnableExecuteCommand": False,
                                "PropagateTags": "TASK_DEFINITION",
                                "NetworkConfiguration": {
                                    "AwsVpcConfiguration": {
                                        "Subnets": ["subnet-1"],
                                        "SecurityGroups": ["sg-1"],
                                        "AssignPublicIp": "ENABLED",
                                    }
                                },
                            },
                        },
                        {
                            "Id": "ecs2",
                            "Arn": "arn:aws:ecs:us-east-1:1:cluster/c",
                            "EcsParameters": {
                                "TaskDefinitionArn": "t",
                                "NetworkConfiguration": {"AwsVpcConfiguration": {"Subnets": ["s"]}},
                            },
                        },
                        {
                            "Id": "ecs3",
                            "Arn": "arn:aws:ecs:us-east-1:1:cluster/c",
                            "EcsParameters": {"TaskDefinitionArn": "t"},
                        },
                        {
                            "Id": "batch2",
                            "Arn": "arn:aws:batch:us-east-1:1:job-queue/q",
                            "BatchParameters": {"JobDefinition": "d", "JobName": "j"},
                        },
                    ],
                },
            },
            "Bare": {
                "Type": "AWS::Events::Rule",
                "Properties": {"ScheduleExpression": "rate(1 hour)"},
            },
            "Computed": {
                "Type": "AWS::Events::Rule",
                "Properties": {"Targets": {"Fn::If": ["On", [], []]}},
            },
        },
        conditions={"On": {"Fn::Equals": [1, 1]}},
    )
    assert 'resource "aws_cloudwatch_event_target" "rule_target_1"' in main
    assert "event_bus_name" not in main.split('"rule_target_1"')[1].split("}")[0]
    assert "http_target {" in main and "header_parameters = {" in main
    assert "array_size     = 2" in main and "job_attempts   = 3" in main
    assert "assign_public_ip = true" in main
    assert 'subnets = ["s"]' in main
    assert 'resource "aws_cloudwatch_event_target" "rule_ecs3"' in main
    assert any("Computed: Targets is computed" in n for n in notes)
    assert not [n for n in notes if "property" in n]


def test_event_bus_without_policy_and_plain_stream():
    main, notes = convert(
        {
            "Bus": {"Type": "AWS::Events::EventBus", "Properties": {"Name": "b"}},
            "Stream": {"Type": "AWS::Kinesis::Stream", "Properties": {"ShardCount": 1}},
        }
    )
    assert "aws_cloudwatch_event_bus_policy" not in main
    assert 'name        = "${var.stack_name}-Stream"' in main
    assert any("Stream had no Name" in n for n in notes)


def _machine(props: dict) -> dict:
    return {
        "M": {"Type": "AWS::StepFunctions::StateMachine", "Properties": {"RoleArn": ROLE} | props}
    }


def test_definition_substitutions_on_an_object():
    main, notes = convert(
        _machine(
            {
                "Definition": {
                    "StartAt": "A",
                    "States": {
                        "A": {
                            "Type": "Task",
                            "Resource": "${Fn}",
                            "Comment": "calls ${Fn} with ${Other}",
                            "Next": "${Missing}",
                            "Branches": [{"Note": "plain"}],
                        }
                    },
                },
                "DefinitionSubstitutions": {"Fn": {"Fn::GetAtt": ["Q", "Arn"]}},
            }
        )
        | {"Q": {"Type": "AWS::SQS::Queue"}},
        outputs={"Name": {"Value": {"Fn::GetAtt": ["M", "Name"]}}},
    )
    assert "Resource = aws_sqs_queue.q.arn" in main
    assert '"calls ${aws_sqs_queue.q.arn} with $${Other}"' in main
    assert 'Next     = "${Missing}"' not in main and '"$${Missing}"' in main
    assert "aws_sfn_state_machine.m.name" in main
    assert notes == [] or not [n for n in notes if "not converted" in n]


def test_definition_string_forms():
    main, notes = convert(
        _machine({"DefinitionString": '{"StartAt": "A", "States": {}}'})
        | {
            "Bad": {
                "Type": "AWS::StepFunctions::StateMachine",
                "Properties": {
                    "RoleArn": ROLE,
                    "DefinitionString": "not json ${X}",
                    "DefinitionSubstitutions": {"X": "1"},
                },
            },
            "SubList": {
                "Type": "AWS::StepFunctions::StateMachine",
                "Properties": {
                    "RoleArn": ROLE,
                    "DefinitionString": {
                        "Fn::Sub": ['{"StartAt": "${S}", "States": {}}', {"S": "First"}]
                    },
                },
            },
            "SubText": {
                "Type": "AWS::StepFunctions::StateMachine",
                "Properties": {
                    "RoleArn": ROLE,
                    "DefinitionString": {"Fn::Sub": "not ${AWS::Region}"},
                },
            },
            "Joined": {
                "Type": "AWS::StepFunctions::StateMachine",
                "Properties": {
                    "RoleArn": ROLE,
                    "DefinitionString": {"Fn::Join": ["", ['{"StartAt":', '"A"}']]},
                },
            },
        }
    )
    assert 'StartAt = "A"' in main
    assert 'definition = "not json ${X}"' not in main and 'definition = "not json $${X}"' in main
    assert any("Bad: DefinitionSubstitutions could not be applied" in n for n in notes)
    assert 'StartAt = "First"' in main
    assert 'definition = "not ${data.aws_region.current.region}"' in main
    assert 'definition = "{\\"StartAt\\":\\"A\\"}"' in main


def test_sub_json_keeps_plain_placeholders():
    assert sub_json({"Fn::Sub": '{"a": "${AWS::Region}", "b": ["x"]}'}) == {
        "a": {"Fn::Sub": "${AWS::Region}"},
        "b": ["x"],
    }
    assert sub_json({"Fn::Sub": [1, {}]}) is None


def test_bus_policy_rule_on_a_bus_and_stream_encryption():
    main, _ = convert(
        {
            "Bus": {
                "Type": "AWS::Events::EventBus",
                "Properties": {"Name": "b", "Policy": DOC},
            },
            "Rule": {
                "Type": "AWS::Events::Rule",
                "Properties": {"EventBusName": "b", "Targets": [{"Id": "t", "Arn": "arn:x"}]},
            },
            "Stream": {
                "Type": "AWS::Kinesis::Stream",
                "Properties": {
                    "Name": "s",
                    "StreamEncryption": {"EncryptionType": "KMS", "KeyId": "alias/k"},
                },
            },
        }
    )
    assert 'resource "aws_cloudwatch_event_bus_policy" "bus_policy"' in main
    assert 'event_bus_name = "b"' in main
    assert 'encryption_type = "KMS"' in main and 'kms_key_id      = "alias/k"' in main


def test_log_destinations():
    main, _ = convert(
        _machine(
            {
                "Definition": {"StartAt": "A", "States": {}},
                "LoggingConfiguration": {
                    "Level": "ALL",
                    "Destinations": [
                        {"CloudWatchLogsLogGroup": {"LogGroupArn": "arn:aws:logs:r:1:log-group:g"}}
                    ],
                },
                "EncryptionConfiguration": {"Type": "AWS_OWNED_KEY"},
            }
        )
        | {
            "Kept": {
                "Type": "AWS::StepFunctions::StateMachine",
                "Properties": {
                    "RoleArn": ROLE,
                    "Definition": {},
                    "LoggingConfiguration": {
                        "Destinations": [
                            {
                                "CloudWatchLogsLogGroup": {
                                    "LogGroupArn": "arn:aws:logs:r:1:log-group:g:*"
                                }
                            }
                        ]
                    },
                },
            },
            "Param": {
                "Type": "AWS::StepFunctions::StateMachine",
                "Properties": {
                    "RoleArn": ROLE,
                    "Definition": {},
                    "LoggingConfiguration": {
                        "Destinations": [
                            {"CloudWatchLogsLogGroup": {"LogGroupArn": {"Ref": "LogArn"}}}
                        ]
                    },
                },
            },
            "Empty": {
                "Type": "AWS::StepFunctions::StateMachine",
                "Properties": {
                    "RoleArn": ROLE,
                    "Definition": {},
                    "LoggingConfiguration": {"Level": "OFF", "Destinations": [{}]},
                },
            },
            "None": {
                "Type": "AWS::StepFunctions::StateMachine",
                "Properties": {
                    "RoleArn": ROLE,
                    "Definition": {},
                    "LoggingConfiguration": {"Level": "OFF"},
                },
            },
        },
        parameters={"LogArn": {"Type": "String"}},
    )
    assert 'log_destination = "arn:aws:logs:r:1:log-group:g:*"' in main
    assert main.count(':log-group:g:*"') == 2
    assert 'log_destination = "${var.log_arn}:*"' in main
    assert 'type = "AWS_OWNED_KEY"' in main


def squash(text: str) -> str:
    """HCL without the alignment spaces before '='."""
    return re.sub(r" +=", " =", text)


ON = {"On": {"Fn::Equals": [{"Ref": "Env"}, "prod"]}}
ENV = {"Env": {"Type": "String"}}
NO = {"Ref": "AWS::NoValue"}


def _if(value: object) -> dict:
    return {"Fn::If": ["On", value, NO]}


def _block(main: str, header: str) -> str:
    return main.split(header + " {")[1].split("\n}")[0]


def test_policies_for_optional_queues_and_topics_are_counted():
    main, notes = convert(
        {
            "A": {"Type": "AWS::SQS::Queue"},
            "B": {"Type": "AWS::SQS::Queue"},
            "T": {"Type": "AWS::SNS::Topic"},
            "QP": {
                "Type": "AWS::SQS::QueuePolicy",
                "Properties": {
                    "PolicyDocument": DOC,
                    "Queues": [_if({"Ref": "B"}), {"Ref": "A"}, NO],
                },
            },
            "TP": {
                "Type": "AWS::SNS::TopicPolicy",
                "Properties": {"PolicyDocument": DOC, "Topics": [_if({"Ref": "T"})]},
            },
        },
        parameters=ENV,
        conditions=ON,
        outputs={"Id": {"Value": {"Ref": "TP"}}},
    )
    main = squash(main)
    main_policy = _block(main, 'resource "aws_sqs_queue_policy" "qp"')
    assert "count" not in main_policy and "queue_url = aws_sqs_queue.a.url" in main_policy
    copy = _block(main, 'resource "aws_sqs_queue_policy" "qp_b"')
    assert "count = local.on ? 1 : 0" in copy and "queue_url = aws_sqs_queue.b.url" in copy
    topic = _block(main, 'resource "aws_sns_topic_policy" "tp"')
    assert "count = local.on ? 1 : 0" in topic and "arn = aws_sns_topic.t.arn" in topic
    assert "aws_sns_topic_policy.tp[0].id" in main
    assert "null" not in main
    assert notes == []


def test_optional_subscriptions_and_rule_targets_are_counted():
    main, notes = convert(
        {
            "Q": {"Type": "AWS::SQS::Queue"},
            "Topic": {
                "Type": "AWS::SNS::Topic",
                "Properties": {
                    "Subscription": [
                        _if({"Protocol": "email", "Endpoint": "ops@example.com"}),
                        NO,
                        {"Fn::If": ["On", {"Protocol": "sqs"}, {"Protocol": "email"}]},
                    ]
                },
            },
            "Rule": {
                "Type": "AWS::Events::Rule",
                "Properties": {
                    "ScheduleExpression": "rate(5 minutes)",
                    "Targets": [
                        _if({"Id": "q", "Arn": {"Fn::GetAtt": ["Q", "Arn"]}}),
                        NO,
                        {"Ref": "Targets"},
                    ],
                },
            },
        },
        parameters=ENV | {"Targets": {"Type": "String"}},
        conditions=ON,
    )
    main = squash(main)
    subscription = _block(main, 'resource "aws_sns_topic_subscription" "topic_subscription_1"')
    assert "count = local.on ? 1 : 0" in subscription
    assert (
        'protocol = "email"' in subscription
        and "topic_arn = aws_sns_topic.topic.arn" in subscription
    )
    target = _block(main, 'resource "aws_cloudwatch_event_target" "rule_q"')
    assert "count = local.on ? 1 : 0" in target and "arn = aws_sqs_queue.q.arn" in target
    assert main.count('resource "aws_sns_topic_subscription"') == 1
    assert main.count('resource "aws_cloudwatch_event_target"') == 1
    assert "Subscription[2] was not converted" in main
    assert "Targets[2] was not converted" in main
    assert any("'Topic.Subscription[2]' is computed by a function" in n for n in notes)
    assert any("'Rule.Targets[2]' is computed by a function" in n for n in notes)


def _ecs_rule(network: object) -> dict:
    return {
        "Type": "AWS::Events::Rule",
        "Properties": {
            "ScheduleExpression": "rate(1 hour)",
            "Targets": [
                {
                    "Id": "t",
                    "Arn": "arn:aws:ecs:us-east-1:123456789012:cluster/c",
                    "RoleArn": ROLE,
                    "EcsParameters": {
                        "TaskDefinitionArn": "arn:task-definition/t:1",
                        "NetworkConfiguration": network,
                        "ArrayProperties": {"Ref": "Size"},
                    },
                }
            ],
        },
    }


def test_ecs_target_network_configuration_from_functions():
    main, notes = convert(
        {
            "Public": _ecs_rule(
                {
                    "AwsVpcConfiguration": {
                        "Subnets": ["subnet-1"],
                        "AssignPublicIp": {"Fn::If": ["On", "ENABLED", "DISABLED"]},
                    }
                }
            ),
            "Computed": _ecs_rule({"Ref": "Network"}),
            "Inner": _ecs_rule({"AwsVpcConfiguration": {"Ref": "Network"}}),
        },
        parameters=ENV | {"Network": {"Type": "String"}, "Size": {"Type": "String"}},
        conditions=ON,
    )
    main = squash(main)
    assert 'assign_public_ip = (local.on ? "ENABLED" : "DISABLED") == "ENABLED"' in main
    assert "NetworkConfiguration was not converted" in main
    assert "AwsVpcConfiguration was not converted" in main
    assert "ArrayProperties was not converted" in main
    assert any("Computed.Targets[0].EcsParameters.NetworkConfiguration" in n for n in notes)
    assert any("Inner.Targets[0].EcsParameters.NetworkConfiguration.AwsVpc" in n for n in notes)


def test_definition_substitutions_stay_text():
    main, _ = convert(
        {
            "Machine": {
                "Type": "AWS::StepFunctions::StateMachine",
                "Properties": {
                    "RoleArn": ROLE,
                    "DefinitionString": json.dumps(
                        {
                            "StartAt": "A",
                            "States": {
                                "A": {
                                    "Type": "Pass",
                                    "Result": {
                                        "n": "${N}",
                                        "f": "${F}",
                                        "b": "${B}",
                                        "p": "${P}",
                                        "s": "${S}",
                                        "mixed": "n=${N}",
                                    },
                                    "End": True,
                                }
                            },
                        }
                    ),
                    "DefinitionSubstitutions": {
                        "N": 5,
                        "F": 1.5,
                        "B": True,
                        "P": {"Ref": "Retries"},
                        "S": {"Ref": "Name"},
                    },
                },
            }
        },
        parameters={"Retries": {"Type": "Number"}, "Name": {"Type": "String"}},
    )
    main = squash(main)
    for line in ('n = "5"', 'f = "1.5"', 'b = "true"', "p = tostring(var.retries)", "s = var.name"):
        assert line in main
    assert 'mixed = "n=5"' in main
