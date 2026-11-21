"""CloudWatch Logs, alarm and dashboard mappings."""

from __future__ import annotations

import json
import re

from converters.cfn.pipeline import convert_template

NO = {"Ref": "AWS::NoValue"}


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


def test_log_group_arn_keeps_the_stream_wildcard_also_when_conditional():
    main, _ = convert(
        {
            "Logs": {
                "Type": "AWS::Logs::LogGroup",
                "Condition": "On",
                "Properties": {"KmsKeyId": "k"},
            }
        },
        conditions={"On": {"Fn::Equals": [1, 1]}},
        outputs={
            "Arn": {"Value": {"Fn::GetAtt": ["Logs", "Arn"]}, "Condition": "On"},
            "Name": {"Value": {"Ref": "Logs"}, "Condition": "On"},
        },
    )
    assert '"${aws_cloudwatch_log_group.logs[0].arn}:*"' in main
    assert "aws_cloudwatch_log_group.logs[0].name" in main


def test_metric_filter_values_and_dimensions():
    main, notes = convert(
        {
            "Filter": {
                "Type": "AWS::Logs::MetricFilter",
                "Properties": {
                    "FilterName": "f",
                    "LogGroupName": "/g",
                    "FilterPattern": "x",
                    "ApplyOnTransformedLogs": "false",
                    "MetricTransformations": [
                        {
                            "MetricName": "M",
                            "MetricNamespace": "N",
                            "MetricValue": "1",
                            "DefaultValue": {"Ref": "Default"},
                            "Dimensions": {"Fn::If": ["On", [], []]},
                        }
                    ],
                },
            }
        },
        parameters={"Default": {"Type": "String"}},
        conditions={"On": {"Fn::Equals": [1, 1]}},
        outputs={"Name": {"Value": {"Ref": "Filter"}}},
    )
    assert "default_value = var.default" in main
    assert "apply_on_transformed_logs = false" in main
    assert "dimensions    = { for tag in local.on ? [] : [] : tag.Key => tag.Value }" in main
    assert "aws_cloudwatch_log_metric_filter.filter.name" in main
    assert not [n for n in notes if "not converted" in n]


def test_subscription_filter():
    main, notes = convert(
        {
            "Sub": {
                "Type": "AWS::Logs::SubscriptionFilter",
                "Properties": {
                    "LogGroupName": "/g",
                    "FilterPattern": "",
                    "DestinationArn": "arn:aws:lambda:us-east-1:1:function:f",
                    "EmitSystemFields": "@aws.region",
                },
            }
        }
    )
    assert 'name               = "${var.stack_name}-Sub"' in main
    assert 'emit_system_fields = ["@aws.region"]' in main
    assert any("Sub had no FilterName" in n for n in notes)


def test_alarm_metric_queries_and_dimension_maps():
    main, _ = convert(
        {
            "Alarm": {
                "Type": "AWS::CloudWatch::Alarm",
                "Properties": {
                    "AlarmName": "a",
                    "ComparisonOperator": "GreaterThanThreshold",
                    "EvaluationPeriods": 1,
                    "Threshold": 1,
                    "ThresholdMetricId": "band",
                    "Metrics": [
                        {"Id": "band", "Expression": "ANOMALY_DETECTION_BAND(m, 2)"},
                        {"Id": "m", "AccountId": "123456789012", "Period": 60, "MetricStat": "x"},
                        {
                            "Id": "n",
                            "MetricStat": {"Metric": {}, "Period": 60, "Stat": "Sum"},
                        },
                    ],
                },
            },
            "Mapped": {
                "Type": "AWS::CloudWatch::Alarm",
                "Properties": {
                    "AlarmName": "b",
                    "ComparisonOperator": "LessThanThreshold",
                    "EvaluationPeriods": 1,
                    "Threshold": 1,
                    "Dimensions": {"Service": "app"},
                },
            },
        },
        outputs={"Name": {"Value": {"Ref": "Alarm"}}},
    )
    assert 'threshold_metric_id = "band"' in main
    assert 'account_id = "123456789012"' in main
    assert "metric {\n      period = 60" in main
    assert 'dimensions = {\n    Service = "app"\n  }' in main
    assert "aws_cloudwatch_metric_alarm.alarm.alarm_name" in main


def test_dashboard_body_forms():
    main, notes = convert(
        {
            "Text": {
                "Type": "AWS::CloudWatch::Dashboard",
                "Properties": {
                    "DashboardName": "t",
                    "DashboardBody": {"Fn::Sub": "not json ${AWS::Region}"},
                },
            },
            "Joined": {
                "Type": "AWS::CloudWatch::Dashboard",
                "Properties": {
                    "DashboardName": "j",
                    "DashboardBody": {"Fn::Join": ["", ['{"widgets":', "[]}"]]},
                },
            },
        },
        outputs={"Name": {"Value": {"Ref": "Text"}}},
    )
    assert 'dashboard_body = "not json ${data.aws_region.current.region}"' in main
    assert 'dashboard_body = "{\\"widgets\\":[]}"' in main
    assert "aws_cloudwatch_dashboard.text.dashboard_name" in main
    assert notes == []


def test_alarm_actions_drop_novalue_items():
    main, notes = convert(
        {
            "Alarm": {
                "Type": "AWS::CloudWatch::Alarm",
                "Properties": {
                    "AlarmName": "a",
                    "ComparisonOperator": "GreaterThanThreshold",
                    "EvaluationPeriods": 1,
                    "Threshold": 1,
                    "MetricName": "Errors",
                    "Namespace": "App",
                    "Period": 60,
                    "Statistic": "Sum",
                    "AlarmActions": [
                        {"Fn::If": ["On", "arn:aws:sns:us-east-1:123456789012:t", NO]},
                        "arn:aws:sns:us-east-1:123456789012:u",
                    ],
                    "OKActions": [NO],
                },
            }
        },
        parameters={"Env": {"Type": "String"}},
        conditions={"On": {"Fn::Equals": [{"Ref": "Env"}, "prod"]}},
    )
    assert (
        'alarm_actions = [for item in [local.on ? "arn:aws:sns:us-east-1:123456789012:t" : null,'
        ' "arn:aws:sns:us-east-1:123456789012:u"] : item if item != null]'
    ) in re.sub(r" +=", " =", main)
    assert "ok_actions" in main and "[null]" not in main


def test_alarm_metric_stat_from_a_function_is_reported():
    main, notes = convert(
        {
            "Alarm": {
                "Type": "AWS::CloudWatch::Alarm",
                "Properties": {
                    "AlarmName": "a",
                    "ComparisonOperator": "GreaterThanThreshold",
                    "EvaluationPeriods": 1,
                    "Threshold": 1,
                    "Metrics": [
                        {"Id": "m1", "MetricStat": {"Ref": "Stat"}},
                        {"Id": "m2", "MetricStat": {"Metric": {"Ref": "Metric"}, "Stat": "Sum"}},
                    ],
                },
            }
        },
        parameters={"Stat": {"Type": "String"}, "Metric": {"Type": "String"}},
    )
    assert "MetricStat was not converted" in main
    assert "Metric was not converted" in main
    assert any("Alarm.Metrics[0].MetricStat' is computed" in n for n in notes)
    assert any("Alarm.Metrics[1].MetricStat.Metric' is computed" in n for n in notes)
