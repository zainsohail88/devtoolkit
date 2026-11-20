"""SQS, SNS, EventBridge, Kinesis and Step Functions mappings."""

from __future__ import annotations

import json

from converters.cfn import convert_template

DOC = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
ROLE = "arn:aws:iam::123456789012:role/r"


def convert(resources: dict, **sections: object) -> tuple[str, list[str]]:
    result = convert_template(json.dumps({"Resources": resources, **sections}), stack_name="demo")
    files = {f["path"]: f["content"] for f in result["files"]}
    return files["main.tf"] + files.get("outputs.tf", ""), result["notes"]


def test_queue_reference_attributes():
    main, notes = convert(
        {"Q": {"Type": "AWS::SQS::Queue", "Properties": {"VisibilityTimeout": "30"}}},
        Outputs={
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
        Conditions={"On": {"Fn::Equals": [1, 1]}},
        Outputs={"Name": {"Value": {"Fn::GetAtt": ["One", "TopicName"]}}},
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
        Conditions={"On": {"Fn::Equals": [1, 1]}},
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


def _machine(**props: object) -> dict:
    return {
        "M": {"Type": "AWS::StepFunctions::StateMachine", "Properties": {"RoleArn": ROLE, **props}}
    }


def test_definition_substitutions_on_an_object():
    main, notes = convert(
        _machine(
            Definition={
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
            DefinitionSubstitutions={"Fn": {"Fn::GetAtt": ["Q", "Arn"]}},
        )
        | {"Q": {"Type": "AWS::SQS::Queue"}},
        Outputs={"Name": {"Value": {"Fn::GetAtt": ["M", "Name"]}}},
    )
    assert "Resource = aws_sqs_queue.q.arn" in main
    assert '"calls ${aws_sqs_queue.q.arn} with $${Other}"' in main
    assert 'Next     = "${Missing}"' not in main and '"$${Missing}"' in main
    assert "aws_sfn_state_machine.m.name" in main
    assert notes == [] or not [n for n in notes if "not converted" in n]


def test_definition_string_forms():
    main, notes = convert(
        {
            **_machine(DefinitionString='{"StartAt": "A", "States": {}}'),
            **{
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
    from converters.cfn.resources.messaging import sub_json

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
        {
            **_machine(
                Definition={"StartAt": "A", "States": {}},
                LoggingConfiguration={
                    "Level": "ALL",
                    "Destinations": [
                        {"CloudWatchLogsLogGroup": {"LogGroupArn": "arn:aws:logs:r:1:log-group:g"}}
                    ],
                },
                EncryptionConfiguration={"Type": "AWS_OWNED_KEY"},
            ),
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
        Parameters={"LogArn": {"Type": "String"}},
    )
    assert 'log_destination = "arn:aws:logs:r:1:log-group:g:*"' in main
    assert main.count(':log-group:g:*"') == 2
    assert 'log_destination = "${var.log_arn}:*"' in main
    assert 'type = "AWS_OWNED_KEY"' in main
