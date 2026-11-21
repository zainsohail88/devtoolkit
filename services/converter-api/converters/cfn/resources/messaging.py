"""SQS, SNS, EventBridge, Kinesis and Step Functions."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props, snippet
from converters.cfn.context import Context
from converters.cfn.expressions import NO_VALUE, SUB_VARIABLE, intrinsic_name, wrap_operand
from converters.cfn.models import Attr, Call, Expression, Raw, ResourceInfo
from converters.cfn.naming import UniqueNames, identifier
from converters.cfn.resources.base import ResourceConverter

# --- helpers shared with other modules --------------------------------------------------


def operand(value: Any) -> str:
    """An HCL value as an operand of `==`: in parentheses unless it is a plain reference or
    string (`a ? b : c == d` would compare c alone)."""
    return wrap_operand(hcl.inline(value))


def optional(value: Any) -> bool:
    """Whether a list item is `!If [Cond, x, !Ref AWS::NoValue]` (either way round): an item
    that only exists when a condition holds. Usable before conversion (no Context)."""
    if not (isinstance(value, dict) and intrinsic_name(value) == "Fn::If"):
        return False
    args = value["Fn::If"]
    return isinstance(args, list) and len(args) == 3 and (NO_VALUE in args[1:])


def main_index(values: list[Any]) -> int:
    """Which of several targets gets the main resource: the first that is always there."""
    return next((i for i, value in enumerate(values) if not optional(value)), 0)


def sub_json(value: dict[str, Any]) -> Any:
    """`!Sub '{...json...}'` (a state machine definition, a dashboard body) parsed into a
    structure with Fn::Sub strings, so it becomes jsonencode({...}) instead of one long
    string. None if the text is no JSON."""
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
    return _sub_strings(parsed, variables)


def _sub_strings(node: Any, variables: dict[str, Any]) -> Any:
    """A parsed JSON document with every string that has placeholders as a Fn::Sub."""
    if isinstance(node, dict):
        return {k: _sub_strings(v, variables) for k, v in node.items()}
    if isinstance(node, list):
        return [_sub_strings(v, variables) for v in node]
    if isinstance(node, str) and SUB_VARIABLE.search(node):
        return {"Fn::Sub": [node, variables]} if variables else {"Fn::Sub": node}
    return node


# --- helpers of this module -------------------------------------------------------------


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
    main = main_index(targets)
    suffixes = UniqueNames()
    for index, item in enumerate(targets, 1):
        when, target = b.ctx.optional_item(item)
        if index - 1 == main:
            b.props.items.insert(0, Attr(argument, b.ctx.expr(target)))
            b.props.items.extend(items)
            b.main_when = when
            continue
        label = str(target["Ref"]) if isinstance(target, dict) and "Ref" in target else ""
        suffix = suffixes.take("_" + identifier(label or f"target_{index}", f"target_{index}"))
        body = [Attr(argument, b.ctx.expr(target))] + items
        b.add_resource(tf_type, suffix, body, when=when)


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


def _dead_letter_config(d: Props) -> None:
    """A dead_letter_config block: the queue ARN."""
    d.attr("arn", "Arn")


# --- SQS --------------------------------------------------------------------------------


class SqsQueue(ResourceConverter):
    """AWS::SQS::Queue as aws_sqs_queue."""

    cfn_type = "AWS::SQS::Queue"
    tf_type = "aws_sqs_queue"
    ref = "url"
    attrs = {"Arn": "arn", "QueueName": "name", "QueueUrl": "url"}

    def convert(self, b: Builder) -> None:
        """The queue settings, with the JSON policies as jsonencode()."""
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


class TargetPolicy(ResourceConverter):
    """A resource policy that CloudFormation applies to a list of targets: one Terraform
    policy per target (see _one_per_target)."""

    # The CloudFormation property listing the targets, and the Terraform argument naming one.
    targets_property: ClassVar[str] = ""
    target_argument: ClassVar[str] = ""

    def prepare(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """References to a policy whose every target is optional need `[0]`."""
        targets = (resource.get("Properties") or {}).get(self.targets_property)
        if isinstance(targets, list):
            targets = [target for target in targets if target != NO_VALUE]
            if targets and optional(targets[main_index(targets)]):
                info.conditional = True

    def convert(self, b: Builder) -> None:
        """The policy document, once per target."""
        policy = b.ctx.json(b.props.take("PolicyDocument", {}))
        _one_per_target(
            b,
            self.targets_property,
            self.tf_type,
            self.target_argument,
            [Attr("policy", policy)],
        )


class SqsQueuePolicy(TargetPolicy):
    """AWS::SQS::QueuePolicy as aws_sqs_queue_policy, one per queue."""

    cfn_type = "AWS::SQS::QueuePolicy"
    tf_type = "aws_sqs_queue_policy"
    targets_property = "Queues"
    target_argument = "queue_url"


# --- SNS --------------------------------------------------------------------------------


class SnsTopic(ResourceConverter):
    """AWS::SNS::Topic as aws_sns_topic, with its subscriptions as separate resources."""

    cfn_type = "AWS::SNS::Topic"
    tf_type = "aws_sns_topic"
    ref = "arn"
    attrs = {"TopicArn": "arn", "TopicName": "name"}

    def convert(self, b: Builder) -> None:
        """The topic settings, then one aws_sns_topic_subscription per subscription."""
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
        self.subscriptions(b)

    def subscriptions(self, b: Builder) -> None:
        """The Subscription list: one aws_sns_topic_subscription each."""
        p = b.props
        subscriptions = p.take("Subscription")
        if subscriptions is None:
            return
        if not isinstance(subscriptions, list):
            b.ctx.note(
                f"{b.logical_id}: Subscription is computed by a function and was not "
                "converted; add aws_sns_topic_subscription resources by hand"
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


class SnsSubscription(ResourceConverter):
    """AWS::SNS::Subscription as aws_sns_topic_subscription."""

    cfn_type = "AWS::SNS::Subscription"
    tf_type = "aws_sns_topic_subscription"
    ref = "arn"

    def convert(self, b: Builder) -> None:
        """The subscription settings, with the JSON policies as jsonencode()."""
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


class SnsTopicPolicy(TargetPolicy):
    """AWS::SNS::TopicPolicy as aws_sns_topic_policy, one per topic."""

    cfn_type = "AWS::SNS::TopicPolicy"
    tf_type = "aws_sns_topic_policy"
    targets_property = "Topics"
    target_argument = "arn"


# --- EventBridge ------------------------------------------------------------------------


class EventsRule(ResourceConverter):
    """AWS::Events::Rule as aws_cloudwatch_event_rule, with its targets as separate
    aws_cloudwatch_event_target resources."""

    cfn_type = "AWS::Events::Rule"
    tf_type = "aws_cloudwatch_event_rule"
    ref = "name"
    attrs = {"Arn": "arn"}

    def convert(self, b: Builder) -> None:
        """The rule settings, then one aws_cloudwatch_event_target per target."""
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
        self.targets(b, bus)

    def targets(self, b: Builder, bus: Any) -> None:
        """The Targets list. A rule on a custom bus (`bus`) needs its targets on that bus."""
        p = b.props
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
                inner.items.append(Attr("event_bus_name", b.ctx.expr(bus)))
            target_id = inner.peek("Id")
            inner.attr("target_id", "Id")
            inner.attr("arn", "Arn")
            self.fill_target(inner)
            inner.finish()
            label = target_id if isinstance(target_id, str) else f"target_{index}"
            suffix = suffixes.take("_" + identifier(label, f"target_{index}"))
            b.add_resource("aws_cloudwatch_event_target", suffix, inner.items, when=when)

    def fill_target(self, t: Props) -> None:
        """The settings of one target besides its ID and ARN."""
        t.attr("role_arn", "RoleArn")
        t.attr("input", "Input")
        t.attr("input_path", "InputPath")
        t.block("input_transformer", "InputTransformer", self.fill_input_transformer)
        t.block("dead_letter_config", "DeadLetterConfig", _dead_letter_config)
        t.block("retry_policy", "RetryPolicy", self.fill_retry_policy)
        t.block("sqs_target", "SqsParameters", self.fill_sqs_target)
        t.block("kinesis_target", "KinesisParameters", self.fill_kinesis_target)
        t.block("http_target", "HttpParameters", self.fill_http_target)
        t.block("batch_target", "BatchParameters", self.fill_batch_target)
        t.block("ecs_target", "EcsParameters", self.fill_ecs_target)

    def fill_input_transformer(self, i: Props) -> None:
        """The input_transformer block."""
        i.attr("input_paths", "InputPathsMap")
        i.attr("input_template", "InputTemplate")

    def fill_retry_policy(self, r: Props) -> None:
        """The retry_policy block."""
        r.number("maximum_event_age_in_seconds", "MaximumEventAgeInSeconds")
        r.number("maximum_retry_attempts", "MaximumRetryAttempts")

    def fill_sqs_target(self, s: Props) -> None:
        """The sqs_target block."""
        s.attr("message_group_id", "MessageGroupId")

    def fill_kinesis_target(self, k: Props) -> None:
        """The kinesis_target block."""
        k.attr("partition_key_path", "PartitionKeyPath")

    def fill_http_target(self, h: Props) -> None:
        """The http_target block."""
        h.attrs(
            {
                "header_parameters": "HeaderParameters",
                "path_parameter_values": "PathParameterValues",
                "query_string_parameters": "QueryStringParameters",
            }
        )

    def fill_batch_target(self, r: Props) -> None:
        """The batch_target block: the array size and attempts are flat in Terraform."""
        r.attr("job_definition", "JobDefinition")
        r.attr("job_name", "JobName")
        size = r.literal("ArrayProperties", dict)
        if size is not None and "Size" in size:
            r.set("array_size", r.ctx.expr(size["Size"]))
        retry = r.literal("RetryStrategy", dict)
        if retry is not None and "Attempts" in retry:
            r.set("job_attempts", r.ctx.expr(retry["Attempts"]))

    def fill_ecs_target(self, r: Props) -> None:
        """The ecs_target block."""
        r.attr("task_definition_arn", "TaskDefinitionArn")
        r.number("task_count", "TaskCount")
        r.attr("launch_type", "LaunchType")
        r.attr("platform_version", "PlatformVersion")
        r.attr("group", "Group")
        r.boolean("enable_ecs_managed_tags", "EnableECSManagedTags")
        r.boolean("enable_execute_command", "EnableExecuteCommand")
        r.attr("propagate_tags", "PropagateTags")
        r.block("network_configuration", "NetworkConfiguration", self.fill_ecs_network)

    def fill_ecs_network(self, n: Props) -> None:
        """NetworkConfiguration.AwsVpcConfiguration: flat in the network_configuration of
        Terraform."""
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


class EventsEventBus(ResourceConverter):
    """AWS::Events::EventBus as aws_cloudwatch_event_bus, with its policy as a separate
    aws_cloudwatch_event_bus_policy."""

    cfn_type = "AWS::Events::EventBus"
    tf_type = "aws_cloudwatch_event_bus"
    ref = "name"
    attrs = {"Arn": "arn", "Name": "name"}
    import_id = "Name"

    def convert(self, b: Builder) -> None:
        """The bus settings and its policy."""
        p = b.props
        b.name_attr("name", "Name")
        p.attr("description", "Description")
        p.attr("event_source_name", "EventSourceName")
        p.attr("kms_key_identifier", "KmsKeyIdentifier")
        p.block("dead_letter_config", "DeadLetterConfig", _dead_letter_config)
        p.block("log_config", "LogConfig", self.fill_log_config)
        p.tags()
        policy = p.take("Policy")
        if policy is not None:
            b.add_resource(
                "aws_cloudwatch_event_bus_policy",
                "_policy",
                [Attr("event_bus_name", b.self_ref("name")), Attr("policy", b.ctx.json(policy))],
            )

    def fill_log_config(self, c: Props) -> None:
        """The log_config block."""
        c.attrs({"include_detail": "IncludeDetail", "level": "Level"})


# --- Kinesis ----------------------------------------------------------------------------


class KinesisStream(ResourceConverter):
    """AWS::Kinesis::Stream as aws_kinesis_stream."""

    cfn_type = "AWS::Kinesis::Stream"
    tf_type = "aws_kinesis_stream"
    ref = "name"
    attrs = {"Arn": "arn"}
    import_id = "Name"

    def convert(self, b: Builder) -> None:
        """The stream settings; StreamEncryption is flat in Terraform."""
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
        p.block("stream_mode_details", "StreamModeDetails", self.fill_stream_mode)
        p.tags()

    def fill_stream_mode(self, m: Props) -> None:
        """The stream_mode_details block."""
        m.attr("stream_mode", "StreamMode")


# --- Step Functions ---------------------------------------------------------------------


def _as_text(value: Any, ctx: Context) -> Any:
    """A substitution value as CloudFormation pastes it into the definition: as text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if ctx.is_number_parameter(value):
        return Expression(f"tostring({ctx.text(value)})")
    return value


def _escape_unknown(text: str, known: dict[str, Any]) -> str:
    """`text` with every placeholder that is no substitution escaped (`${!Name}`), so it
    stays literal text."""
    pieces: list[str] = []
    last = 0
    for match in SUB_VARIABLE.finditer(text):
        pieces.append(text[last : match.start()])
        name = match.group(1)
        pieces.append(match.group(0) if name.strip() in known else "${!" + name + "}")
        last = match.end()
    pieces.append(text[last:])
    return "".join(pieces)


def _substitute(value: Any, substitutions: dict[str, Any], ctx: Context) -> Any:
    """Strings of a state machine definition with `${Name}` placeholders as Fn::Sub, so the
    substitution values (often references) become Terraform expressions. CloudFormation
    replaces text, so a string stays a string whatever the type of the value."""
    if isinstance(value, dict):
        return {k: _substitute(v, substitutions, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, substitutions, ctx) for v in value]
    if isinstance(value, str) and SUB_VARIABLE.search(value):
        names = {m.strip() for m in SUB_VARIABLE.findall(value)}
        known = {
            name: _as_text(substitutions[name], ctx) for name in names if name in substitutions
        }
        return {"Fn::Sub": [_escape_unknown(value, known), known]} if known else value
    return value


class StepFunctionsStateMachine(ResourceConverter):
    """AWS::StepFunctions::StateMachine as aws_sfn_state_machine."""

    cfn_type = "AWS::StepFunctions::StateMachine"
    tf_type = "aws_sfn_state_machine"
    ref = "arn"
    attrs = {"Arn": "arn", "Name": "name"}

    def convert(self, b: Builder) -> None:
        """The state machine settings and its definition."""
        p = b.props
        p.attr("name", "StateMachineName")
        p.attr("type", "StateMachineType")
        p.attr("role_arn", "RoleArn")
        self.definition(b)
        p.block("logging_configuration", "LoggingConfiguration", self.fill_logging)
        p.block("tracing_configuration", "TracingConfiguration", self.fill_tracing)
        p.block("encryption_configuration", "EncryptionConfiguration", self.fill_encryption)
        p.tags()

    def definition(self, b: Builder) -> None:
        """Definition, DefinitionString and DefinitionSubstitutions as one definition: a
        jsonencode() of the structure where it can be read, the text otherwise."""
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
            p.set("definition", Call("jsonencode", b.ctx.expr(definition)))
        if substitutions is not None:
            b.ctx.note(
                f"{b.logical_id}: DefinitionSubstitutions could not be applied to this "
                "definition; replace the ${...} placeholders by hand"
            )
            p.comment(f"DefinitionSubstitutions was not converted: {snippet(substitutions)}")

    def fill_logging(self, log: Props) -> None:
        """The logging_configuration block."""
        self.log_destination(log)
        log.boolean("include_execution_data", "IncludeExecutionData")
        log.attr("level", "Level")

    def log_destination(self, log: Props) -> None:
        """The first log group of Destinations as log_destination."""
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

    def fill_tracing(self, t: Props) -> None:
        """The tracing_configuration block."""
        t.boolean("enabled", "Enabled")

    def fill_encryption(self, e: Props) -> None:
        """The encryption_configuration block."""
        e.attr("kms_key_id", "KmsKeyId")
        e.number("kms_data_key_reuse_period_seconds", "KmsDataKeyReusePeriodSeconds")
        e.attr("type", "Type")


CONVERTERS: tuple[type[ResourceConverter], ...] = (
    SqsQueue,
    SqsQueuePolicy,
    SnsTopic,
    SnsSubscription,
    SnsTopicPolicy,
    EventsRule,
    EventsEventBus,
    KinesisStream,
    StepFunctionsStateMachine,
)
