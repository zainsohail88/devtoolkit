"""AWS SAM templates expanded into plain CloudFormation, the way the SAM transform does it.

Each AWS::Serverless::* resource becomes the resources SAM itself would create, with the
logical IDs SAM uses (MyFunctionRole, ServerlessRestApi, MyFunctionMyEventPermission...), so
`!GetAtt MyFunctionRole.Arn` and `!Ref ServerlessRestApi` in the template keep working.
Local code paths (CodeUri: ./src) are passed to the Lambda converter as hints, so it can zip
them with an archive_file data source.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from typing import Any

from converters.cfn.context import Expression
from converters.cfn.hcl import template_text
from converters.cfn.parse import SAM_TRANSFORM, CfnError

_PARTITION_ARN = "arn:${AWS::Partition}"
_BASIC_EXECUTION = "service-role/AWSLambdaBasicExecutionRole"
_GLOBAL_SECTIONS = {
    "Function": "AWS::Serverless::Function",
    "Api": "AWS::Serverless::Api",
    "HttpApi": "AWS::Serverless::HttpApi",
    "SimpleTable": "AWS::Serverless::SimpleTable",
    "StateMachine": "AWS::Serverless::StateMachine",
    "LayerVersion": "AWS::Serverless::LayerVersion",
}
# Resource attributes every generated resource inherits from the SAM resource.
_INHERITED = ("Condition", "DeletionPolicy", "UpdateReplacePolicy")
_S3_URI = re.compile(r"^s3://([^/]+)/(.+)$")
_SUB_REFERENCE = re.compile(r"\$\{([A-Za-z0-9]+\.[A-Za-z]+)\}")
_FUNCTION_PROPERTIES = (
    "FunctionName",
    "Description",
    "Handler",
    "Runtime",
    "MemorySize",
    "Timeout",
    "Environment",
    "VpcConfig",
    "Layers",
    "Architectures",
    "EphemeralStorage",
    "KmsKeyArn",
    "ReservedConcurrentExecutions",
    "LoggingConfig",
    "SnapStart",
    "ImageConfig",
    "FileSystemConfigs",
    "PackageType",
    "RuntimeManagementConfig",
    "CodeSigningConfigArn",
    "RecursiveLoop",
)


def _sub(text: str, **variables: Any) -> dict[str, Any]:
    if variables:
        return {"Fn::Sub": [text, variables]}
    return {"Fn::Sub": text}


def _arn(service: str, resource: str, **variables: Any) -> dict[str, Any]:
    """An ARN in this partition, region and account."""
    return _sub(
        f"{_PARTITION_ARN}:{service}:${{AWS::Region}}:${{AWS::AccountId}}:{resource}", **variables
    )


def _managed_policy(name: str) -> Any:
    if name.startswith("arn:"):
        return name
    return _sub(f"{_PARTITION_ARN}:iam::aws:policy/{name}")


def _statement(actions: list[str], resources: Any) -> dict[str, Any]:
    return {
        "Effect": "Allow",
        "Action": actions,
        "Resource": resources if isinstance(resources, list) else [resources],
    }


def _document(statements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": statements}


def _table(params: dict[str, Any], suffix: str = "") -> Any:
    return _arn("dynamodb", "table/${tableName}" + suffix, tableName=params.get("TableName"))


def _bucket(params: dict[str, Any], suffix: str = "") -> Any:
    return _sub(
        f"{_PARTITION_ARN}:s3:::${{bucketName}}{suffix}", bucketName=params.get("BucketName")
    )


def _named(service: str, resource: str, param: str) -> Callable[[dict[str, Any]], Any]:
    """An ARN builder for `resource` with one `${name}` taken from a template parameter."""
    return lambda p: _arn(service, resource, name=p.get(param))


_DYNAMODB_CRUD = [
    "dynamodb:GetItem",
    "dynamodb:DeleteItem",
    "dynamodb:PutItem",
    "dynamodb:Scan",
    "dynamodb:Query",
    "dynamodb:UpdateItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:BatchGetItem",
    "dynamodb:DescribeTable",
    "dynamodb:ConditionCheckItem",
]
_DYNAMODB_READ = [
    "dynamodb:GetItem",
    "dynamodb:Scan",
    "dynamodb:Query",
    "dynamodb:BatchGetItem",
    "dynamodb:DescribeTable",
]
_DYNAMODB_WRITE = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:BatchWriteItem"]
_S3_READ = [
    "s3:GetObject",
    "s3:ListBucket",
    "s3:GetBucketLocation",
    "s3:GetObjectVersion",
    "s3:GetLifecycleConfiguration",
]
_S3_WRITE = ["s3:PutObject", "s3:PutObjectAcl", "s3:PutLifecycleConfiguration"]
_SQS_POLL = [
    "sqs:ChangeMessageVisibility",
    "sqs:ChangeMessageVisibilityBatch",
    "sqs:DeleteMessage",
    "sqs:DeleteMessageBatch",
    "sqs:GetQueueAttributes",
    "sqs:ReceiveMessage",
]
_SSM_READ = ["ssm:GetParameters", "ssm:GetParameter", "ssm:GetParametersByPath"]
_ENI = [
    "ec2:CreateNetworkInterface",
    "ec2:DeleteNetworkInterface",
    "ec2:DescribeNetworkInterfaces",
    "ec2:DetachNetworkInterface",
]


def _single(actions: list[str], arn: Callable[[dict[str, Any]], Any]) -> Callable[..., Any]:
    return lambda p: [_statement(actions, arn(p))]


# The commonly used SAM policy templates, as IAM statements. Each takes the template's
# parameters ({"TableName": ...}) and returns a list of statements.
POLICY_TEMPLATES: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] = {
    "DynamoDBCrudPolicy": lambda p: [
        _statement(_DYNAMODB_CRUD, [_table(p), _table(p, "/index/*")])
    ],
    "DynamoDBReadPolicy": lambda p: [
        _statement(_DYNAMODB_READ, [_table(p), _table(p, "/index/*")])
    ],
    "DynamoDBWritePolicy": lambda p: [
        _statement(_DYNAMODB_WRITE, [_table(p), _table(p, "/index/*")])
    ],
    "DynamoDBStreamReadPolicy": lambda p: [
        _statement(
            ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator"],
            _arn(
                "dynamodb",
                "table/${tableName}/stream/${streamName}",
                tableName=p.get("TableName"),
                streamName=p.get("StreamName"),
            ),
        ),
        _statement(["dynamodb:ListStreams"], _table(p, "/stream/*")),
    ],
    "S3ReadPolicy": lambda p: [_statement(_S3_READ, [_bucket(p), _bucket(p, "/*")])],
    "S3WritePolicy": lambda p: [_statement(_S3_WRITE, [_bucket(p), _bucket(p, "/*")])],
    "S3CrudPolicy": lambda p: [
        _statement(_S3_READ + _S3_WRITE + ["s3:DeleteObject"], [_bucket(p), _bucket(p, "/*")])
    ],
    "SQSPollerPolicy": _single(_SQS_POLL, _named("sqs", "${name}", "QueueName")),
    "SQSSendMessagePolicy": _single(["sqs:SendMessage*"], _named("sqs", "${name}", "QueueName")),
    "SNSPublishMessagePolicy": _single(["sns:Publish"], _named("sns", "${name}", "TopicName")),
    "LambdaInvokePolicy": _single(
        ["lambda:InvokeFunction"], _named("lambda", "function:${name}*", "FunctionName")
    ),
    "StepFunctionsExecutionPolicy": _single(
        ["states:StartExecution"], _named("states", "stateMachine:${name}", "StateMachineName")
    ),
    "EventBridgePutEventsPolicy": _single(
        ["events:PutEvents"], _named("events", "event-bus/${name}", "EventBusName")
    ),
    "SSMParameterReadPolicy": lambda p: [
        _statement(["ssm:DescribeParameters"], "*"),
        _statement(_SSM_READ, _named("ssm", "parameter/${name}", "ParameterName")(p)),
    ],
    "SSMParameterWithSlashPrefixReadPolicy": lambda p: [
        _statement(["ssm:DescribeParameters"], "*"),
        _statement(_SSM_READ, _named("ssm", "parameter${name}", "ParameterName")(p)),
    ],
    "AWSSecretsManagerGetSecretValuePolicy": lambda p: [
        _statement(["secretsmanager:GetSecretValue"], p.get("SecretArn"))
    ],
    "KMSDecryptPolicy": _single(["kms:Decrypt"], _named("kms", "key/${name}", "KeyId")),
    "KMSEncryptPolicy": _single(["kms:Encrypt"], _named("kms", "key/${name}", "KeyId")),
    "KinesisStreamReadPolicy": lambda p: [
        _statement(["kinesis:ListStreams", "kinesis:DescribeLimits"], _arn("kinesis", "stream/*")),
        _statement(
            [
                "kinesis:DescribeStream",
                "kinesis:DescribeStreamSummary",
                "kinesis:GetRecords",
                "kinesis:GetShardIterator",
            ],
            _named("kinesis", "stream/${name}", "StreamName")(p),
        ),
    ],
    "CloudWatchPutMetricPolicy": lambda p: [_statement(["cloudwatch:PutMetricData"], "*")],
    "VPCAccessPolicy": lambda p: [_statement(_ENI, "*")],
    "AMIDescribePolicy": lambda p: [_statement(["ec2:DescribeImages"], "*")],
    "CloudFormationDescribeStacksPolicy": lambda p: [
        _statement(["cloudformation:DescribeStacks"], _arn("cloudformation", "stack/*"))
    ],
    "FilterLogEventsPolicy": _single(
        ["logs:FilterLogEvents"],
        _named("logs", "log-group:${name}:log-stream:*", "LogGroupName"),
    ),
}

# Event sources that poll a stream or queue, and the managed policy SAM attaches for each.
_POLLERS = {
    "SQS": ("Queue", "service-role/AWSLambdaSQSQueueExecutionRole"),
    "Kinesis": ("Stream", "service-role/AWSLambdaKinesisExecutionRole"),
    "DynamoDB": ("Stream", "service-role/AWSLambdaDynamoDBExecutionRole"),
}
_POLLER_PROPERTIES = (
    "BatchSize",
    "Enabled",
    "MaximumBatchingWindowInSeconds",
    "FilterCriteria",
    "FunctionResponseTypes",
    "ScalingConfig",
    "StartingPosition",
    "StartingPositionTimestamp",
    "MaximumRetryAttempts",
    "MaximumRecordAgeInSeconds",
    "BisectBatchOnFunctionError",
    "ParallelizationFactor",
    "TumblingWindowInSeconds",
    "DestinationConfig",
)


def _is_function(value: dict[str, Any]) -> bool:
    return len(value) == 1 and str(next(iter(value))).startswith(("Fn::", "Ref"))


def _merge_globals(global_props: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    """SAM's Globals rules: the resource's own value wins, maps merge, lists add up."""
    merged = copy.deepcopy(global_props)
    for name, value in props.items():
        current = merged.get(name)
        if (
            isinstance(current, dict)
            and isinstance(value, dict)
            and not _is_function(current)
            and not _is_function(value)
        ):
            merged[name] = _merge_globals(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged[name] = current + value
        else:
            merged[name] = value
    return merged


def _tag_list(tags: Any) -> Any:
    """SAM writes tags as a map; most CloudFormation types take [{Key, Value}]."""
    if isinstance(tags, dict) and not _is_function(tags):
        return [{"Key": k, "Value": v} for k, v in tags.items()]
    return tags


def _local_path(value: str) -> str:
    return value.strip().removeprefix("./").rstrip("/") or "."


def _file_body(uri: str) -> Expression:
    # yamldecode reads JSON as well, so this works for either kind of definition file.
    path = template_text(_local_path(uri))
    return Expression(f'yamldecode(file("${{path.module}}/{path}"))')


def _s3_location(uri: Any) -> Any:
    match = _S3_URI.match(uri) if isinstance(uri, str) else None
    if match:
        return {"Bucket": match.group(1), "Key": match.group(2)}
    return uri


def _alnum(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _unique(values: list[Any]) -> list[Any]:
    """Values without repeats, in order. Keys are JSON text, so it stays linear."""
    seen: set[str] = set()
    out: list[Any] = []
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _ref_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("Id", "Ref"):
            if isinstance(value.get(key), str):
                return value[key]
    return value if isinstance(value, str) else None


def _meta(resource: dict[str, Any]) -> dict[str, Any]:
    return {k: resource[k] for k in ("DependsOn", "Metadata") if k in resource}


def _substitute(definition: Any, substitutions: dict[str, Any]) -> Any:
    """SAM DefinitionSubstitutions applied to a definition object: `${Name}` in a string
    becomes the value, through Fn::Sub when the value is computed."""
    if isinstance(definition, dict):
        return {k: _substitute(v, substitutions) for k, v in definition.items()}
    if isinstance(definition, list):
        return [_substitute(v, substitutions) for v in definition]
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


def _rewrite_sub(text: str, aliases: dict[str, str]) -> str:
    """`${MyApi.Stage}` in a Fn::Sub string means the stage resource, as with Ref."""
    return _SUB_REFERENCE.sub(
        lambda m: "${" + aliases[m.group(1)] + "}" if m.group(1) in aliases else m.group(0),
        text,
    )


def _rewrite_refs(value: Any, aliases: dict[str, str]) -> Any:
    """`!Ref MyFunction.Alias`, `!Ref MyApi.Stage` and friends point at generated resources."""
    if isinstance(value, dict):
        if set(value) == {"Ref"} and value["Ref"] in aliases:
            return {"Ref": aliases[value["Ref"]]}
        if set(value) == {"Fn::Sub"}:
            args = value["Fn::Sub"]
            if isinstance(args, str):
                return {"Fn::Sub": _rewrite_sub(args, aliases)}
            if isinstance(args, list) and args and isinstance(args[0], str):
                rest = [_rewrite_refs(item, aliases) for item in args[1:]]
                return {"Fn::Sub": [_rewrite_sub(args[0], aliases), *rest]}
        return {k: _rewrite_refs(v, aliases) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_refs(v, aliases) for v in value]
    return value


class _Expansion:
    def __init__(self, template: dict[str, Any], notes: list[str]) -> None:
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
        if message not in self.notes:
            self.notes.append(message)

    def leftovers(self, logical_id: str, props: dict[str, Any]) -> None:
        for name in props:
            self.note(f"{logical_id}: SAM property '{name}' was not converted")

    def add(
        self,
        logical_id: str,
        kind: str,
        props: dict[str, Any],
        origin: dict[str, Any],
        **extra: Any,
    ) -> None:
        resource: dict[str, Any] = {"Type": kind, "Properties": props}
        for attribute in _INHERITED:
            if attribute in origin:
                resource[attribute] = origin[attribute]
        resource.update(extra)
        self.out[logical_id] = resource

    def props(self, resource: dict[str, Any]) -> dict[str, Any]:
        kind = resource["Type"]
        props = dict(resource.get("Properties") or {})
        for section, section_kind in _GLOBAL_SECTIONS.items():
            if kind == section_kind and isinstance(self.globals.get(section), dict):
                return _merge_globals(self.globals[section], props)
        return props

    # --- IAM --------------------------------------------------------------------------

    def _inline(self, name: str, statements: list[dict[str, Any]]) -> dict[str, Any]:
        return {"PolicyName": name, "PolicyDocument": _document(statements)}

    def _role(
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
        is SAM's Policies property: managed policy names, documents, or policy templates."""
        owner = role_id.removesuffix("Role")
        statements: list[dict[str, Any]] = []
        for index, policy in enumerate(_as_list(policies)):
            if isinstance(policy, str):
                managed.append(_managed_policy(policy))
            elif isinstance(policy, dict) and "Statement" in policy:
                inline.append(
                    self._inline(f"{role_id}Policy{index}", _as_list(policy["Statement"]))
                )
            elif isinstance(policy, dict) and _is_function(policy):
                managed.append(policy)
            elif isinstance(policy, dict) and len(policy) == 1:
                ((name, params),) = policy.items()
                template = POLICY_TEMPLATES.get(name)
                if template is None:
                    self.note(
                        f"{owner}: SAM policy template '{name}' was not converted; add its "
                        f"permissions to {role_id} by hand"
                    )
                    continue
                statements.extend(template(params if isinstance(params, dict) else {}))
            else:
                self.note(f"{owner}: a Policies entry was not converted; add it to {role_id}")
        if statements:
            inline.append(self._inline(f"{role_id}PolicyTemplates", statements))
        role: dict[str, Any] = {
            "AssumeRolePolicyDocument": _document(
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
            role["ManagedPolicyArns"] = _unique(managed)
        if inline:
            role["Policies"] = inline
        self.add(role_id, "AWS::IAM::Role", role, origin)

    def _permission(
        self,
        logical_id: str,
        function: Any,
        principal: str,
        source_arn: Any,
        origin: dict[str, Any],
        account: bool = False,
    ) -> None:
        props: dict[str, Any] = {
            "Action": "lambda:InvokeFunction",
            "FunctionName": function,
            "Principal": principal,
        }
        if source_arn is not None:
            props["SourceArn"] = source_arn
        if account:
            props["SourceAccount"] = {"Ref": "AWS::AccountId"}
        self.add(logical_id, "AWS::Lambda::Permission", props, origin)

    # --- functions --------------------------------------------------------------------

    def function(self, logical_id: str, resource: dict[str, Any]) -> None:
        p = self.props(resource)
        managed: list[Any] = [_managed_policy(_BASIC_EXECUTION)]
        inline: list[dict[str, Any]] = []
        fn: dict[str, Any] = {name: p.pop(name) for name in _FUNCTION_PROPERTIES if name in p}
        if "VpcConfig" in fn:
            # SAM gives functions in a VPC the permissions to manage their network interfaces.
            managed.append(_managed_policy("service-role/AWSLambdaVPCAccessExecutionRole"))
        self._code(logical_id, p, fn)

        tracing = p.pop("Tracing", None)
        if tracing is not None:
            fn["TracingConfig"] = {"Mode": tracing}
            if tracing == "Active":
                managed.append(_managed_policy("AWSXrayWriteOnlyAccess"))
        dlq = p.pop("DeadLetterQueue", None)
        if isinstance(dlq, dict):
            fn["DeadLetterConfig"] = {"TargetArn": dlq.get("TargetArn")}
            action = "sns:Publish" if dlq.get("Type") == "SNS" else "sqs:SendMessage"
            inline.append(
                self._inline(
                    f"{logical_id}RoleDeadLetterQueuePolicy",
                    [_statement([action], dlq.get("TargetArn"))],
                )
            )
        if "Tags" in p:
            fn["Tags"] = _tag_list(p.pop("Tags"))

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
                self.note(
                    f"{logical_id} sets both Role and Policies: SAM ignores Policies then, so "
                    "they were not converted"
                )
        self.add(logical_id, "AWS::Lambda::Function", fn, resource, **_meta(resource))

        target: Any = {"Fn::GetAtt": [logical_id, "Arn"]}
        if alias is not None:
            target = self._alias(logical_id, alias, resource)
        if not isinstance(events, dict):
            raise CfnError(f"Invalid SAM template: 'Events' of '{logical_id}' must be a mapping")
        for event_name, event in events.items():
            if not isinstance(event, dict):
                raise CfnError(
                    f"Invalid SAM template: event '{event_name}' of '{logical_id}' must be a "
                    "mapping"
                )
            managed.extend(self._event(f"{logical_id}{event_name}", event, target, resource))
        if isinstance(url, dict):
            self._url(
                logical_id, url, target if alias is not None else {"Ref": logical_id}, resource
            )

        if role is None:
            if assume is not None:
                role_extra["AssumeRolePolicyDocument"] = assume
            self._role(
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
                self.note(f"{logical_id}: SAM's {name} was not converted; set it up by hand")
        p.pop("Metadata", None)
        self.leftovers(logical_id, p)

    def _code(self, logical_id: str, p: dict[str, Any], fn: dict[str, Any]) -> None:
        image = p.pop("ImageUri", None)
        inline = p.pop("InlineCode", None)
        code = p.pop("CodeUri", None)
        match = _S3_URI.match(code) if isinstance(code, str) else None
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
            self.hints.setdefault(logical_id, {})["code_path"] = _local_path(code)
        elif code is not None:
            self.note(f"{logical_id}: CodeUri is computed by a function and was not converted")

    def _alias(self, logical_id: str, alias: Any, origin: dict[str, Any]) -> dict[str, Any]:
        version_id = f"{logical_id}Version"
        alias_id = f"{logical_id}Alias{_alnum(alias) if isinstance(alias, str) else ''}"
        self.add(version_id, "AWS::Lambda::Version", {"FunctionName": {"Ref": logical_id}}, origin)
        self.add(
            alias_id,
            "AWS::Lambda::Alias",
            {
                "FunctionName": {"Ref": logical_id},
                "FunctionVersion": {"Fn::GetAtt": [version_id, "Version"]},
                "Name": alias,
            },
            origin,
        )
        self.aliases[f"{logical_id}.Alias"] = alias_id
        self.aliases[f"{logical_id}.Version"] = version_id
        return {"Ref": alias_id}

    def _url(
        self, logical_id: str, url: dict[str, Any], target: Any, origin: dict[str, Any]
    ) -> None:
        props: dict[str, Any] = {
            "TargetFunctionArn": target,
            "AuthType": url.get("AuthType", "AWS_IAM"),
        }
        for name in ("Cors", "InvokeMode"):
            if name in url:
                props[name] = url[name]
        self.add(f"{logical_id}Url", "AWS::Lambda::Url", props, origin)
        if props["AuthType"] == "NONE":
            self.add(
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

    def _event(
        self, event_id: str, event: dict[str, Any], target: Any, origin: dict[str, Any]
    ) -> list[Any]:
        """One entry of a function's Events. Returns managed policies the role needs."""
        kind = event.get("Type")
        props = event.get("Properties") or {}
        if kind in _POLLERS:
            source_key, policy = _POLLERS[kind]
            mapping: dict[str, Any] = {
                "EventSourceArn": props.get(source_key),
                "FunctionName": target,
            }
            mapping.update({k: props[k] for k in _POLLER_PROPERTIES if k in props})
            self.add(event_id, "AWS::Lambda::EventSourceMapping", mapping, origin)
            return [_managed_policy(policy)]
        if kind in ("Schedule", "CloudWatchEvent", "EventBridgeRule"):
            self._rule(
                event_id, kind, props, {"Arn": target, "Id": f"{event_id}LambdaTarget"}, origin
            )
            self._permission(
                f"{event_id}Permission",
                target,
                "events.amazonaws.com",
                {"Fn::GetAtt": [event_id, "Arn"]},
                origin,
            )
        elif kind == "ScheduleV2":
            self._schedule_v2(event_id, props, target, "lambda:InvokeFunction", origin)
        elif kind == "SNS":
            subscription: dict[str, Any] = {
                "Protocol": "lambda",
                "Endpoint": target,
                "TopicArn": props.get("Topic"),
            }
            for name in ("FilterPolicy", "FilterPolicyScope", "Region", "RedrivePolicy"):
                if name in props:
                    subscription[name] = props[name]
            if props.get("SqsSubscription"):
                self.note(
                    f"{event_id}: the SNS event's SqsSubscription (a queue between the topic and "
                    "the function) was not converted"
                )
            self.add(event_id, "AWS::SNS::Subscription", subscription, origin)
            self._permission(
                f"{event_id}Permission", target, "sns.amazonaws.com", props.get("Topic"), origin
            )
        elif kind == "S3":
            self._s3_event(event_id, props, target, origin)
        elif kind == "CloudWatchLogs":
            self._permission(f"{event_id}Permission", target, "logs.amazonaws.com", None, origin)
            self.add(
                event_id,
                "AWS::Logs::SubscriptionFilter",
                {
                    "DestinationArn": target,
                    "FilterPattern": props.get("FilterPattern", ""),
                    "LogGroupName": props.get("LogGroupName"),
                },
                origin,
                DependsOn=[f"{event_id}Permission"],
            )
        elif kind == "Api":
            self._api_event(event_id, props, target, origin, http=False)
        elif kind == "HttpApi":
            self._api_event(event_id, props, target, origin, http=True)
        else:
            self.note(
                f"{event_id}: SAM event type '{kind}' was not converted; connect the event "
                "source by hand"
            )
        return []

    def _rule(
        self,
        event_id: str,
        kind: str,
        props: dict[str, Any],
        target: dict[str, Any],
        origin: dict[str, Any],
    ) -> None:
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
        self.add(event_id, "AWS::Events::Rule", rule, origin)

    def _schedule_v2(
        self, event_id: str, props: dict[str, Any], target: Any, action: str, origin: dict[str, Any]
    ) -> None:
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
        self.add(event_id, "AWS::Scheduler::Schedule", schedule, origin)
        self._role(
            role_id,
            "scheduler.amazonaws.com",
            [],
            None,
            [self._inline(f"{event_id}Policy", [_statement([action], target)])],
            origin,
        )

    def _s3_event(
        self, event_id: str, props: dict[str, Any], target: Any, origin: dict[str, Any]
    ) -> None:
        """S3 notifications live on the bucket, so the bucket in this template gets them."""
        self._permission(
            f"{event_id}Permission", target, "s3.amazonaws.com", None, origin, account=True
        )
        bucket_id = _ref_id(props.get("Bucket")) if isinstance(props.get("Bucket"), dict) else None
        bucket = self.source.get(bucket_id or "")
        if bucket is None or bucket.get("Type") != "AWS::S3::Bucket":
            self.note(
                f"{event_id}: the S3 event's bucket is not in this template; add the "
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
        configs.extend(dict(config, Event=name) for name in _as_list(props.get("Events")))
        bucket["DependsOn"] = _as_list(bucket.get("DependsOn")) + [f"{event_id}Permission"]

    def _api_event(
        self, event_id: str, props: dict[str, Any], target: Any, origin: dict[str, Any], http: bool
    ) -> None:
        default_api = "ServerlessHttpApi" if http else "ServerlessRestApi"
        api_id = _ref_id(props.get("ApiId" if http else "RestApiId")) or default_api
        paths = self.http_paths if http else self.rest_paths
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
                integration["uri"] = _sub(
                    f"{_PARTITION_ARN}:apigateway:${{AWS::Region}}:lambda:path/2015-03-31/"
                    "functions/${fn}/invocations",
                    fn=target,
                )
            operation = "x-amazon-apigateway-any-method" if method == "any" else method
            paths.setdefault(api_id, {}).setdefault(path, {})[operation] = {
                "x-amazon-apigateway-integration": integration,
                "responses": {},
            }
        if props.get("Auth"):
            self.note(f"{event_id}: the API event's Auth was not converted; add the authorizer")
        if http and path == "$default":
            scope = "*"
        else:
            source_path = re.sub(r"\{[^}]*\}", "*", path)
            scope = f"*/{'*' if method == 'any' else method.upper()}{source_path}"
        self._permission(
            f"{event_id}Permission",
            target,
            "apigateway.amazonaws.com",
            _sub(
                f"{_PARTITION_ARN}:execute-api:${{AWS::Region}}:${{AWS::AccountId}}:${{api}}/{scope}",
                api={"Ref": api_id},
            ),
            origin,
        )

    # --- APIs -------------------------------------------------------------------------

    def _api_body(self, p: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
        body = p.pop("DefinitionBody", None)
        uri = p.pop("DefinitionUri", None)
        if body is not None:
            return {"Body": body}
        if isinstance(uri, str) and not uri.startswith("s3://"):
            return {"Body": _file_body(uri)}
        if uri is not None:
            return {"BodyS3Location": _s3_location(uri)}
        return {"Body": generated}

    def rest_api(self, logical_id: str, resource: dict[str, Any] | None) -> None:
        """AWS::Serverless::Api (or the implicit ServerlessRestApi): RestApi, Deployment, Stage."""
        origin = resource or {}
        p = self.props(resource or {"Type": "AWS::Serverless::Api"})
        api: dict[str, Any] = {"Name": p.pop("Name", {"Ref": "AWS::StackName"})}
        generated = {
            "swagger": "2.0",
            "info": {"version": "1.0", "title": api["Name"]},
            "paths": self.rest_paths.get(logical_id, {}),
        }
        api.update(self._api_body(p, generated))
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
            api["Tags"] = _tag_list(p.pop("Tags"))
        self.add(logical_id, "AWS::ApiGateway::RestApi", api, origin, **_meta(origin))

        stage_name = p.pop("StageName", "Prod")
        deployment_id = f"{logical_id}Deployment"
        self.add(
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
        stage_id = f"{logical_id}{_alnum(str(stage_name))}Stage"
        self.add(stage_id, "AWS::ApiGateway::Stage", stage, origin)
        self.aliases[f"{logical_id}.Stage"] = stage_id
        self.aliases[f"{logical_id}.Deployment"] = deployment_id
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
                self.note(
                    f"{logical_id}: SAM API setting '{name}' was not converted; add it to the "
                    "API definition by hand"
                )
        self.leftovers(logical_id, p)

    def http_api(self, logical_id: str, resource: dict[str, Any] | None) -> None:
        """AWS::Serverless::HttpApi (or the implicit ServerlessHttpApi): Api and Stage."""
        origin = resource or {}
        p = self.props(resource or {"Type": "AWS::Serverless::HttpApi"})
        api: dict[str, Any] = {
            "Name": p.pop("Name", {"Ref": "AWS::StackName"}),
            "ProtocolType": "HTTP",
        }
        generated = {
            "openapi": "3.0.1",
            "info": {"version": "1.0", "title": api["Name"]},
            "paths": self.http_paths.get(logical_id, {}),
        }
        api.update(self._api_body(p, generated))
        cors = p.pop("CorsConfiguration", None)
        if isinstance(cors, dict):
            api["CorsConfiguration"] = cors
        elif cors is not None:
            self.note(f"{logical_id}: CorsConfiguration given as a single value was not converted")
        for name in ("Description", "DisableExecuteApiEndpoint", "FailOnWarnings"):
            if name in p:
                api[name] = p.pop(name)
        if "Tags" in p:
            api["Tags"] = p.pop("Tags")
        self.add(logical_id, "AWS::ApiGatewayV2::Api", api, origin, **_meta(origin))

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
            stage_id = f"{logical_id}{_alnum(str(stage_name))}Stage"
        self.add(stage_id, "AWS::ApiGatewayV2::Stage", stage, origin)
        self.aliases[f"{logical_id}.Stage"] = stage_id
        for name in ("Auth", "Domain"):
            if p.pop(name, None) is not None:
                self.note(
                    f"{logical_id}: SAM HTTP API setting '{name}' was not converted; set it up "
                    "by hand"
                )
        self.leftovers(logical_id, p)

    # --- other serverless types -------------------------------------------------------

    def simple_table(self, logical_id: str, resource: dict[str, Any]) -> None:
        p = self.props(resource)
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
            table["Tags"] = _tag_list(p.pop("Tags"))
        self.add(logical_id, "AWS::DynamoDB::Table", table, resource, **_meta(resource))
        self.leftovers(logical_id, p)

    def layer(self, logical_id: str, resource: dict[str, Any]) -> None:
        p = self.props(resource)
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
        match = _S3_URI.match(content) if isinstance(content, str) else None
        if isinstance(content, dict) and "Bucket" in content:
            layer["Content"] = {"S3Bucket": content["Bucket"], "S3Key": content.get("Key")}
            if "Version" in content:
                layer["Content"]["S3ObjectVersion"] = content["Version"]
        elif match:
            layer["Content"] = {"S3Bucket": match.group(1), "S3Key": match.group(2)}
        elif isinstance(content, str):
            self.hints.setdefault(logical_id, {})["code_path"] = _local_path(content)
        extra = {"DeletionPolicy": "Retain"} if p.pop("RetentionPolicy", None) == "Retain" else {}
        self.add(logical_id, "AWS::Lambda::LayerVersion", layer, resource, **extra)
        self.leftovers(logical_id, p)

    def state_machine(self, logical_id: str, resource: dict[str, Any]) -> None:
        p = self.props(resource)
        machine: dict[str, Any] = {}
        definition = p.pop("Definition", None)
        uri = p.pop("DefinitionUri", None)
        substitutions = p.pop("DefinitionSubstitutions", None)
        if definition is not None:
            machine["Definition"] = _substitute(definition, substitutions or {})
        elif isinstance(uri, str) and not uri.startswith("s3://"):
            machine["Definition"] = _file_body(uri)
            if substitutions:
                self.note(
                    f"{logical_id}: DefinitionSubstitutions for a definition file were not "
                    "applied; use templatefile() in place of file()"
                )
        elif uri is not None:
            machine["DefinitionS3Location"] = _s3_location(uri)
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
            machine["Tags"] = _tag_list(p.pop("Tags"))
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
        self.add(
            logical_id, "AWS::StepFunctions::StateMachine", machine, resource, **_meta(resource)
        )
        if role is None:
            self._role(
                f"{logical_id}Role", "states.amazonaws.com", [], policies, [], resource, extra
            )

        events = p.pop("Events", None) or {}
        for event_name, event in events.items() if isinstance(events, dict) else []:
            event_id = f"{logical_id}{event_name}"
            kind = (event or {}).get("Type")
            props = (event or {}).get("Properties") or {}
            start = [_statement(["states:StartExecution"], {"Ref": logical_id})]
            if kind in ("Schedule", "EventBridgeRule", "CloudWatchEvent"):
                role_id = f"{event_id}Role"
                target = {
                    "Arn": {"Ref": logical_id},
                    "Id": f"{event_id}StepFunctionsTarget",
                    "RoleArn": {"Fn::GetAtt": [role_id, "Arn"]},
                }
                self._rule(event_id, kind, props, target, resource)
                self._role(
                    role_id,
                    "events.amazonaws.com",
                    [],
                    None,
                    [self._inline(f"{event_id}StartExecution", start)],
                    resource,
                )
            elif kind == "ScheduleV2":
                self._schedule_v2(
                    event_id, props, {"Ref": logical_id}, "states:StartExecution", resource
                )
            else:
                self.note(f"{event_id}: SAM state machine event type '{kind}' was not converted")
        self.leftovers(logical_id, p)

    def connector(self, logical_id: str, props: dict[str, Any], source_id: str | None) -> None:
        """AWS::Serverless::Connector: IAM permissions (or a Lambda permission) between two
        resources of this template."""
        source_id = source_id or _ref_id(props.get("Source"))
        permissions = _as_list(props.get("Permissions"))
        destinations = _as_list(props.get("Destination"))
        for index, destination in enumerate(destinations):
            destination_id = _ref_id(destination)
            # SAM names each destination's policy separately when there are several.
            name = f"{logical_id}PolicyDestination{index}" if len(destinations) > 1 else None
            if source_id not in self.source or destination_id not in self.source:
                self.note(
                    f"{logical_id}: a connector end is not a resource in this template; not "
                    "converted"
                )
                continue
            _connect(self, logical_id, source_id, destination_id or "", permissions, name)

    def run(self) -> dict[str, Any]:
        handlers: dict[str, Callable[[str, dict[str, Any]], None]] = {
            "AWS::Serverless::Function": self.function,
            "AWS::Serverless::SimpleTable": self.simple_table,
            "AWS::Serverless::LayerVersion": self.layer,
            "AWS::Serverless::StateMachine": self.state_machine,
        }
        for logical_id, resource in self.source.items():
            props = resource.get("Properties") or {}
            if "DefinitionBody" in props or "DefinitionUri" in props:
                continue
            if resource["Type"] == "AWS::Serverless::Api":
                self.rest_paths[logical_id] = {}
            elif resource["Type"] == "AWS::Serverless::HttpApi":
                self.http_paths[logical_id] = {}

        connectors: list[tuple[str, dict[str, Any], str | None]] = []
        apis: list[str] = []
        for logical_id, resource in self.source.items():
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
            if kind in handlers:
                handlers[kind](logical_id, resource)
            elif kind in ("AWS::Serverless::Api", "AWS::Serverless::HttpApi"):
                # API bodies collect every function's events, so APIs are built last. The
                # placeholder keeps the resource order.
                apis.append(logical_id)
                self.out[logical_id] = resource
            elif kind == "AWS::Serverless::Connector":
                connectors.append((logical_id, resource.get("Properties") or {}, None))
            elif kind.startswith("AWS::Serverless::"):
                self.note(
                    f"{logical_id} ({kind}) was not converted: Terraform has no equivalent; "
                    "convert it by hand"
                )
            else:
                self.out[logical_id] = resource

        for logical_id in apis:
            resource = self.out[logical_id]
            if resource["Type"] == "AWS::Serverless::Api":
                self.rest_api(logical_id, resource)
            else:
                self.http_api(logical_id, resource)
        if "ServerlessRestApi" in self.rest_paths and "ServerlessRestApi" not in self.out:
            self.rest_api("ServerlessRestApi", None)
        if "ServerlessHttpApi" in self.http_paths and "ServerlessHttpApi" not in self.out:
            self.http_api("ServerlessHttpApi", None)
        for logical_id, props, source in connectors:
            self.connector(logical_id, props, source)

        template = {k: v for k, v in self.template.items() if k not in ("Globals", "Transform")}
        others = [t for t in _as_list(self.template.get("Transform")) if t != SAM_TRANSFORM]
        if others:
            template["Transform"] = others
        template["Resources"] = _rewrite_refs(self.out, self.aliases)
        if "Outputs" in template:
            template["Outputs"] = _rewrite_refs(template["Outputs"], self.aliases)
        return template


# --- connectors -------------------------------------------------------------------------

_CONNECTOR_ACTIONS: dict[str, dict[str, list[str]]] = {
    "AWS::DynamoDB::Table": {
        "Read": [
            "dynamodb:GetItem",
            "dynamodb:Query",
            "dynamodb:Scan",
            "dynamodb:BatchGetItem",
            "dynamodb:ConditionCheckItem",
            "dynamodb:PartiQLSelect",
        ],
        "Write": [
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:PartiQLDelete",
            "dynamodb:PartiQLInsert",
            "dynamodb:PartiQLUpdate",
        ],
    },
    "AWS::S3::Bucket": {
        "Read": ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket", "s3:GetObjectAcl"],
        "Write": ["s3:PutObject", "s3:PutObjectAcl", "s3:DeleteObject", "s3:DeleteObjectVersion"],
    },
    "AWS::SQS::Queue": {
        "Read": ["sqs:ReceiveMessage", "sqs:GetQueueAttributes"],
        "Write": ["sqs:SendMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility"],
    },
    "AWS::SNS::Topic": {"Write": ["sns:Publish"]},
    "AWS::Lambda::Function": {"Write": ["lambda:InvokeFunction"], "Read": ["lambda:GetFunction"]},
    "AWS::StepFunctions::StateMachine": {
        "Write": ["states:StartExecution", "states:StartSyncExecution"],
        "Read": ["states:DescribeStateMachine", "states:ListExecutions"],
    },
    "AWS::Events::EventBus": {"Write": ["events:PutEvents"]},
}
_SERVERLESS_TYPES = {
    "AWS::Serverless::Function": "AWS::Lambda::Function",
    "AWS::Serverless::SimpleTable": "AWS::DynamoDB::Table",
    "AWS::Serverless::StateMachine": "AWS::StepFunctions::StateMachine",
    "AWS::Serverless::Api": "AWS::ApiGateway::RestApi",
    "AWS::Serverless::HttpApi": "AWS::ApiGatewayV2::Api",
}
# Sources that call a function through a resource-based permission instead of a role.
_PRINCIPALS = {
    "AWS::ApiGateway::RestApi": "apigateway.amazonaws.com",
    "AWS::ApiGatewayV2::Api": "apigateway.amazonaws.com",
    "AWS::Events::Rule": "events.amazonaws.com",
    "AWS::SNS::Topic": "sns.amazonaws.com",
    "AWS::S3::Bucket": "s3.amazonaws.com",
}
_ROLE_PROPERTY = {"AWS::Lambda::Function": "Role", "AWS::StepFunctions::StateMachine": "RoleArn"}
# Ref gives the ARN for these; Fn::GetAtt Arn for the rest.
_REF_IS_ARN = {"AWS::SNS::Topic", "AWS::StepFunctions::StateMachine"}


def _connect(
    exp: _Expansion,
    logical_id: str,
    source_id: str,
    destination_id: str,
    permissions: list[str],
    policy_name: str | None = None,
) -> None:
    source_type = _SERVERLESS_TYPES.get(
        exp.source[source_id]["Type"], exp.source[source_id]["Type"]
    )
    destination_type = _SERVERLESS_TYPES.get(
        exp.source[destination_id]["Type"], exp.source[destination_id]["Type"]
    )
    if destination_type == "AWS::Lambda::Function" and source_type in _PRINCIPALS:
        source_arn: Any = {"Fn::GetAtt": [source_id, "Arn"]}
        if source_type in ("AWS::ApiGateway::RestApi", "AWS::ApiGatewayV2::Api"):
            source_arn = _sub(
                f"{_PARTITION_ARN}:execute-api:${{AWS::Region}}:${{AWS::AccountId}}:${{api}}/*",
                api={"Ref": source_id},
            )
        elif source_type == "AWS::SNS::Topic":
            source_arn = {"Ref": source_id}
        exp._permission(
            f"{logical_id}WriteLambdaPermission",
            {"Fn::GetAtt": [destination_id, "Arn"]},
            _PRINCIPALS[source_type],
            source_arn,
            {},
        )
        return
    actions_by_permission = _CONNECTOR_ACTIONS.get(destination_type)
    source = exp.out.get(source_id) or {}
    role_value = (source.get("Properties") or {}).get(_ROLE_PROPERTY.get(source_type, ""))
    role_id = role_value.get("Fn::GetAtt", [None])[0] if isinstance(role_value, dict) else None
    role = exp.out.get(role_id) if isinstance(role_id, str) else None
    actions = [a for p in permissions for a in (actions_by_permission or {}).get(str(p), [])]
    if role is None or not actions:
        exp.note(
            f"{logical_id}: connector from {source_type} to {destination_type} was not "
            "converted; grant the access by hand"
        )
        return
    if destination_type in _REF_IS_ARN:
        resources: list[Any] = [{"Ref": destination_id}]
    else:
        resources = [{"Fn::GetAtt": [destination_id, "Arn"]}]
    if destination_type == "AWS::DynamoDB::Table":
        resources.append(_sub("${arn}/index/*", arn={"Fn::GetAtt": [destination_id, "Arn"]}))
    elif destination_type == "AWS::S3::Bucket":
        resources.append(_sub("${arn}/*", arn={"Fn::GetAtt": [destination_id, "Arn"]}))
    role["Properties"].setdefault("Policies", []).append(
        {
            "PolicyName": policy_name or f"{logical_id}Policy",
            "PolicyDocument": _document([_statement(actions, resources)]),
        }
    )


def expand_sam(
    template: dict[str, Any], notes: list[str]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Expand every AWS::Serverless::* resource. Returns (template, converter hints)."""
    expansion = _Expansion(copy.deepcopy(template), notes)
    return expansion.run(), expansion.hints
