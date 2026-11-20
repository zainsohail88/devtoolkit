"""CloudWatch Logs, alarms and dashboards."""

from __future__ import annotations

from typing import Any

from converters.cfn.builder import Builder, Props, tags_map
from converters.cfn.context import Context, ResourceInfo, intrinsic_name
from converters.cfn.hcl import Block, Call
from converters.cfn.resources import resource
from converters.cfn.resources.messaging import sub_json


def _log_group_arn(info: ResourceInfo, resource: dict[str, Any]) -> None:
    """CloudFormation's LogGroup Arn ends in ':*' (it covers the log streams); the provider's
    arn doesn't. IAM policies written for the former must keep matching the streams."""
    index = "[0]" if info.conditional else ""
    info.attrs["Arn"] = "arn_with_streams"
    info.overrides["arn_with_streams"] = f'"${{{info.address}{index}.arn}}:*"'


@resource(
    "AWS::Logs::LogGroup",
    "aws_cloudwatch_log_group",
    ref="name",
    import_id="LogGroupName",
    prepare=_log_group_arn,
)
def log_group(b: Builder) -> None:
    p = b.props
    p.attr("name", "LogGroupName")
    p.number("retention_in_days", "RetentionInDays")
    p.attr("kms_key_id", "KmsKeyId")
    p.attr("log_group_class", "LogGroupClass")
    p.boolean("deletion_protection_enabled", "DeletionProtectionEnabled")
    p.tags()


def _dimensions(ctx: Context, value: Any) -> Any:
    """[{Name, Value}] (alarms) or [{Key, Value}] (metric filters) as a map."""
    if isinstance(value, list) and all(isinstance(d, dict) for d in value):
        renamed = [{"Key": d.get("Name", d.get("Key")), "Value": d.get("Value")} for d in value]
        return tags_map(ctx, renamed)
    return tags_map(ctx, value)


def _metric_transformation(m: Props) -> None:
    m.attr("name", "MetricName")
    m.attr("namespace", "MetricNamespace")
    m.attr("value", "MetricValue")
    m.attr(
        "default_value",
        "DefaultValue",
        lambda v: str(v) if isinstance(v, int | float) else m.ctx.expr(v),
    )
    m.attr("unit", "Unit")
    m.attr("dimensions", "Dimensions", lambda v: _dimensions(m.ctx, v))


@resource("AWS::Logs::MetricFilter", "aws_cloudwatch_log_metric_filter", ref="name")
def log_metric_filter(b: Builder) -> None:
    p = b.props
    b.name_attr("name", "FilterName")
    p.attr("log_group_name", "LogGroupName")
    p.attr("pattern", "FilterPattern")
    p.boolean("apply_on_transformed_logs", "ApplyOnTransformedLogs")
    # CloudFormation takes a list but allows exactly one transformation, as does Terraform.
    p.block("metric_transformation", "MetricTransformations", _metric_transformation)


@resource("AWS::Logs::SubscriptionFilter", "aws_cloudwatch_log_subscription_filter", ref="name")
def log_subscription_filter(b: Builder) -> None:
    p = b.props
    b.name_attr("name", "FilterName")
    p.attr("log_group_name", "LogGroupName")
    p.attr("filter_pattern", "FilterPattern")
    p.attr("destination_arn", "DestinationArn")
    p.attr("role_arn", "RoleArn")
    p.attr("distribution", "Distribution")
    p.boolean("apply_on_transformed_logs", "ApplyOnTransformedLogs")
    p.list_attr("emit_system_fields", "EmitSystemFields")


def _metric_query(q: Props) -> None:
    q.attr("id", "Id")
    q.attr("expression", "Expression")
    q.attr("label", "Label")
    q.boolean("return_data", "ReturnData")
    q.number("period", "Period")
    q.attr("account_id", "AccountId")
    stat = q.literal("MetricStat", dict)
    if stat is None:
        return
    inner = Props(q.ctx, stat, f"{q.where}.MetricStat", q.owner)
    metric = inner.literal("Metric", dict)
    if metric is not None:
        if "MetricName" in metric:
            inner.set("metric_name", q.ctx.expr(metric["MetricName"]))
        if "Namespace" in metric:
            inner.set("namespace", q.ctx.expr(metric["Namespace"]))
        if metric.get("Dimensions"):
            inner.set("dimensions", _dimensions(q.ctx, metric["Dimensions"]))
    inner.number("period", "Period")
    inner.attr("stat", "Stat")
    inner.attr("unit", "Unit")
    inner.finish()
    q.items.append(Block("metric", [], inner.items))


@resource(
    "AWS::CloudWatch::Alarm",
    "aws_cloudwatch_metric_alarm",
    ref="alarm_name",
    attrs={"Arn": "arn"},
    import_id="AlarmName",
)
def cloudwatch_alarm(b: Builder) -> None:
    p = b.props
    b.name_attr("alarm_name", "AlarmName")
    p.attr("alarm_description", "AlarmDescription")
    p.boolean("actions_enabled", "ActionsEnabled")
    p.list_attr("alarm_actions", "AlarmActions")
    p.list_attr("ok_actions", "OKActions")
    p.list_attr("insufficient_data_actions", "InsufficientDataActions")
    p.attr("comparison_operator", "ComparisonOperator")
    p.number("evaluation_periods", "EvaluationPeriods")
    p.number("datapoints_to_alarm", "DatapointsToAlarm")
    p.attr("metric_name", "MetricName")
    p.attr("namespace", "Namespace")
    p.number("period", "Period")
    p.attr("statistic", "Statistic")
    p.attr("extended_statistic", "ExtendedStatistic")
    p.number("threshold", "Threshold")
    p.attr("threshold_metric_id", "ThresholdMetricId")
    p.attr("treat_missing_data", "TreatMissingData")
    p.attr("unit", "Unit")
    p.attr("evaluate_low_sample_count_percentiles", "EvaluateLowSampleCountPercentile")
    p.attr("dimensions", "Dimensions", lambda v: _dimensions(b.ctx, v))
    p.block("metric_query", "Metrics", _metric_query)
    p.tags()


@resource(
    "AWS::CloudWatch::Dashboard",
    "aws_cloudwatch_dashboard",
    ref="dashboard_name",
    import_id="DashboardName",
)
def cloudwatch_dashboard(b: Builder) -> None:
    p = b.props
    b.name_attr("dashboard_name", "DashboardName")
    body = p.peek("DashboardBody")
    parsed = (
        sub_json(body) if isinstance(body, dict) and intrinsic_name(body) == "Fn::Sub" else None
    )
    if parsed is not None:
        p.drop("DashboardBody")
        p.set("dashboard_body", Call("jsonencode", b.ctx.expr(parsed)))
    else:
        p.json("dashboard_body", "DashboardBody")
