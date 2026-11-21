"""CloudWatch Logs, alarms and dashboards."""

from __future__ import annotations

from typing import Any

from converters.cfn.builder import Builder, Props, tags_map
from converters.cfn.context import Context
from converters.cfn.expressions import intrinsic_name
from converters.cfn.models import Block, Call, ResourceInfo
from converters.cfn.resources.base import ResourceConverter
from converters.cfn.resources.messaging import sub_json


def _dimensions(ctx: Context, value: Any) -> Any:
    """[{Name, Value}] (alarms) or [{Key, Value}] (metric filters) as a map."""
    if isinstance(value, list) and all(isinstance(d, dict) for d in value):
        renamed = [{"Key": d.get("Name", d.get("Key")), "Value": d.get("Value")} for d in value]
        return tags_map(ctx, renamed)
    return tags_map(ctx, value)


def _dimensions_attr(p: Props) -> None:
    """The Dimensions property, when present, as the dimensions map."""
    value = p.take("Dimensions")
    if value is not None:
        p.set("dimensions", _dimensions(p.ctx, value))


class LogGroup(ResourceConverter):
    """AWS::Logs::LogGroup as aws_cloudwatch_log_group."""

    cfn_type = "AWS::Logs::LogGroup"
    tf_type = "aws_cloudwatch_log_group"
    ref = "name"
    import_id = "LogGroupName"

    def prepare(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """The CloudFormation Arn of a log group ends in ':*' (it covers the log streams);
        the arn of the provider has no suffix. IAM policies written for the former must
        keep matching the streams."""
        index = "[0]" if info.conditional else ""
        info.attrs["Arn"] = "arn_with_streams"
        info.overrides["arn_with_streams"] = f'"${{{info.address}{index}.arn}}:*"'

    def convert(self, b: Builder) -> None:
        """The log group settings."""
        p = b.props
        p.attr("name", "LogGroupName")
        p.number("retention_in_days", "RetentionInDays")
        p.attr("kms_key_id", "KmsKeyId")
        p.attr("log_group_class", "LogGroupClass")
        p.boolean("deletion_protection_enabled", "DeletionProtectionEnabled")
        p.tags()


class LogMetricFilter(ResourceConverter):
    """AWS::Logs::MetricFilter as aws_cloudwatch_log_metric_filter."""

    cfn_type = "AWS::Logs::MetricFilter"
    tf_type = "aws_cloudwatch_log_metric_filter"
    ref = "name"

    def convert(self, b: Builder) -> None:
        """The filter and its metric transformation."""
        p = b.props
        b.name_attr("name", "FilterName")
        p.attr("log_group_name", "LogGroupName")
        p.attr("pattern", "FilterPattern")
        p.boolean("apply_on_transformed_logs", "ApplyOnTransformedLogs")
        # CloudFormation takes a list but allows exactly one transformation, as does Terraform.
        p.block("metric_transformation", "MetricTransformations", self.fill_transformation)

    def fill_transformation(self, m: Props) -> None:
        """The metric_transformation block. The default value is text in Terraform."""
        m.attr("name", "MetricName")
        m.attr("namespace", "MetricNamespace")
        m.attr("value", "MetricValue")
        default = m.take("DefaultValue")
        if default is not None:
            text = str(default) if isinstance(default, int | float) else m.ctx.expr(default)
            m.set("default_value", text)
        m.attr("unit", "Unit")
        _dimensions_attr(m)


class LogSubscriptionFilter(ResourceConverter):
    """AWS::Logs::SubscriptionFilter as aws_cloudwatch_log_subscription_filter."""

    cfn_type = "AWS::Logs::SubscriptionFilter"
    tf_type = "aws_cloudwatch_log_subscription_filter"
    ref = "name"

    def convert(self, b: Builder) -> None:
        """The filter settings."""
        p = b.props
        b.name_attr("name", "FilterName")
        p.attr("log_group_name", "LogGroupName")
        p.attr("filter_pattern", "FilterPattern")
        p.attr("destination_arn", "DestinationArn")
        p.attr("role_arn", "RoleArn")
        p.attr("distribution", "Distribution")
        p.boolean("apply_on_transformed_logs", "ApplyOnTransformedLogs")
        p.list_attr("emit_system_fields", "EmitSystemFields")


class CloudWatchAlarm(ResourceConverter):
    """AWS::CloudWatch::Alarm as aws_cloudwatch_metric_alarm."""

    cfn_type = "AWS::CloudWatch::Alarm"
    tf_type = "aws_cloudwatch_metric_alarm"
    ref = "alarm_name"
    attrs = {"Arn": "arn"}
    import_id = "AlarmName"

    def convert(self, b: Builder) -> None:
        """The alarm settings and its metric queries."""
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
        _dimensions_attr(p)
        p.block("metric_query", "Metrics", self.fill_metric_query)
        p.tags()

    def fill_metric_query(self, q: Props) -> None:
        """One metric_query block, with MetricStat as its metric block."""
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


class CloudWatchDashboard(ResourceConverter):
    """AWS::CloudWatch::Dashboard as aws_cloudwatch_dashboard."""

    cfn_type = "AWS::CloudWatch::Dashboard"
    tf_type = "aws_cloudwatch_dashboard"
    ref = "dashboard_name"
    import_id = "DashboardName"

    def convert(self, b: Builder) -> None:
        """The dashboard, with a `!Sub` JSON body as a jsonencode() structure."""
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


CONVERTERS: tuple[type[ResourceConverter], ...] = (
    LogGroup,
    LogMetricFilter,
    LogSubscriptionFilter,
    CloudWatchAlarm,
    CloudWatchDashboard,
)
