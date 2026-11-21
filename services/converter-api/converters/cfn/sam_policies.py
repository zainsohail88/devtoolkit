"""IAM documents for the SAM expansion: Fn::Sub ARNs, statements and the SAM policy templates."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from converters.cfn.sam_constants import (
    DYNAMODB_CRUD,
    DYNAMODB_READ,
    DYNAMODB_WRITE,
    ENI,
    PARTITION_ARN,
    S3_READ,
    S3_WRITE,
    SQS_POLL,
    SSM_READ,
)

PolicyTemplate = Callable[[dict[str, Any]], list[dict[str, Any]]]


def sub(text: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """A Fn::Sub of `text`, with the variable map when there is one."""
    if variables:
        return {"Fn::Sub": [text, variables]}
    return {"Fn::Sub": text}


def arn(service: str, resource: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """An ARN in this partition, region and account."""
    return sub(
        f"{PARTITION_ARN}:{service}:${{AWS::Region}}:${{AWS::AccountId}}:{resource}", variables
    )


def managed_policy(name: str) -> Any:
    """The ARN of an AWS managed policy given by name (or the ARN itself)."""
    if name.startswith("arn:"):
        return name
    return sub(f"{PARTITION_ARN}:iam::aws:policy/{name}")


def statement(actions: list[str] | tuple[str, ...], resources: Any) -> dict[str, Any]:
    """An Allow statement for `actions` on `resources` (one resource or a list)."""
    return {
        "Effect": "Allow",
        "Action": list(actions),
        "Resource": resources if isinstance(resources, list) else [resources],
    }


def document(statements: list[dict[str, Any]]) -> dict[str, Any]:
    """A policy document holding `statements`."""
    return {"Version": "2012-10-17", "Statement": statements}


def inline_policy(name: str, statements: list[dict[str, Any]]) -> dict[str, Any]:
    """An inline policy of a role."""
    return {"PolicyName": name, "PolicyDocument": document(statements)}


class PolicyTemplates:
    """The commonly used SAM policy templates as IAM statements. Each template takes its
    parameters ({"TableName": ...}) and returns a list of statements."""

    def __init__(self) -> None:
        """The table of templates by SAM name."""
        self.by_name: dict[str, PolicyTemplate] = {
            "DynamoDBCrudPolicy": self.dynamodb_crud,
            "DynamoDBReadPolicy": self.dynamodb_read,
            "DynamoDBWritePolicy": self.dynamodb_write,
            "DynamoDBStreamReadPolicy": self.dynamodb_stream_read,
            "S3ReadPolicy": self.s3_read,
            "S3WritePolicy": self.s3_write,
            "S3CrudPolicy": self.s3_crud,
            "SQSPollerPolicy": self.sqs_poller,
            "SQSSendMessagePolicy": self.sqs_send_message,
            "SNSPublishMessagePolicy": self.sns_publish_message,
            "LambdaInvokePolicy": self.lambda_invoke,
            "StepFunctionsExecutionPolicy": self.step_functions_execution,
            "EventBridgePutEventsPolicy": self.event_bridge_put_events,
            "SSMParameterReadPolicy": self.ssm_parameter_read,
            "SSMParameterWithSlashPrefixReadPolicy": self.ssm_parameter_with_slash_prefix_read,
            "AWSSecretsManagerGetSecretValuePolicy": self.secrets_manager_get_secret_value,
            "KMSDecryptPolicy": self.kms_decrypt,
            "KMSEncryptPolicy": self.kms_encrypt,
            "KinesisStreamReadPolicy": self.kinesis_stream_read,
            "CloudWatchPutMetricPolicy": self.cloudwatch_put_metric,
            "VPCAccessPolicy": self.vpc_access,
            "AMIDescribePolicy": self.ami_describe,
            "CloudFormationDescribeStacksPolicy": self.cloudformation_describe_stacks,
            "FilterLogEventsPolicy": self.filter_log_events,
        }

    def names(self) -> list[str]:
        """Every supported template name, sorted."""
        return sorted(self.by_name)

    def get(self, name: str) -> PolicyTemplate | None:
        """The template called `name`, or None for one this converter does not know."""
        return self.by_name.get(name)

    # --- ARN helpers ------------------------------------------------------------------

    def table(self, params: dict[str, Any], suffix: str = "") -> Any:
        """The ARN of the TableName table, with `suffix` (/index/*, /stream/*)."""
        return arn(
            "dynamodb", "table/${tableName}" + suffix, {"tableName": params.get("TableName")}
        )

    def bucket(self, params: dict[str, Any], suffix: str = "") -> Any:
        """The ARN of the BucketName bucket, with `suffix` (/*)."""
        return sub(
            f"{PARTITION_ARN}:s3:::${{bucketName}}{suffix}",
            {"bucketName": params.get("BucketName")},
        )

    def named(self, service: str, resource: str, value: Any) -> Any:
        """The ARN of `resource`, where `${name}` is `value` (a template parameter)."""
        return arn(service, resource, {"name": value})

    def single(
        self,
        actions: list[str] | tuple[str, ...],
        service: str,
        resource: str,
        value: Any,
    ) -> list[dict[str, Any]]:
        """One statement for `actions` on the ARN that `named` builds."""
        return [statement(actions, self.named(service, resource, value))]

    # --- the templates ----------------------------------------------------------------

    def dynamodb_crud(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """DynamoDBCrudPolicy."""
        return [statement(DYNAMODB_CRUD, [self.table(p), self.table(p, "/index/*")])]

    def dynamodb_read(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """DynamoDBReadPolicy."""
        return [statement(DYNAMODB_READ, [self.table(p), self.table(p, "/index/*")])]

    def dynamodb_write(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """DynamoDBWritePolicy."""
        return [statement(DYNAMODB_WRITE, [self.table(p), self.table(p, "/index/*")])]

    def dynamodb_stream_read(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """DynamoDBStreamReadPolicy."""
        return [
            statement(
                ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator"],
                arn(
                    "dynamodb",
                    "table/${tableName}/stream/${streamName}",
                    {"tableName": p.get("TableName"), "streamName": p.get("StreamName")},
                ),
            ),
            statement(["dynamodb:ListStreams"], self.table(p, "/stream/*")),
        ]

    def s3_read(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """S3ReadPolicy."""
        return [statement(S3_READ, [self.bucket(p), self.bucket(p, "/*")])]

    def s3_write(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """S3WritePolicy."""
        return [statement(S3_WRITE, [self.bucket(p), self.bucket(p, "/*")])]

    def s3_crud(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """S3CrudPolicy."""
        actions = S3_READ + S3_WRITE + ("s3:DeleteObject",)
        return [statement(actions, [self.bucket(p), self.bucket(p, "/*")])]

    def sqs_poller(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """SQSPollerPolicy."""
        return self.single(SQS_POLL, "sqs", "${name}", p.get("QueueName"))

    def sqs_send_message(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """SQSSendMessagePolicy."""
        return self.single(["sqs:SendMessage*"], "sqs", "${name}", p.get("QueueName"))

    def sns_publish_message(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """SNSPublishMessagePolicy."""
        return self.single(["sns:Publish"], "sns", "${name}", p.get("TopicName"))

    def lambda_invoke(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """LambdaInvokePolicy."""
        return self.single(
            ["lambda:InvokeFunction"], "lambda", "function:${name}*", p.get("FunctionName")
        )

    def step_functions_execution(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """StepFunctionsExecutionPolicy."""
        return self.single(
            ["states:StartExecution"], "states", "stateMachine:${name}", p.get("StateMachineName")
        )

    def event_bridge_put_events(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """EventBridgePutEventsPolicy."""
        return self.single(
            ["events:PutEvents"], "events", "event-bus/${name}", p.get("EventBusName")
        )

    def ssm_parameter_read(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """SSMParameterReadPolicy."""
        return [
            statement(["ssm:DescribeParameters"], "*"),
            statement(SSM_READ, self.named("ssm", "parameter/${name}", p.get("ParameterName"))),
        ]

    def ssm_parameter_with_slash_prefix_read(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """SSMParameterWithSlashPrefixReadPolicy."""
        return [
            statement(["ssm:DescribeParameters"], "*"),
            statement(SSM_READ, self.named("ssm", "parameter${name}", p.get("ParameterName"))),
        ]

    def secrets_manager_get_secret_value(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """AWSSecretsManagerGetSecretValuePolicy."""
        return [statement(["secretsmanager:GetSecretValue"], p.get("SecretArn"))]

    def kms_decrypt(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """KMSDecryptPolicy."""
        return self.single(["kms:Decrypt"], "kms", "key/${name}", p.get("KeyId"))

    def kms_encrypt(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """KMSEncryptPolicy."""
        return self.single(["kms:Encrypt"], "kms", "key/${name}", p.get("KeyId"))

    def kinesis_stream_read(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """KinesisStreamReadPolicy."""
        return [
            statement(
                ["kinesis:ListStreams", "kinesis:DescribeLimits"], arn("kinesis", "stream/*")
            ),
            statement(
                [
                    "kinesis:DescribeStream",
                    "kinesis:DescribeStreamSummary",
                    "kinesis:GetRecords",
                    "kinesis:GetShardIterator",
                ],
                self.named("kinesis", "stream/${name}", p.get("StreamName")),
            ),
        ]

    def cloudwatch_put_metric(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """CloudWatchPutMetricPolicy (no parameters)."""
        return [statement(["cloudwatch:PutMetricData"], "*")]

    def vpc_access(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """VPCAccessPolicy (no parameters)."""
        return [statement(ENI, "*")]

    def ami_describe(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """AMIDescribePolicy (no parameters)."""
        return [statement(["ec2:DescribeImages"], "*")]

    def cloudformation_describe_stacks(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """CloudFormationDescribeStacksPolicy (no parameters)."""
        return [statement(["cloudformation:DescribeStacks"], arn("cloudformation", "stack/*"))]

    def filter_log_events(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """FilterLogEventsPolicy."""
        return self.single(
            ["logs:FilterLogEvents"],
            "logs",
            "log-group:${name}:log-stream:*",
            p.get("LogGroupName"),
        )
