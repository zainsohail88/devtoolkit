"""Names, property lists and IAM actions the SAM expansion uses."""

from __future__ import annotations

import re

PARTITION_ARN = "arn:${AWS::Partition}"
BASIC_EXECUTION = "service-role/AWSLambdaBasicExecutionRole"
GLOBAL_SECTIONS = {
    "Function": "AWS::Serverless::Function",
    "Api": "AWS::Serverless::Api",
    "HttpApi": "AWS::Serverless::HttpApi",
    "SimpleTable": "AWS::Serverless::SimpleTable",
    "StateMachine": "AWS::Serverless::StateMachine",
    "LayerVersion": "AWS::Serverless::LayerVersion",
}
# Resource attributes every generated resource inherits from the SAM resource.
INHERITED = ("Condition", "DeletionPolicy", "UpdateReplacePolicy")
S3_URI = re.compile(r"^s3://([^/]+)/(.+)$")
SUB_REFERENCE = re.compile(r"\$\{([A-Za-z0-9]+\.[A-Za-z]+)\}")
FUNCTION_PROPERTIES = (
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

# IAM actions of the SAM policy templates.
DYNAMODB_CRUD = (
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
)
DYNAMODB_READ = (
    "dynamodb:GetItem",
    "dynamodb:Scan",
    "dynamodb:Query",
    "dynamodb:BatchGetItem",
    "dynamodb:DescribeTable",
)
DYNAMODB_WRITE = ("dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:BatchWriteItem")
S3_READ = (
    "s3:GetObject",
    "s3:ListBucket",
    "s3:GetBucketLocation",
    "s3:GetObjectVersion",
    "s3:GetLifecycleConfiguration",
)
S3_WRITE = ("s3:PutObject", "s3:PutObjectAcl", "s3:PutLifecycleConfiguration")
SQS_POLL = (
    "sqs:ChangeMessageVisibility",
    "sqs:ChangeMessageVisibilityBatch",
    "sqs:DeleteMessage",
    "sqs:DeleteMessageBatch",
    "sqs:GetQueueAttributes",
    "sqs:ReceiveMessage",
)
SSM_READ = ("ssm:GetParameters", "ssm:GetParameter", "ssm:GetParametersByPath")
ENI = (
    "ec2:CreateNetworkInterface",
    "ec2:DeleteNetworkInterface",
    "ec2:DescribeNetworkInterfaces",
    "ec2:DetachNetworkInterface",
)

# Event sources that poll a stream or queue, and the managed policy SAM attaches for each.
POLLERS = {
    "SQS": ("Queue", "service-role/AWSLambdaSQSQueueExecutionRole"),
    "Kinesis": ("Stream", "service-role/AWSLambdaKinesisExecutionRole"),
    "DynamoDB": ("Stream", "service-role/AWSLambdaDynamoDBExecutionRole"),
}
POLLER_PROPERTIES = (
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

# The IAM actions a connector grants, by destination type and permission (Read, Write).
CONNECTOR_ACTIONS: dict[str, dict[str, list[str]]] = {
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
# The plain CloudFormation type each serverless type expands to.
SERVERLESS_TYPES = {
    "AWS::Serverless::Function": "AWS::Lambda::Function",
    "AWS::Serverless::SimpleTable": "AWS::DynamoDB::Table",
    "AWS::Serverless::StateMachine": "AWS::StepFunctions::StateMachine",
    "AWS::Serverless::Api": "AWS::ApiGateway::RestApi",
    "AWS::Serverless::HttpApi": "AWS::ApiGatewayV2::Api",
}
# Sources that call a function through a resource-based permission instead of a role.
PRINCIPALS = {
    "AWS::ApiGateway::RestApi": "apigateway.amazonaws.com",
    "AWS::ApiGatewayV2::Api": "apigateway.amazonaws.com",
    "AWS::Events::Rule": "events.amazonaws.com",
    "AWS::SNS::Topic": "sns.amazonaws.com",
    "AWS::S3::Bucket": "s3.amazonaws.com",
}
# The property that holds the role of a connector source.
ROLE_PROPERTY = {"AWS::Lambda::Function": "Role", "AWS::StepFunctions::StateMachine": "RoleArn"}
# Ref gives the ARN for these; Fn::GetAtt Arn for the rest.
REF_IS_ARN = {"AWS::SNS::Topic", "AWS::StepFunctions::StateMachine"}
