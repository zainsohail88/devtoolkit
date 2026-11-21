"""Limits, names and versions the converter uses."""

from __future__ import annotations

# CloudFormation accepts up to 1 MB from S3. 500,000 characters covers large CDK stacks and
# keeps one request well under a second of CPU.
MAX_TEMPLATE_CHARS = 500_000
MAX_NODES = 100_000
MAX_DEPTH = 100
# Nested Fn::If and Fn::Join chains deeper than this are beyond anything written by hand.
MAX_EXPRESSION_DEPTH = 50
MAX_JSON_DEPTH = 100
# CloudFormation's own limit on the number of CIDR blocks Fn::Cidr returns.
MAX_CIDR_COUNT = 256

SAM_TRANSFORM = "AWS::Serverless-2016-10-31"
LANGUAGE_EXTENSIONS = "AWS::LanguageExtensions"

PROVIDERS = ("aws", "awscc")
LAYOUTS = ("files", "single")
SOURCES = ("cloudformation", "sam", "cdk")

TERRAFORM_VERSION = ">= 1.5"
# 6.40 or later: DynamoDB global indexes use key_schema blocks, added in 6.30.
AWS_VERSION = "~> 6.40"
AWSCC_VERSION = "~> 1.0"
ARCHIVE_VERSION = "~> 2.0"

# Pseudo parameters read from a data source: (data source type, name, attribute).
PSEUDO_DATA = {
    "AWS::Region": ("aws_region", "current", "region"),
    "AWS::AccountId": ("aws_caller_identity", "current", "account_id"),
    "AWS::Partition": ("aws_partition", "current", "partition"),
    "AWS::URLSuffix": ("aws_partition", "current", "dns_suffix"),
}

# Intrinsic functions whose result is text, so a JSON document property holding one is
# already JSON.
STRING_FUNCTIONS = frozenset(
    {
        "Ref",
        "Fn::Sub",
        "Fn::Join",
        "Fn::GetAtt",
        "Fn::ImportValue",
        "Fn::ToJsonString",
        "Fn::Base64",
    }
)
