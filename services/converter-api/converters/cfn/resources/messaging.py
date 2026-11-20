"""SQS, SNS, EventBridge, Kinesis and Step Functions."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props, snippet
from converters.cfn.context import (
    Context,
    Expression,
    ResourceInfo,
    _wrap,
    intrinsic_name,
    json_value,
)
from converters.cfn.hcl import Attr, Raw
from converters.cfn.naming import UniqueNames, identifier
from converters.cfn.resources import resource

_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
_NO_VALUE = {"Ref": "AWS::NoValue"}


def operand(value: Any) -> str:
    """An HCL value as an operand of `==`: in parentheses unless it is a plain reference or
    string (`a ? b : c == d` would compare c alone)."""
    return _wrap(hcl.inline(value))


def optional(value: Any) -> bool:
    """Whether a list item is `!If [Cond, x, !Ref AWS::NoValue]` (either way round): an item
    that only exists when a condition holds. Usable before conversion (no Context)."""
    if not (isinstance(value, dict) and intrinsic_name(value) == "Fn::If"):
        return False
    args = value["Fn::If"]
    return isinstance(args, list) and len(args) == 3 and (_NO_VALUE in args[1:])


def main_index(values: list[Any]) -> int:
    """Which of several targets gets the main resource: the first that is always there."""
    return next((i for i, value in enumerate(values) if not optional(value)), 0)


def only_when(b: Builder, when: str | None) -> None:
    """The main resource exists only `when` (all its targets are optional). The framework
    counts the main resource with `b.count_items()` once the converter returns, so that call
    gets the condition too. Call it after every add_resource: those keep their own `when`."""
    if not when:
        return
    count_items = b.count_items

    def counted(extra: str | None = None) -> list[Any]:
        return count_items(extra or when)

    b.count_items = counted  # type: ignore[method-assign]


def counted_refs(info: ResourceInfo) -> None:
    """References to a main resource counted by only_when: `address[0].attribute`. (The
    registration sets `conditional` from the Condition alone, after prepare hooks.)"""
    for attribute in {info.ref, *info.attrs.values()}:
        info.overrides[attribute] = f"{info.address}[0].{attribute}"


def _prepare_targets(cfn_name: str) -> Callable[[ResourceInfo, dict[str, Any]], None]:
    """References to a policy whose every target is optional need `[0]`."""

    def prepare(info: ResourceInfo, resource: dict[str, Any]) -> None:
        targets = (resource.get("Properties") or {}).get(cfn_name)
        if isinstance(targets, list):
            targets = [target for target in targets if target != _NO_VALUE]
            if targets and optional(targets[main_index(targets)]):
                counted_refs(info)

    return prepare


def _one_per_target(
    b: Builder, cfn_name: str, tf_type: str, argument: str, items: list[Any]
) -> None:
    """Resource policies that CloudFormation applies to a list of targets (queues, topics):
    the first target that is always there is the main resource, each further one gets a
    copy. `!If [Cond, x, !Ref AWS::NoValue]` targets get counted copies."""
    targets = b.props.peek(cfn_name)
    if isinstance(targets, list):
        targets = [target for target in targets if not b.ctx.is_no_value(target)]
    if not isinstance(targets, list) or not targets:
        # A computed list: finish() keeps it as a comment and reports it.
        b.props.items.extend(items)
        return
    b.props.drop(cfn_name)
    main, main_when = main_index(targets), None
    suffixes = UniqueNames()
    for index, item in enumerate(targets, 1):
        when, target = b.ctx.optional_item(item)
        if index - 1 == main:
            b.props.items.insert(0, Attr(argument, b.ctx.expr(target)))
            b.props.items.extend(items)
            main_when = when
            continue
        label = str(target["Ref"]) if isinstance(target, dict) and "Ref" in target else ""
        suffix = suffixes.take("_" + identifier(label or f"target_{index}", f"target_{index}"))
        b.add_resource(tf_type, suffix, [Attr(argument, b.ctx.expr(target)), *items], when=when)
    only_when(b, main_when)


# --- SQS --------------------------------------------------------------------------------


@resource(
    "AWS::SQS::Queue",
    "aws_sqs_queue",
    ref="url",
    attrs={"Arn": "arn", "QueueName": "name", "QueueUrl": "url"},
)
def sqs_queue(b: Builder) -> None:
    p = b.props
    p.attr("name", "QueueName")
    p.boolean("fifo_queue", "FifoQueue")
    p.boolean("content_based_deduplication", "ContentBasedDeduplication")
    p.attr("deduplication_scope", "DeduplicationScope")
    p.attr("fifo_throughput_limit", "FifoThroughputLimit")
    p.number("visibility_timeout_seconds", "VisibilityTimeout")
    p.number("message_retention_seconds", "MessageRetentionPeriod")
    p.number("delay_seconds", "DelaySeconds")
    p.number("max_message_size", "MaximumMessageSize")
    p.number("receive_wait_time_seconds", "ReceiveMessageWaitTimeSeconds")
    p.attr("kms_master_key_id", "KmsMasterKeyId")
    p.number("kms_data_key_reuse_period_seconds", "KmsDataKeyReusePeriodSeconds")
    p.boolean("sqs_managed_sse_enabled", "SqsManagedSseEnabled")
    p.json("redrive_policy", "RedrivePolicy")
    p.json("redrive_allow_policy", "RedriveAllowPolicy")
    p.tags()


@resource("AWS::SQS::QueuePolicy", "aws_sqs_queue_policy", prepare=_prepare_targets("Queues"))
def sqs_queue_policy(b: Builder) -> None:
    policy = json_value(b.ctx, b.props.take("PolicyDocument", {}))
    _one_per_target(b, "Queues", "aws_sqs_queue_policy", "queue_url", [Attr("policy", policy)])


# --- SNS --------------------------------------------------------------------------------


@resource(
    "AWS::SNS::Topic",
    "aws_sns_topic",
    ref="arn",
    attrs={"TopicArn": "arn", "TopicName": "name"},
)
def sns_topic(b: Builder) -> None:
    p = b.props
    p.attr("name", "TopicName")
    p.attr("display_name", "DisplayName")
    p.boolean("fifo_topic", "FifoTopic")
    p.boolean("content_based_deduplication", "ContentBasedDeduplication")
    p.attr("fifo_throughput_scope", "FifoThroughputScope")
    p.attr("kms_master_key_id", "KmsMasterKeyId")
    p.number("signature_version", "SignatureVersion")
    p.attr("tracing_config", "TracingConfig")
    p.json("archive_policy", "ArchivePolicy")
    p.json("delivery_policy", "DeliveryPolicy")
    p.tags()
    subscriptions = p.take("Subscription")
    if subscriptions is None:
        return
    if not isinstance(subscriptions, list):
        b.ctx.note(
            f"{b.logical_id}: Subscription is computed by a function and was not converted; "
            "add aws_sns_topic_subscription resources by hand"
        )
        p.comment(f"Subscription was not converted: {snippet(subscriptions)}")
        return
    count = sum(not b.ctx.is_no_value(s) for s in subscriptions)
    for index, item in enumerate(subscriptions, 1):
        if b.ctx.is_no_value(item):
            continue
        where = f"{b.logical_id}.Subscription[{index - 1}]"
        when, subscription = _list_item(p, item, where, "aws_sns_topic_subscription")
        if subscription is None:
            continue
        inner = Props(b.ctx, subscription, where, p.owner)
        inner.items.append(Attr("topic_arn", b.self_ref("arn")))
        inner.attr("protocol", "Protocol")
        inner.attr("endpoint", "Endpoint")
        inner.finish()
        suffix = "_subscription" if count == 1 else f"_subscription_{index}"
        b.add_resource("aws_sns_topic_subscription", suffix, inner.items, when=when)


def _list_item(p: Props, item: Any, where: str, tf_type: str) -> tuple[str | None, Any]:
    """One entry of a list of structures that each become a resource: (condition, mapping).
    `!If [Cond, {...}, !Ref AWS::NoValue]` gives a counted resource; any other function is
    reported and kept as a comment, and the mapping is None."""
    when, value = p.ctx.optional_item(item)
    if isinstance(value, dict) and intrinsic_name(value) is None:
        return when, value
    p.ctx.note(
        f"{p.owner}: '{where}' is computed by a function and was not converted; add the "
        f"{tf_type} by hand"
    )
    p.comment(f"{where.split('.', 1)[-1]} was not converted: {snippet(item)}")
    return None, None


@resource("AWS::SNS::Subscription", "aws_sns_topic_subscription", ref="arn")
def sns_subscription(b: Builder) -> None:
    p = b.props
    p.attr("topic_arn", "TopicArn")
    p.attr("protocol", "Protocol")
    p.attr("endpoint", "Endpoint")
    p.json("filter_policy", "FilterPolicy")
    p.attr("filter_policy_scope", "FilterPolicyScope")
    p.boolean("raw_message_delivery", "RawMessageDelivery")
    p.json("redrive_policy", "RedrivePolicy")
    p.json("delivery_policy", "DeliveryPolicy")
    p.json("replay_policy", "ReplayPolicy")
    p.attr("subscription_role_arn", "SubscriptionRoleArn")
    p.attr("region", "Region")


@resource("AWS::SNS::TopicPolicy", "aws_sns_topic_policy", prepare=_prepare_targets("Topics"))
def sns_topic_policy(b: Builder) -> None:
    policy = json_value(b.ctx, b.props.take("PolicyDocument", {}))
    _one_per_target(b, "Topics", "aws_sns_topic_policy", "arn", [Attr("policy", policy)])


# --- EventBridge ------------------------------------------------------------------------


@resource("AWS::Events::Rule", "aws_cloudwatch_event_rule", ref="name", attrs={"Arn": "arn"})
def events_rule(b: Builder) -> None:
    p = b.props
    p.attr("name", "Name")
    p.attr("description", "Description")
    bus = p.peek("EventBusName")
    p.attr("event_bus_name", "EventBusName")
    p.json("event_pattern", "EventPattern")
    p.attr("schedule_expression", "ScheduleExpression")
    p.attr("state", "State")
    p.attr("role_arn", "RoleArn")
    p.tags()
    targets = p.take("Targets")
    if targets is None:
        return
    if not isinstance(targets, list):
        b.ctx.note(
            f"{b.logical_id}: Targets is computed by a function and was not converted; add "
            "aws_cloudwatch_event_target resources by hand"
        )
        p.comment(f"Targets was not converted: {snippet(targets)}")
        return
    suffixes = UniqueNames()
    for index, item in enumerate(targets, 1):
        if b.ctx.is_no_value(item):
            continue
        where = f"{b.logical_id}.Targets[{index - 1}]"
        when, target = _list_item(p, item, where, "aws_cloudwatch_event_target")
        if target is None:
            continue
        inner = Props(b.ctx, target, where, p.owner)
        inner.items.append(Attr("rule", b.self_ref("name")))
        if bus is not None and not b.ctx.is_no_value(bus):
            # A rule on a custom bus needs its targets on the same bus.
            inner.items.append(Attr("event_bus_name", b.ctx.expr(bus)))
        target_id = inner.peek("Id")
        inner.attr("target_id", "Id")
        inner.attr("arn", "Arn")
        _event_target(inner)
        inner.finish()
        label = target_id if isinstance(target_id, str) else f"target_{index}"
        suffix = suffixes.take("_" + identifier(label, f"target_{index}"))
        b.add_resource("aws_cloudwatch_event_target", suffix, inner.items, when=when)


def _event_target(t: Props) -> None:
    t.attr("role_arn", "RoleArn")
    t.attr("input", "Input")
    t.attr("input_path", "InputPath")
    t.block(
        "input_transformer",
        "InputTransformer",
        lambda i: (
            i.attr("input_paths", "InputPathsMap"),
            i.attr("input_template", "InputTemplate"),
        ),
    )
    t.block("dead_letter_config", "DeadLetterConfig", lambda d: d.attr("arn", "Arn"))
    t.block(
        "retry_policy",
        "RetryPolicy",
        lambda r: (
            r.number("maximum_event_age_in_seconds", "MaximumEventAgeInSeconds"),
            r.number("maximum_retry_attempts", "MaximumRetryAttempts"),
        ),
    )
    t.block("sqs_target", "SqsParameters", lambda s: s.attr("message_group_id", "MessageGroupId"))
    t.block(
        "kinesis_target",
        "KinesisParameters",
        lambda k: k.attr("partition_key_path", "PartitionKeyPath"),
    )
    t.block(
        "http_target",
        "HttpParameters",
        lambda h: h.attrs(
            {
                "header_parameters": "HeaderParameters",
                "path_parameter_values": "PathParameterValues",
                "query_string_parameters": "QueryStringParameters",
            }
        ),
    )
    t.block("batch_target", "BatchParameters", _batch_target)
    t.block("ecs_target", "EcsParameters", _ecs_target)


def _batch_target(r: Props) -> None:
    r.attr("job_definition", "JobDefinition")
    r.attr("job_name", "JobName")
    size = r.literal("ArrayProperties", dict)
    if size is not None and "Size" in size:
        r.set("array_size", r.ctx.expr(size["Size"]))
    retry = r.literal("RetryStrategy", dict)
    if retry is not None and "Attempts" in retry:
        r.set("job_attempts", r.ctx.expr(retry["Attempts"]))


def _ecs_target(r: Props) -> None:
    r.attr("task_definition_arn", "TaskDefinitionArn")
    r.number("task_count", "TaskCount")
    r.attr("launch_type", "LaunchType")
    r.attr("platform_version", "PlatformVersion")
    r.attr("group", "Group")
    r.boolean("enable_ecs_managed_tags", "EnableECSManagedTags")
    r.boolean("enable_execute_command", "EnableExecuteCommand")
    r.attr("propagate_tags", "PropagateTags")
    r.block("network_configuration", "NetworkConfiguration", _ecs_network)


def _ecs_network(n: Props) -> None:
    """NetworkConfiguration.AwsVpcConfiguration: flat in Terraform's network_configuration."""
    vpc = n.literal("AwsVpcConfiguration", dict)
    if vpc is None:
        return
    v = Props(n.ctx, vpc, f"{n.where}.AwsVpcConfiguration", n.owner)
    v.list_attr("subnets", "Subnets")
    v.list_attr("security_groups", "SecurityGroups")
    public = v.take("AssignPublicIp")
    if isinstance(public, str):
        v.set("assign_public_ip", public.upper() == "ENABLED")
    elif public is not None:
        v.set("assign_public_ip", Raw(f'{operand(n.ctx.expr(public))} == "ENABLED"'))
    v.finish()
    n.items.extend(v.items)


@resource(
    "AWS::Events::EventBus",
    "aws_cloudwatch_event_bus",
    ref="name",
    attrs={"Arn": "arn", "Name": "name"},
    import_id="Name",
)
def events_bus(b: Builder) -> None:
    p = b.props
    b.name_attr("name", "Name")
    p.attr("description", "Description")
    p.attr("event_source_name", "EventSourceName")
    p.attr("kms_key_identifier", "KmsKeyIdentifier")
    p.block("dead_letter_config", "DeadLetterConfig", lambda d: d.attr("arn", "Arn"))
    p.block(
        "log_config",
        "LogConfig",
        lambda c: c.attrs({"include_detail": "IncludeDetail", "level": "Level"}),
    )
    p.tags()
    policy = p.take("Policy")
    if policy is not None:
        b.add_resource(
            "aws_cloudwatch_event_bus_policy",
            "_policy",
            [Attr("event_bus_name", b.self_ref("name")), Attr("policy", json_value(b.ctx, policy))],
        )


# --- Kinesis ----------------------------------------------------------------------------


@resource(
    "AWS::Kinesis::Stream",
    "aws_kinesis_stream",
    ref="name",
    attrs={"Arn": "arn"},
    import_id="Name",
)
def kinesis_stream(b: Builder) -> None:
    p = b.props
    b.name_attr("name", "Name")
    p.number("shard_count", "ShardCount")
    p.number("retention_period", "RetentionPeriodHours")
    p.list_attr("shard_level_metrics", "DesiredShardLevelMetrics")
    p.number("max_record_size_in_kib", "MaxRecordSizeInKiB")
    encryption = p.take("StreamEncryption")
    if isinstance(encryption, dict):
        inner = Props(b.ctx, encryption, f"{b.logical_id}.StreamEncryption", p.owner)
        inner.attr("encryption_type", "EncryptionType")
        inner.attr("kms_key_id", "KeyId")
        inner.finish()
        p.items.extend(inner.items)
    p.block(
        "stream_mode_details",
        "StreamModeDetails",
        lambda m: m.attr("stream_mode", "StreamMode"),
    )
    p.tags()


# --- Step Functions ---------------------------------------------------------------------


def _as_text(value: Any, ctx: Context) -> Any:
    """A substitution value as CloudFormation pastes it into the definition: as text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if _number_param(ctx, value):
        return Expression(f"tostring({ctx.text(value)})")
    return value


def _number_param(ctx: Context, value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"Ref"}
        and ctx.parameter_types.get(str(value["Ref"])) == "Number"
    )


def _substitute(value: Any, substitutions: dict[str, Any], ctx: Context) -> Any:
    """Strings of a state machine definition with `${Name}` placeholders as Fn::Sub, so the
    substitution values (often references) become Terraform expressions. CloudFormation
    replaces text, so a string stays a string whatever the value's type."""
    if isinstance(value, dict):
        return {k: _substitute(v, substitutions, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, substitutions, ctx) for v in value]
    if isinstance(value, str) and _PLACEHOLDER.search(value):
        names = {m.strip() for m in _PLACEHOLDER.findall(value)}
        known = {
            name: _as_text(substitutions[name], ctx) for name in names if name in substitutions
        }
        # Placeholders that are not substitutions stay literal text.
        template = _PLACEHOLDER.sub(
            lambda m: m.group(0) if m.group(1).strip() in known else "${!" + m.group(1) + "}",
            value,
        )
        return {"Fn::Sub": [template, known]} if known else value
    return value


def sub_json(value: dict[str, Any]) -> Any:
    """`!Sub '{...json...}'` (a state machine definition, a dashboard body) parsed into a
    structure with Fn::Sub strings, so it becomes jsonencode({...}) instead of one long
    string. None if the text isn't JSON."""
    args = value["Fn::Sub"]
    text, variables = (
        (args[0], args[1]) if isinstance(args, list) and len(args) == 2 else (args, {})
    )
    if not isinstance(text, str) or not isinstance(variables, dict):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str) and _PLACEHOLDER.search(node):
            return {"Fn::Sub": [node, variables]} if variables else {"Fn::Sub": node}
        return node

    return walk(parsed)


def _definition(b: Builder) -> None:
    p = b.props
    substitutions = p.take("DefinitionSubstitutions")
    definition = p.take("Definition")
    text = p.take("DefinitionString")
    if definition is None and isinstance(text, str):
        try:
            definition = json.loads(text)
        except ValueError:
            definition = None
            p.set("definition", text)
    elif definition is None and isinstance(text, dict) and intrinsic_name(text) == "Fn::Sub":
        definition = sub_json(text)
        if definition is None:
            p.set("definition", b.ctx.expr(text))
    elif definition is None and text is not None:
        p.set("definition", b.ctx.expr(text))
    if definition is not None:
        if isinstance(substitutions, dict):
            definition = _substitute(definition, substitutions, b.ctx)
            substitutions = None
        p.set("definition", hcl.Call("jsonencode", b.ctx.expr(definition)))
    if substitutions is not None:
        b.ctx.note(
            f"{b.logical_id}: DefinitionSubstitutions could not be applied to this definition; "
            "replace the ${...} placeholders by hand"
        )
        p.comment(f"DefinitionSubstitutions was not converted: {snippet(substitutions)}")


def _log_destination(log: Props) -> None:
    destinations = log.take("Destinations")
    if not isinstance(destinations, list) or not destinations:
        return
    group = (
        destinations[0].get("CloudWatchLogsLogGroup", {})
        if isinstance(destinations[0], dict)
        else {}
    )
    arn = group.get("LogGroupArn") if isinstance(group, dict) else None
    if arn is None:
        return
    value = log.ctx.expr(arn)
    text = hcl.inline(value)
    # Step Functions wants the log group ARN with its ':*' suffix.
    if not text.endswith(':*"'):
        if isinstance(value, str):
            value = value + ":*"
        else:
            value = Raw('"${' + text + '}:*"')
    log.set("log_destination", value)


@resource(
    "AWS::StepFunctions::StateMachine",
    "aws_sfn_state_machine",
    ref="arn",
    attrs={"Arn": "arn", "Name": "name"},
)
def step_functions_state_machine(b: Builder) -> None:
    p = b.props
    p.attr("name", "StateMachineName")
    p.attr("type", "StateMachineType")
    p.attr("role_arn", "RoleArn")
    _definition(b)
    p.block(
        "logging_configuration",
        "LoggingConfiguration",
        lambda log: (
            _log_destination(log),
            log.boolean("include_execution_data", "IncludeExecutionData"),
            log.attr("level", "Level"),
        ),
    )
    p.block(
        "tracing_configuration", "TracingConfiguration", lambda t: t.boolean("enabled", "Enabled")
    )
    p.block(
        "encryption_configuration",
        "EncryptionConfiguration",
        lambda e: (
            e.attr("kms_key_id", "KmsKeyId"),
            e.number("kms_data_key_reuse_period_seconds", "KmsDataKeyReusePeriodSeconds"),
            e.attr("type", "Type"),
        ),
    )
    p.tags()
