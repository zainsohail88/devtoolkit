"""Names the CDK writes into synthesized templates."""

from __future__ import annotations

import re

# Helper Lambda functions the CDK adds to implement a feature through a custom resource,
# recognised by their resource type, and what replaces them in Terraform.
AUTO_DELETE = "Custom::S3AutoDeleteObjects"
LOG_RETENTION = "Custom::LogRetention"
RESTRICT_DEFAULT_SG = "Custom::VpcRestrictDefaultSG"
HANDLED = (AUTO_DELETE, LOG_RETENTION, RESTRICT_DEFAULT_SG)
BOOTSTRAP_PARAMETER = "BootstrapVersion"
BOOTSTRAP_RULE = "CheckBootstrapVersion"
METADATA_TYPE = "AWS::CDK::Metadata"
METADATA_CONDITION = "CDKMetadataAvailable"
AUTO_DELETE_TAG = "aws-cdk:auto-delete-objects"
# Construct path parts the CDK adds below the construct a user names.
PATH_SUFFIXES = ("Resource", "Default")
NON_WORD = re.compile(r"[^A-Za-z0-9]+")
SUB_NAME = re.compile(r"\$\{([A-Za-z0-9.]+)\}")
