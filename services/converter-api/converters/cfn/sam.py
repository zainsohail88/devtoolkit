"""AWS SAM templates expanded into plain CloudFormation, the way the SAM transform does it.

Each AWS::Serverless::* resource becomes the resources SAM itself would create, with the
logical IDs SAM uses (MyFunctionRole, ServerlessRestApi, MyFunctionMyEventPermission...), so
`!GetAtt MyFunctionRole.Arn` and `!Ref ServerlessRestApi` in the template keep working.
Local code paths (CodeUri: ./src) are passed to the Lambda converter as hints, so it can zip
them with an archive_file data source.

The work is split by concern: `Expansion` holds the state of one run, `IamWriter` writes
roles and Lambda permissions, `RuleWriter` writes EventBridge rules and schedules, and one
expander class per kind of serverless resource builds on those. `SamExpander` runs them in
the order SAM does.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from typing import Any

from converters.cfn.constants import SAM_TRANSFORM
from converters.cfn.errors import CfnError
from converters.cfn.hcl import template_text
from converters.cfn.models import Expression
from converters.cfn.sam_constants import (
    BASIC_EXECUTION,
    CONNECTOR_ACTIONS,
    FUNCTION_PROPERTIES,
    GLOBAL_SECTIONS,
    INHERITED,
    PARTITION_ARN,
    POLLER_PROPERTIES,
    POLLERS,
    PRINCIPALS,
    REF_IS_ARN,
    ROLE_PROPERTY,
    S3_URI,
    SERVERLESS_TYPES,
    SUB_REFERENCE,
)
from converters.cfn.sam_policies import (
    PolicyTemplates,
    document,
    inline_policy,
    managed_policy,
    statement,
    sub,
)

# --- value helpers ----------------------------------------------------------------------


def is_function(value: dict[str, Any]) -> bool:
    """Whether a mapping is an intrinsic function call (Ref, Fn::*)."""
    return len(value) == 1 and str(next(iter(value))).startswith(("Fn::", "Ref"))


def merge_globals(global_props: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    """The Globals rules of SAM: the value of the resource wins, maps merge, lists add up."""
    merged = copy.deepcopy(global_props)
    for name, value in props.items():
        current = merged.get(name)
        if (
            isinstance(current, dict)
            and isinstance(value, dict)
            and not is_function(current)
            and not is_function(value)
        ):
            merged[name] = merge_globals(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged[name] = current + value
        else:
            merged[name] = value
    return merged


def tag_list(tags: Any) -> Any:
    """SAM writes tags as a map; most CloudFormation types take [{Key, Value}]."""
    if isinstance(tags, dict) and not is_function(tags):
        return [{"Key": k, "Value": v} for k, v in tags.items()]
    return tags


def local_path(value: str) -> str:
    """A local code path without the leading ./ and the trailing slash."""
    return value.strip().removeprefix("./").rstrip("/") or "."


def file_body(uri: str) -> Expression:
    """A definition file next to the Terraform files, read with yamldecode (which reads
    JSON as well, so this works for either kind of definition file)."""
    path = template_text(local_path(uri))
    return Expression(f'yamldecode(file("${{path.module}}/{path}"))')


def s3_location(uri: Any) -> Any:
    """An s3://bucket/key URI as {Bucket, Key}; any other value as it is."""
    match = S3_URI.match(uri) if isinstance(uri, str) else None
    if match:
        return {"Bucket": match.group(1), "Key": match.group(2)}
    return uri


def alnum(text: str) -> str:
    """`text` with only its letters and digits."""
    return re.sub(r"[^A-Za-z0-9]", "", text)


def as_list(value: Any) -> list[Any]:
    """None as [], a list as it is and any other value as a list of one."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def unique(values: list[Any]) -> list[Any]:
    """Values without repeats, in order. Keys are JSON text, so it stays linear."""
    seen: set[str] = set()
    out: list[Any] = []
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def ref_id(value: Any) -> str | None:
    """The logical id in {Id: X}, {Ref: X} or a plain X."""
    if isinstance(value, dict):
        for key in ("Id", "Ref"):
            if isinstance(value.get(key), str):
                return value[key]
    return value if isinstance(value, str) else None


def meta(resource: dict[str, Any]) -> dict[str, Any]:
    """The DependsOn and Metadata attributes of a resource."""
    return {k: resource[k] for k in ("DependsOn", "Metadata") if k in resource}


def substitute(definition: Any, substitutions: dict[str, Any]) -> Any:
    """SAM DefinitionSubstitutions applied to a definition object: `${Name}` in a string
    becomes the value, through Fn::Sub when the value is computed."""
    if isinstance(definition, dict):
        return {k: substitute(v, substitutions) for k, v in definition.items()}
    if isinstance(definition, list):
        return [substitute(v, substitutions) for v in definition]
    if isinstance(definition, str) and "${" in definition:
        used = {k: v for k, v in substitutions.items() if "${" + k + "}" in definition}
        if not used:
            return definition
        if all(isinstance(v, str | int | float) for v in used.values()):
            for name, value in used.items():
                definition = definition.replace("${" + name + "}", str(value))
            return definition
        return {"Fn::Sub": [definition, used]}
    return definition


class ReferenceRewriter:
    """Points `!Ref MyFunction.Alias`, `!Ref MyApi.Stage` and the like (in Ref and Fn::Sub)
    at the generated resources they stand for."""

    def __init__(self, aliases: dict[str, str]) -> None:
        """`aliases`: "MyApi.Stage" -> the logical id of the generated stage."""
        self.aliases = aliases

    def replace(self, match: re.Match[str]) -> str:
        """One `${MyApi.Stage}` of a Fn::Sub string, rewritten when it is an alias."""
        name = match.group(1)
        if name in self.aliases:
            return "${" + self.aliases[name] + "}"
        return match.group(0)

    def rewrite_sub(self, text: str) -> str:
        """`${MyApi.Stage}` in a Fn::Sub string means the stage resource, as with Ref."""
        return SUB_REFERENCE.sub(self.replace, text)

    def rewrite(self, value: Any) -> Any:
        """`value` with every alias reference rewritten."""
        if isinstance(value, dict):
            if set(value) == {"Ref"} and value["Ref"] in self.aliases:
                return {"Ref": self.aliases[value["Ref"]]}
            if set(value) == {"Fn::Sub"}:
                args = value["Fn::Sub"]
                if isinstance(args, str):
                    return {"Fn::Sub": self.rewrite_sub(args)}
                if isinstance(args, list) and args and isinstance(args[0], str):
                    rest = [self.rewrite(item) for item in args[1:]]
                    return {"Fn::Sub": [self.rewrite_sub(args[0])] + rest}
            return {k: self.rewrite(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.rewrite(v) for v in value]
        return value


# --- shared state and writers -----------------------------------------------------------


class Expansion:
    """The state of one SAM expansion: the source resources, the generated ones, notes,
    converter hints, the generated API paths and the aliases of generated resources."""

    def __init__(self, template: dict[str, Any], notes: list[str]) -> None:
        """Start from `template` (a copy the expansion may change) and the notes list."""
        self.template = template
        self.notes = notes
        self.source: dict[str, dict[str, Any]] = template["Resources"]
        self.out: dict[str, dict[str, Any]] = {}
        self.hints: dict[str, dict[str, Any]] = {}
        # API id -> {path: {method: operation}}, for the APIs whose body SAM writes.
        self.rest_paths: dict[str, dict[str, Any]] = {}
        self.http_paths: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        self.globals = template.get("Globals") or {}
        if not isinstance(self.globals, dict):
            raise CfnError("Invalid SAM template: 'Globals' must be a mapping")

    def note(self, message: str) -> None:
        """Add a note once."""
        if message not in self.notes:
            self.notes.append(message)

    def leftovers(self, logical_id: str, props: dict[str, Any]) -> None:
        """A note for each SAM property nothing used."""
        for name in props:
            self.note(f"{logical_id}: SAM property '{name}' was not converted")

    def add(
        self,
        logical_id: str,
        kind: str,
        props: dict[str, Any],
        origin: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Add a generated resource. It inherits the Condition and deletion policies of
        `origin` (the SAM resource); `extra` holds more resource attributes."""
        resource: dict[str, Any] = {"Type": kind, "Properties": props}
        for attribute in INHERITED:
            if attribute in origin:
                resource[attribute] = origin[attribute]
        resource.update(extra or {})
        self.out[logical_id] = resource

    def props(self, resource: dict[str, Any]) -> dict[str, Any]:
        """The properties of a SAM resource with its Globals section merged in."""
        kind = resource["Type"]
        props = dict(resource.get("Properties") or {})
        for section, section_kind in GLOBAL_SECTIONS.items():
            if kind == section_kind and isinstance(self.globals.get(section), dict):
                return merge_globals(self.globals[section], props)
        return props


class IamWriter:
    """The IAM roles and Lambda permissions SAM creates for functions, state machines,
    event sources and connectors."""

    def __init__(self, expansion: Expansion) -> None:
        """Write into `expansion`."""
        self.exp = expansion
        self.templates = PolicyTemplates()

    def policy_template(
        self, owner: str, role_id: str, name: str, params: Any
    ) -> list[dict[str, Any]]:
        """The statements of one SAM policy template, or a note when it is unknown."""
        template = self.templates.get(name)
        if template is None:
            self.exp.note(
                f"{owner}: SAM policy template '{name}' was not converted; add its "
                f"permissions to {role_id} by hand"
            )
            return []
        return template(params if isinstance(params, dict) else {})

    def role(
        self,
        role_id: str,
        service: str,
        managed: list[Any],
        policies: Any,
        inline: list[dict[str, Any]],
        origin: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> None:
        """The role SAM creates for a function, state machine or event source. `policies`
        is the Policies property of SAM: managed policy names, documents or policy
        templates."""
        owner = role_id.removesuffix("Role")
        statements: list[dict[str, Any]] = []
        for index, policy in enumerate(as_list(policies)):
            if isinstance(policy, str):
                managed.append(managed_policy(policy))
            elif isinstance(policy, dict) and "Statement" in policy:
                inline.append(
                    inline_policy(f"{role_id}Policy{index}", as_list(policy["Statement"]))
                )
            elif isinstance(policy, dict) and is_function(policy):
                managed.append(policy)
            elif isinstance(policy, dict) and len(policy) == 1:
                ((name, params),) = policy.items()
                statements.extend(self.policy_template(owner, role_id, name, params))
            else:
                self.exp.note(f"{owner}: a Policies entry was not converted; add it to {role_id}")
        if statements:
            inline.append(inline_policy(f"{role_id}PolicyTemplates", statements))
        role: dict[str, Any] = {
            "AssumeRolePolicyDocument": document(
                [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": [service]},
                        "Action": ["sts:AssumeRole"],
                    }
                ]
            )
        }
        role.update(extra or {})
        if managed:
            role["ManagedPolicyArns"] = unique(managed)
        if inline:
            role["Policies"] = inline
        self.exp.add(role_id, "AWS::IAM::Role", role, origin)

    def permission(
        self,
        logical_id: str,
        function: Any,
        principal: str,
        source_arn: Any,
        origin: dict[str, Any],
        account: bool = False,
    ) -> None:
        """A Lambda permission for `principal` to invoke `function`."""
        props: dict[str, Any] = {
            "Action": "lambda:InvokeFunction",
            "FunctionName": function,
            "Principal": principal,
        }
        if source_arn is not None:
            props["SourceArn"] = source_arn
        if account:
            props["SourceAccount"] = {"Ref": "AWS::AccountId"}
        self.exp.add(logical_id, "AWS::Lambda::Permission", props, origin)


class RuleWriter:
    """The EventBridge rules and EventBridge Scheduler schedules behind the Schedule,
    EventBridgeRule and ScheduleV2 events of functions and state machines."""

    def __init__(self, expansion: Expansion, iam: IamWriter) -> None:
        """Write into `expansion`, with roles from `iam`."""
        self.exp = expansion
        self.iam = iam

    def rule(
        self,
        event_id: str,
        kind: str,
        props: dict[str, Any],
        target: dict[str, Any],
        origin: dict[str, Any],
    ) -> None:
        """An AWS::Events::Rule for a Schedule, CloudWatchEvent or EventBridgeRule event."""
        rule: dict[str, Any] = {}
        if kind == "Schedule":
            rule["ScheduleExpression"] = props.get("Schedule")
        else:
            rule["EventPattern"] = props.get("Pattern")
            if "EventBusName" in props:
                rule["EventBusName"] = props["EventBusName"]
        for source in ("Name", "RuleName"):
            if source in props:
                rule["Name"] = props[source]
        for name in ("Description", "State"):
            if name in props:
                rule[name] = props[name]
        if "Enabled" in props and "State" not in rule:
            rule["State"] = "ENABLED" if str(props["Enabled"]).lower() == "true" else "DISABLED"
        for name in ("Input", "InputPath"):
            if name in props:
                target[name] = props[name]
        rule["Targets"] = [target]
        self.exp.add(event_id, "AWS::Events::Rule", rule, origin)

    def schedule_v2(
        self, event_id: str, props: dict[str, Any], target: Any, action: str, origin: dict[str, Any]
    ) -> None:
        """An AWS::Scheduler::Schedule and the role it runs `action` on `target` with."""
        role_id = f"{event_id}Role"
        schedule: dict[str, Any] = {
            "ScheduleExpression": props.get("ScheduleExpression"),
            "FlexibleTimeWindow": props.get("FlexibleTimeWindow") or {"Mode": "OFF"},
            "Target": {"Arn": target, "RoleArn": {"Fn::GetAtt": [role_id, "Arn"]}},
        }
        for name in (
            "Name",
            "Description",
            "State",
            "ScheduleExpressionTimezone",
            "GroupName",
            "StartDate",
            "EndDate",
        ):
            if name in props:
                schedule[name] = props[name]
        if "Input" in props:
            schedule["Target"]["Input"] = props["Input"]
        self.exp.add(event_id, "AWS::Scheduler::Schedule", schedule, origin)
        self.iam.role(
            role_id,
            "scheduler.amazonaws.com",
            [],
            None,
            [inline_policy(f"{event_id}Policy", [statement([action], target)])],
            origin,
        )


# --- functions --------------------------------------------------------------------------


class FunctionExpander:
    """AWS::Serverless::Function: the function, its role, alias, URL and event sources."""

    def __init__(self, expansion: Expansion, iam: IamWriter, rules: RuleWriter) -> None:
        """Write into `expansion` with the shared writers."""
        self.exp = expansion
        self.iam = iam
        self.rules = rules

    def expand(self, logical_id: str, resource: dict[str, Any]) -> None:
        """Expand one function."""
        p = self.exp.props(resource)
        managed: list[Any] = [managed_policy(BASIC_EXECUTION)]
        inline: list[dict[str, Any]] = []
        fn: dict[str, Any] = {name: p.pop(name) for name in FUNCTION_PROPERTIES if name in p}
        if "VpcConfig" in fn:
            # SAM gives functions in a VPC the permissions to manage their network interfaces.
            managed.append(managed_policy("service-role/AWSLambdaVPCAccessExecutionRole"))
        self.code(logical_id, p, fn)

        tracing = p.pop("Tracing", None)
        if tracing is not None:
            fn["TracingConfig"] = {"Mode": tracing}
            if tracing == "Active":
                managed.append(managed_policy("AWSXrayWriteOnlyAccess"))
        dlq = p.pop("DeadLetterQueue", None)
        if isinstance(dlq, dict):
            fn["DeadLetterConfig"] = {"TargetArn": dlq.get("TargetArn")}
            action = "sns:Publish" if dlq.get("Type") == "SNS" else "sqs:SendMessage"
            inline.append(
                inline_policy(
                    f"{logical_id}RoleDeadLetterQueuePolicy",
                    [statement([action], dlq.get("TargetArn"))],
                )
            )
        if "Tags" in p:
            fn["Tags"] = tag_list(p.pop("Tags"))

        role = p.pop("Role", None)
        policies = p.pop("Policies", None)
        role_extra = {
            name: p.pop(source)
            for source, name in (
                ("PermissionsBoundary", "PermissionsBoundary"),
                ("RolePath", "Path"),
            )
            if source in p
        }
        assume = p.pop("AssumeRolePolicyDocument", None)
        events = p.pop("Events", None) or {}
        alias = p.pop("AutoPublishAlias", None)
        url = p.pop("FunctionUrlConfig", None)

        if role is None:
            fn["Role"] = {"Fn::GetAtt": [f"{logical_id}Role", "Arn"]}
        else:
            fn["Role"] = role
            if policies:
                self.exp.note(
                    f"{logical_id} sets both Role and Policies: SAM ignores Policies then, so "
                    "they were not converted"
                )
        self.exp.add(logical_id, "AWS::Lambda::Function", fn, resource, meta(resource))

        target: Any = {"Fn::GetAtt": [logical_id, "Arn"]}
        if alias is not None:
            target = self.alias(logical_id, alias, resource)
        if not isinstance(events, dict):
            raise CfnError(f"Invalid SAM template: 'Events' of '{logical_id}' must be a mapping")
        for event_name, event in events.items():
            if not isinstance(event, dict):
                raise CfnError(
                    f"Invalid SAM template: event '{event_name}' of '{logical_id}' must be a "
                    "mapping"
                )
            managed.extend(self.event(f"{logical_id}{event_name}", event, target, resource))
        if isinstance(url, dict):
            self.url(
                logical_id, url, target if alias is not None else {"Ref": logical_id}, resource
            )

        if role is None:
            if assume is not None:
                role_extra["AssumeRolePolicyDocument"] = assume
            self.iam.role(
                f"{logical_id}Role",
                "lambda.amazonaws.com",
                managed,
                policies,
                inline,
                resource,
                role_extra,
            )
        for name in ("DeploymentPreference", "ProvisionedConcurrencyConfig", "EventInvokeConfig"):
            if p.pop(name, None) is not None:
                self.exp.note(
                    f"{logical_id}: the SAM setting {name} was not converted; set it up by hand"
                )
        p.pop("Metadata", None)
        self.exp.leftovers(logical_id, p)

    def code(self, logical_id: str, p: dict[str, Any], fn: dict[str, Any]) -> None:
        """The Code of the function, or a code_path hint for a local folder."""
        image = p.pop("ImageUri", None)
        inline = p.pop("InlineCode", None)
        code = p.pop("CodeUri", None)
        match = S3_URI.match(code) if isinstance(code, str) else None
        if image is not None:
            fn["Code"] = {"ImageUri": image}
            fn["PackageType"] = "Image"
        elif inline is not None:
            fn["Code"] = {"ZipFile": inline}
        elif isinstance(code, dict) and "Bucket" in code:
            fn["Code"] = {"S3Bucket": code["Bucket"], "S3Key": code.get("Key")}
            if "Version" in code:
                fn["Code"]["S3ObjectVersion"] = code["Version"]
        elif match:
            fn["Code"] = {"S3Bucket": match.group(1), "S3Key": match.group(2)}
        elif isinstance(code, str):
            self.exp.hints.setdefault(logical_id, {})["code_path"] = local_path(code)
        elif code is not None:
            self.exp.note(f"{logical_id}: CodeUri is computed by a function and was not converted")

    def alias(self, logical_id: str, alias: Any, origin: dict[str, Any]) -> dict[str, Any]:
        """The version and alias of AutoPublishAlias. Returns a Ref to the alias."""
        version_id = f"{logical_id}Version"
        alias_id = f"{logical_id}Alias{alnum(alias) if isinstance(alias, str) else ''}"
        self.exp.add(
            version_id, "AWS::Lambda::Version", {"FunctionName": {"Ref": logical_id}}, origin
        )
        self.exp.add(
            alias_id,
            "AWS::Lambda::Alias",
            {
                "FunctionName": {"Ref": logical_id},
                "FunctionVersion": {"Fn::GetAtt": [version_id, "Version"]},
                "Name": alias,
            },
            origin,
        )
        self.exp.aliases[f"{logical_id}.Alias"] = alias_id
        self.exp.aliases[f"{logical_id}.Version"] = version_id
        return {"Ref": alias_id}

    def url(
        self, logical_id: str, url: dict[str, Any], target: Any, origin: dict[str, Any]
    ) -> None:
        """FunctionUrlConfig: the function URL, and a public permission for AuthType NONE."""
        props: dict[str, Any] = {
            "TargetFunctionArn": target,
            "AuthType": url.get("AuthType", "AWS_IAM"),
        }
        for name in ("Cors", "InvokeMode"):
            if name in url:
                props[name] = url[name]
        self.exp.add(f"{logical_id}Url", "AWS::Lambda::Url", props, origin)
        if props["AuthType"] == "NONE":
            self.exp.add(
                f"{logical_id}UrlPublicPermissions",
                "AWS::Lambda::Permission",
                {
                    "Action": "lambda:InvokeFunctionUrl",
                    "FunctionName": target,
                    "Principal": "*",
                    "FunctionUrlAuthType": "NONE",
                },
                origin,
            )

    def event(
        self, event_id: str, event: dict[str, Any], target: Any, origin: dict[str, Any]
    ) -> list[Any]:
        """One entry of the Events of a function. Returns managed policies the role needs."""
        kind = event.get("Type")
        props = event.get("Properties") or {}
        if kind in POLLERS:
            return self.poller(event_id, kind, props, target, origin)
        if kind in ("Schedule", "CloudWatchEvent", "EventBridgeRule"):
            self.rules.rule(
                event_id, kind, props, {"Arn": target, "Id": f"{event_id}LambdaTarget"}, origin
            )
            self.iam.permission(
                f"{event_id}Permission",
                target,
                "events.amazonaws.com",
                {"Fn::GetAtt": [event_id, "Arn"]},
                origin,
            )
        elif kind == "ScheduleV2":
            self.rules.schedule_v2(event_id, props, target, "lambda:InvokeFunction", origin)
        elif kind == "SNS":
            self.sns_event(event_id, props, target, origin)
        elif kind == "S3":
            self.s3_event(event_id, props, target, origin)
        elif kind == "CloudWatchLogs":
            self.iam.permission(f"{event_id}Permission", target, "logs.amazonaws.com", None, origin)
            self.exp.add(
                event_id,
                "AWS::Logs::SubscriptionFilter",
                {
                    "DestinationArn": target,
                    "FilterPattern": props.get("FilterPattern", ""),
                    "LogGroupName": props.get("LogGroupName"),
                },
                origin,
                {"DependsOn": [f"{event_id}Permission"]},
            )
        elif kind == "Api":
            self.api_event(event_id, props, target, origin, http=False)
        elif kind == "HttpApi":
            self.api_event(event_id, props, target, origin, http=True)
        else:
            self.exp.note(
                f"{event_id}: SAM event type '{kind}' was not converted; connect the event "
                "source by hand"
            )
        return []

    def poller(
        self,
        event_id: str,
        kind: str,
        props: dict[str, Any],
        target: Any,
        origin: dict[str, Any],
    ) -> list[Any]:
        """An SQS, Kinesis or DynamoDB event: an event source mapping. Returns the managed
        policy SAM attaches for it."""
        source_key, policy = POLLERS[kind]
        mapping: dict[str, Any] = {
            "EventSourceArn": props.get(source_key),
            "FunctionName": target,
        }
        mapping.update({k: props[k] for k in POLLER_PROPERTIES if k in props})
        self.exp.add(event_id, "AWS::Lambda::EventSourceMapping", mapping, origin)
        return [managed_policy(policy)]

    def sns_event(
        self, event_id: str, props: dict[str, Any], target: Any, origin: dict[str, Any]
    ) -> None:
        """An SNS event: a subscription of the function and the permission it needs."""
        subscription: dict[str, Any] = {
            "Protocol": "lambda",
            "Endpoint": target,
            "TopicArn": props.get("Topic"),
        }
        for name in ("FilterPolicy", "FilterPolicyScope", "Region", "RedrivePolicy"):
            if name in props:
                subscription[name] = props[name]
        if props.get("SqsSubscription"):
            self.exp.note(
                f"{event_id}: the SqsSubscription of the SNS event (a queue between the topic "
                "and the function) was not converted"
            )
        self.exp.add(event_id, "AWS::SNS::Subscription", subscription, origin)
        self.iam.permission(
            f"{event_id}Permission", target, "sns.amazonaws.com", props.get("Topic"), origin
        )

    def s3_event(
        self, event_id: str, props: dict[str, Any], target: Any, origin: dict[str, Any]
    ) -> None:
        """S3 notifications live on the bucket, so the bucket in this template gets them."""
        self.iam.permission(
            f"{event_id}Permission", target, "s3.amazonaws.com", None, origin, account=True
        )
        bucket_id = ref_id(props.get("Bucket")) if isinstance(props.get("Bucket"), dict) else None
        bucket = self.exp.source.get(bucket_id or "")
        if bucket is None or bucket.get("Type") != "AWS::S3::Bucket":
            self.exp.note(
                f"{event_id}: the bucket of the S3 event is outside this template; add the "
                "notification to that bucket by hand"
            )
            return
        config: dict[str, Any] = {"Function": target}
        if "Filter" in props:
            config["Filter"] = props["Filter"]
        notification = bucket.setdefault("Properties", {}).setdefault(
            "NotificationConfiguration", {}
        )
        configs = notification.setdefault("LambdaConfigurations", [])
        configs.extend(dict(config, Event=name) for name in as_list(props.get("Events")))
        bucket["DependsOn"] = as_list(bucket.get("DependsOn")) + [f"{event_id}Permission"]

    def api_event(
        self, event_id: str, props: dict[str, Any], target: Any, origin: dict[str, Any], http: bool
    ) -> None:
        """An Api or HttpApi event: an operation in the API body SAM writes, and the
        permission for API Gateway to call the function."""
        default_api = "ServerlessHttpApi" if http else "ServerlessRestApi"
        api_id = ref_id(props.get("ApiId" if http else "RestApiId")) or default_api
        paths = self.exp.http_paths if http else self.exp.rest_paths
        path = str(props.get("Path", "$default" if http else "/"))
        method = str(props.get("Method", "any")).lower()
        if api_id == default_api or api_id in paths:
            integration: dict[str, Any] = {"type": "aws_proxy", "httpMethod": "POST"}
            if http:
                integration["uri"] = target
                integration["payloadFormatVersion"] = str(props.get("PayloadFormatVersion", "2.0"))
                if "TimeoutInMillis" in props:
                    integration["timeoutInMillis"] = props["TimeoutInMillis"]
            else:
                integration["uri"] = sub(
                    f"{PARTITION_ARN}:apigateway:${{AWS::Region}}:lambda:path/2015-03-31/"
                    "functions/${fn}/invocations",
                    {"fn": target},
                )
            operation = "x-amazon-apigateway-any-method" if method == "any" else method
            paths.setdefault(api_id, {}).setdefault(path, {})[operation] = {
                "x-amazon-apigateway-integration": integration,
                "responses": {},
            }
        if props.get("Auth"):
            self.exp.note(
                f"{event_id}: the Auth setting of the API event was not converted; add the "
                "authorizer"
            )
        if http and path == "$default":
            scope = "*"
        else:
            source_path = re.sub(r"\{[^}]*\}", "*", path)
            scope = f"*/{'*' if method == 'any' else method.upper()}{source_path}"
        self.iam.permission(
            f"{event_id}Permission",
            target,
            "apigateway.amazonaws.com",
            sub(
                f"{PARTITION_ARN}:execute-api:${{AWS::Region}}:${{AWS::AccountId}}:${{api}}/{scope}",
                {"api": {"Ref": api_id}},
            ),
            origin,
        )


# --- APIs -------------------------------------------------------------------------------


class ApiExpander:
    """AWS::Serverless::Api and AWS::Serverless::HttpApi, and the implicit APIs SAM creates
    for function events: the API, its deployment and its stage."""

    def __init__(self, expansion: Expansion) -> None:
        """Write into `expansion`."""
        self.exp = expansion

    def body(self, p: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
        """Body (inline or from a local file) or BodyS3Location; `generated` when the
        template gives neither."""
        body = p.pop("DefinitionBody", None)
        uri = p.pop("DefinitionUri", None)
        if body is not None:
            return {"Body": body}
        if isinstance(uri, str) and not uri.startswith("s3://"):
            return {"Body": file_body(uri)}
        if uri is not None:
            return {"BodyS3Location": s3_location(uri)}
        return {"Body": generated}

    def rest_api(self, logical_id: str, resource: dict[str, Any] | None) -> None:
        """AWS::Serverless::Api (or the implicit ServerlessRestApi): RestApi, Deployment, Stage."""
        origin = resource or {}
        p = self.exp.props(resource or {"Type": "AWS::Serverless::Api"})
        api: dict[str, Any] = {"Name": p.pop("Name", {"Ref": "AWS::StackName"})}
        generated = {
            "swagger": "2.0",
            "info": {"version": "1.0", "title": api["Name"]},
            "paths": self.exp.rest_paths.get(logical_id, {}),
        }
        api.update(self.body(p, generated))
        for name in (
            "Description",
            "BinaryMediaTypes",
            "MinimumCompressionSize",
            "DisableExecuteApiEndpoint",
            "Mode",
            "ApiKeySourceType",
        ):
            if name in p:
                api[name] = p.pop(name)
        endpoint = p.pop("EndpointConfiguration", None)
        if endpoint is not None:
            api["EndpointConfiguration"] = (
                {"Types": [endpoint]} if isinstance(endpoint, str) else endpoint
            )
        if "Tags" in p:
            api["Tags"] = tag_list(p.pop("Tags"))
        self.exp.add(logical_id, "AWS::ApiGateway::RestApi", api, origin, meta(origin))

        stage_name = p.pop("StageName", "Prod")
        deployment_id = f"{logical_id}Deployment"
        self.exp.add(
            deployment_id,
            "AWS::ApiGateway::Deployment",
            {"RestApiId": {"Ref": logical_id}, "Description": f"Deployment of {logical_id}"},
            origin,
        )
        stage: dict[str, Any] = {
            "RestApiId": {"Ref": logical_id},
            "DeploymentId": {"Ref": deployment_id},
            "StageName": stage_name,
        }
        for name in (
            "Variables",
            "TracingEnabled",
            "CacheClusterEnabled",
            "CacheClusterSize",
            "MethodSettings",
            "AccessLogSetting",
        ):
            if name in p:
                stage[name] = p.pop(name)
        stage_id = f"{logical_id}{alnum(str(stage_name))}Stage"
        self.exp.add(stage_id, "AWS::ApiGateway::Stage", stage, origin)
        self.exp.aliases[f"{logical_id}.Stage"] = stage_id
        self.exp.aliases[f"{logical_id}.Deployment"] = deployment_id
        for name in (
            "Auth",
            "Cors",
            "Domain",
            "GatewayResponses",
            "Models",
            "OpenApiVersion",
            "CanarySetting",
            "AlwaysDeploy",
            "MergeDefinitions",
        ):
            if p.pop(name, None) is not None:
                self.exp.note(
                    f"{logical_id}: SAM API setting '{name}' was not converted; add it to the "
                    "API definition by hand"
                )
        self.exp.leftovers(logical_id, p)

    def http_api(self, logical_id: str, resource: dict[str, Any] | None) -> None:
        """AWS::Serverless::HttpApi (or the implicit ServerlessHttpApi): Api and Stage."""
        origin = resource or {}
        p = self.exp.props(resource or {"Type": "AWS::Serverless::HttpApi"})
        api: dict[str, Any] = {
            "Name": p.pop("Name", {"Ref": "AWS::StackName"}),
            "ProtocolType": "HTTP",
        }
        generated = {
            "openapi": "3.0.1",
            "info": {"version": "1.0", "title": api["Name"]},
            "paths": self.exp.http_paths.get(logical_id, {}),
        }
        api.update(self.body(p, generated))
        cors = p.pop("CorsConfiguration", None)
        if isinstance(cors, dict):
            api["CorsConfiguration"] = cors
        elif cors is not None:
            self.exp.note(
                f"{logical_id}: CorsConfiguration given as a single value was not converted"
            )
        for name in ("Description", "DisableExecuteApiEndpoint", "FailOnWarnings"):
            if name in p:
                api[name] = p.pop(name)
        if "Tags" in p:
            api["Tags"] = p.pop("Tags")
        self.exp.add(logical_id, "AWS::ApiGatewayV2::Api", api, origin, meta(origin))

        stage_name = p.pop("StageName", "$default")
        stage: dict[str, Any] = {
            "ApiId": {"Ref": logical_id},
            "StageName": stage_name,
            "AutoDeploy": True,
        }
        for name in (
            "StageVariables",
            "AccessLogSettings",
            "DefaultRouteSettings",
            "RouteSettings",
        ):
            if name in p:
                stage[name] = p.pop(name)
        if stage_name == "$default":
            stage_id = f"{logical_id}ApiGatewayDefaultStage"
        else:
            stage_id = f"{logical_id}{alnum(str(stage_name))}Stage"
        self.exp.add(stage_id, "AWS::ApiGatewayV2::Stage", stage, origin)
        self.exp.aliases[f"{logical_id}.Stage"] = stage_id
        for name in ("Auth", "Domain"):
            if p.pop(name, None) is not None:
                self.exp.note(
                    f"{logical_id}: SAM HTTP API setting '{name}' was not converted; set it up "
                    "by hand"
                )
        self.exp.leftovers(logical_id, p)


# --- tables, layers and state machines --------------------------------------------------


class StorageExpander:
    """AWS::Serverless::SimpleTable and AWS::Serverless::LayerVersion."""

    def __init__(self, expansion: Expansion) -> None:
        """Write into `expansion`."""
        self.exp = expansion

    def simple_table(self, logical_id: str, resource: dict[str, Any]) -> None:
        """A SimpleTable as a DynamoDB table with one hash key."""
        p = self.exp.props(resource)
        key = p.pop("PrimaryKey", None) or {}
        name = key.get("Name", "id")
        kind = {"String": "S", "Number": "N", "Binary": "B"}.get(
            str(key.get("Type", "String")), "S"
        )
        table: dict[str, Any] = {
            "AttributeDefinitions": [{"AttributeName": name, "AttributeType": kind}],
            "KeySchema": [{"AttributeName": name, "KeyType": "HASH"}],
        }
        if "ProvisionedThroughput" in p:
            table["ProvisionedThroughput"] = p.pop("ProvisionedThroughput")
        else:
            table["BillingMode"] = "PAY_PER_REQUEST"
        for prop in ("TableName", "SSESpecification", "PointInTimeRecoverySpecification"):
            if prop in p:
                table[prop] = p.pop(prop)
        if "Tags" in p:
            table["Tags"] = tag_list(p.pop("Tags"))
        self.exp.add(logical_id, "AWS::DynamoDB::Table", table, resource, meta(resource))
        self.exp.leftovers(logical_id, p)

    def layer(self, logical_id: str, resource: dict[str, Any]) -> None:
        """A LayerVersion, with its content in S3 or a code_path hint for a local folder."""
        p = self.exp.props(resource)
        layer: dict[str, Any] = {
            name: p.pop(name)
            for name in (
                "LayerName",
                "Description",
                "CompatibleRuntimes",
                "CompatibleArchitectures",
                "LicenseInfo",
            )
            if name in p
        }
        content = p.pop("ContentUri", None)
        match = S3_URI.match(content) if isinstance(content, str) else None
        if isinstance(content, dict) and "Bucket" in content:
            layer["Content"] = {"S3Bucket": content["Bucket"], "S3Key": content.get("Key")}
            if "Version" in content:
                layer["Content"]["S3ObjectVersion"] = content["Version"]
        elif match:
            layer["Content"] = {"S3Bucket": match.group(1), "S3Key": match.group(2)}
        elif isinstance(content, str):
            self.exp.hints.setdefault(logical_id, {})["code_path"] = local_path(content)
        extra = {"DeletionPolicy": "Retain"} if p.pop("RetentionPolicy", None) == "Retain" else {}
        self.exp.add(logical_id, "AWS::Lambda::LayerVersion", layer, resource, extra)
        self.exp.leftovers(logical_id, p)


class StateMachineExpander:
    """AWS::Serverless::StateMachine: the state machine, its role and its event sources."""

    def __init__(self, expansion: Expansion, iam: IamWriter, rules: RuleWriter) -> None:
        """Write into `expansion` with the shared writers."""
        self.exp = expansion
        self.iam = iam
        self.rules = rules

    def expand(self, logical_id: str, resource: dict[str, Any]) -> None:
        """Expand one state machine."""
        p = self.exp.props(resource)
        machine: dict[str, Any] = {}
        definition = p.pop("Definition", None)
        uri = p.pop("DefinitionUri", None)
        substitutions = p.pop("DefinitionSubstitutions", None)
        if definition is not None:
            machine["Definition"] = substitute(definition, substitutions or {})
        elif isinstance(uri, str) and not uri.startswith("s3://"):
            machine["Definition"] = file_body(uri)
            if substitutions:
                self.exp.note(
                    f"{logical_id}: DefinitionSubstitutions for a definition file were not "
                    "applied; use templatefile() in place of file()"
                )
        elif uri is not None:
            machine["DefinitionS3Location"] = s3_location(uri)
            if substitutions:
                machine["DefinitionSubstitutions"] = substitutions
        for source, dest in (
            ("Name", "StateMachineName"),
            ("Type", "StateMachineType"),
            ("Logging", "LoggingConfiguration"),
            ("Tracing", "TracingConfiguration"),
        ):
            if source in p:
                machine[dest] = p.pop(source)
        if "Tags" in p:
            machine["Tags"] = tag_list(p.pop("Tags"))
        role = p.pop("Role", None)
        policies = p.pop("Policies", None)
        extra = (
            {"PermissionsBoundary": p.pop("PermissionsBoundary")}
            if "PermissionsBoundary" in p
            else {}
        )
        machine["RoleArn"] = (
            role if role is not None else {"Fn::GetAtt": [f"{logical_id}Role", "Arn"]}
        )
        self.exp.add(
            logical_id, "AWS::StepFunctions::StateMachine", machine, resource, meta(resource)
        )
        if role is None:
            self.iam.role(
                f"{logical_id}Role", "states.amazonaws.com", [], policies, [], resource, extra
            )
        events = p.pop("Events", None) or {}
        for event_name, event in events.items() if isinstance(events, dict) else []:
            self.event(logical_id, f"{logical_id}{event_name}", event or {}, resource)
        self.exp.leftovers(logical_id, p)

    def event(
        self, logical_id: str, event_id: str, event: dict[str, Any], resource: dict[str, Any]
    ) -> None:
        """One entry of the Events of a state machine: a rule or schedule that starts it."""
        kind = event.get("Type")
        props = event.get("Properties") or {}
        start = [statement(["states:StartExecution"], {"Ref": logical_id})]
        if kind in ("Schedule", "EventBridgeRule", "CloudWatchEvent"):
            role_id = f"{event_id}Role"
            target = {
                "Arn": {"Ref": logical_id},
                "Id": f"{event_id}StepFunctionsTarget",
                "RoleArn": {"Fn::GetAtt": [role_id, "Arn"]},
            }
            self.rules.rule(event_id, kind, props, target, resource)
            self.iam.role(
                role_id,
                "events.amazonaws.com",
                [],
                None,
                [inline_policy(f"{event_id}StartExecution", start)],
                resource,
            )
        elif kind == "ScheduleV2":
            self.rules.schedule_v2(
                event_id, props, {"Ref": logical_id}, "states:StartExecution", resource
            )
        else:
            self.exp.note(f"{event_id}: SAM state machine event type '{kind}' was not converted")


# --- connectors -------------------------------------------------------------------------


class ConnectorExpander:
    """AWS::Serverless::Connector (and the Connectors attribute of a resource): IAM
    permissions, or a Lambda permission, between two resources of this template."""

    def __init__(self, expansion: Expansion, iam: IamWriter) -> None:
        """Write into `expansion`, with Lambda permissions from `iam`."""
        self.exp = expansion
        self.iam = iam

    def expand(self, logical_id: str, props: dict[str, Any], source_id: str | None) -> None:
        """One connector. `source_id` is the resource that embeds it, if any."""
        source_id = source_id or ref_id(props.get("Source"))
        permissions = as_list(props.get("Permissions"))
        destinations = as_list(props.get("Destination"))
        for index, destination in enumerate(destinations):
            destination_id = ref_id(destination)
            # SAM names each destination policy separately when there are several.
            name = f"{logical_id}PolicyDestination{index}" if len(destinations) > 1 else None
            if source_id not in self.exp.source or destination_id not in self.exp.source:
                self.exp.note(
                    f"{logical_id}: a connector end is outside this template; the connector "
                    "was not converted"
                )
                continue
            self.connect(logical_id, source_id, destination_id or "", permissions, name)

    def plain_type(self, logical_id: str) -> str:
        """The CloudFormation type a source resource has once SAM expanded it."""
        kind = self.exp.source[logical_id]["Type"]
        return SERVERLESS_TYPES.get(kind, kind)

    def connect(
        self,
        logical_id: str,
        source_id: str,
        destination_id: str,
        permissions: list[str],
        policy_name: str | None = None,
    ) -> None:
        """Grant `permissions` (Read, Write) from the source to one destination."""
        source_type = self.plain_type(source_id)
        destination_type = self.plain_type(destination_id)
        if destination_type == "AWS::Lambda::Function" and source_type in PRINCIPALS:
            self.lambda_permission(logical_id, source_id, source_type, destination_id)
            return
        actions_by_permission = CONNECTOR_ACTIONS.get(destination_type)
        source = self.exp.out.get(source_id) or {}
        role_value = (source.get("Properties") or {}).get(ROLE_PROPERTY.get(source_type, ""))
        role_id = role_value.get("Fn::GetAtt", [None])[0] if isinstance(role_value, dict) else None
        role = self.exp.out.get(role_id) if isinstance(role_id, str) else None
        actions = [a for p in permissions for a in (actions_by_permission or {}).get(str(p), [])]
        if role is None or not actions:
            self.exp.note(
                f"{logical_id}: connector from {source_type} to {destination_type} was not "
                "converted; grant the access by hand"
            )
            return
        if destination_type in REF_IS_ARN:
            resources: list[Any] = [{"Ref": destination_id}]
        else:
            resources = [{"Fn::GetAtt": [destination_id, "Arn"]}]
        if destination_type == "AWS::DynamoDB::Table":
            resources.append(
                sub("${arn}/index/*", {"arn": {"Fn::GetAtt": [destination_id, "Arn"]}})
            )
        elif destination_type == "AWS::S3::Bucket":
            resources.append(sub("${arn}/*", {"arn": {"Fn::GetAtt": [destination_id, "Arn"]}}))
        role["Properties"].setdefault("Policies", []).append(
            inline_policy(policy_name or f"{logical_id}Policy", [statement(actions, resources)])
        )

    def lambda_permission(
        self, logical_id: str, source_id: str, source_type: str, destination_id: str
    ) -> None:
        """A source that calls a function through a resource-based permission."""
        source_arn: Any = {"Fn::GetAtt": [source_id, "Arn"]}
        if source_type in ("AWS::ApiGateway::RestApi", "AWS::ApiGatewayV2::Api"):
            source_arn = sub(
                f"{PARTITION_ARN}:execute-api:${{AWS::Region}}:${{AWS::AccountId}}:${{api}}/*",
                {"api": {"Ref": source_id}},
            )
        elif source_type == "AWS::SNS::Topic":
            source_arn = {"Ref": source_id}
        self.iam.permission(
            f"{logical_id}WriteLambdaPermission",
            {"Fn::GetAtt": [destination_id, "Arn"]},
            PRINCIPALS[source_type],
            source_arn,
            {},
        )


# --- the whole expansion ----------------------------------------------------------------


class SamExpander:
    """Expands every AWS::Serverless::* resource of one template, in the order SAM does:
    functions and other resources first, then the APIs (whose bodies collect the events of
    every function), then connectors (which add to the generated roles)."""

    def __init__(self, template: dict[str, Any], notes: list[str]) -> None:
        """`template` is changed in place; pass a copy."""
        self.exp = Expansion(template, notes)
        iam = IamWriter(self.exp)
        rules = RuleWriter(self.exp, iam)
        self.functions = FunctionExpander(self.exp, iam, rules)
        self.apis = ApiExpander(self.exp)
        self.storage = StorageExpander(self.exp)
        self.state_machines = StateMachineExpander(self.exp, iam, rules)
        self.connectors = ConnectorExpander(self.exp, iam)
        self.handlers: dict[str, Callable[[str, dict[str, Any]], None]] = {
            "AWS::Serverless::Function": self.functions.expand,
            "AWS::Serverless::SimpleTable": self.storage.simple_table,
            "AWS::Serverless::LayerVersion": self.storage.layer,
            "AWS::Serverless::StateMachine": self.state_machines.expand,
        }

    def collect_generated_apis(self) -> None:
        """Mark the APIs whose body SAM writes from function events."""
        for logical_id, resource in self.exp.source.items():
            props = resource.get("Properties") or {}
            if "DefinitionBody" in props or "DefinitionUri" in props:
                continue
            if resource["Type"] == "AWS::Serverless::Api":
                self.exp.rest_paths[logical_id] = {}
            elif resource["Type"] == "AWS::Serverless::HttpApi":
                self.exp.http_paths[logical_id] = {}

    def expand_resources(
        self, connectors: list[tuple[str, dict[str, Any], str | None]], apis: list[str]
    ) -> None:
        """Expand every resource but APIs and connectors, which are collected into `apis`
        and `connectors` for later."""
        for logical_id, resource in self.exp.source.items():
            kind = resource["Type"]
            embedded = resource.pop("Connectors", None)
            if isinstance(embedded, dict):
                for name, connector in embedded.items():
                    connectors.append(
                        (
                            f"{logical_id}{name}",
                            (connector or {}).get("Properties") or {},
                            logical_id,
                        )
                    )
            if kind in self.handlers:
                self.handlers[kind](logical_id, resource)
            elif kind in ("AWS::Serverless::Api", "AWS::Serverless::HttpApi"):
                # API bodies collect every event of every function, so APIs are built last.
                # The placeholder keeps the resource order.
                apis.append(logical_id)
                self.exp.out[logical_id] = resource
            elif kind == "AWS::Serverless::Connector":
                connectors.append((logical_id, resource.get("Properties") or {}, None))
            elif kind.startswith("AWS::Serverless::"):
                self.exp.note(
                    f"{logical_id} ({kind}) was not converted: Terraform has no equivalent; "
                    "convert it by hand"
                )
            else:
                self.exp.out[logical_id] = resource

    def expand_apis(self, apis: list[str]) -> None:
        """The declared APIs, then the implicit ones function events asked for."""
        for logical_id in apis:
            resource = self.exp.out[logical_id]
            if resource["Type"] == "AWS::Serverless::Api":
                self.apis.rest_api(logical_id, resource)
            else:
                self.apis.http_api(logical_id, resource)
        if "ServerlessRestApi" in self.exp.rest_paths and "ServerlessRestApi" not in self.exp.out:
            self.apis.rest_api("ServerlessRestApi", None)
        if "ServerlessHttpApi" in self.exp.http_paths and "ServerlessHttpApi" not in self.exp.out:
            self.apis.http_api("ServerlessHttpApi", None)

    def run(self) -> dict[str, Any]:
        """Expand the template. Returns the plain CloudFormation template."""
        self.collect_generated_apis()
        connectors: list[tuple[str, dict[str, Any], str | None]] = []
        apis: list[str] = []
        self.expand_resources(connectors, apis)
        self.expand_apis(apis)
        for logical_id, props, source in connectors:
            self.connectors.expand(logical_id, props, source)

        original = self.exp.template
        template = {k: v for k, v in original.items() if k not in ("Globals", "Transform")}
        others = [t for t in as_list(original.get("Transform")) if t != SAM_TRANSFORM]
        if others:
            template["Transform"] = others
        rewriter = ReferenceRewriter(self.exp.aliases)
        template["Resources"] = rewriter.rewrite(self.exp.out)
        if "Outputs" in template:
            template["Outputs"] = rewriter.rewrite(template["Outputs"])
        return template


def expand_sam(
    template: dict[str, Any], notes: list[str]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Expand every AWS::Serverless::* resource. Returns (template, converter hints)."""
    expander = SamExpander(copy.deepcopy(template), notes)
    return expander.run(), expander.exp.hints
