"""Request bodies of the converter-api endpoints, with the limits each field enforces."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from converters.cfn.constants import MAX_TEMPLATE_CHARS

# Large enough for any real compose file, small enough to stop someone posting megabytes of YAML.
MAX_INPUT_CHARS = 100_000
# K8s namespace names are DNS-1123 labels.
NAMESPACE_PATTERN = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
# CloudFormation stack names: a letter, then letters, digits and '-', at most 128 characters.
STACK_NAME_PATTERN = r"^[A-Za-z][-A-Za-z0-9]{0,127}$"


class ConvertRequest(BaseModel):
    """A docker-compose file to convert to Kubernetes manifests."""

    compose_yaml: str = Field(..., max_length=MAX_INPUT_CHARS, description="docker-compose YAML")
    namespace: str = Field("default", pattern=NAMESPACE_PATTERN)
    include_service: bool = True


class ConvertFilesRequest(ConvertRequest):
    """A docker-compose file to convert, and the kind of files to return."""

    output: Literal["manifests", "helm"] = Field(
        "manifests", description="'manifests': one YAML file per resource. 'helm': a Helm chart."
    )


class CfnRequest(BaseModel):
    """A CloudFormation, AWS SAM or synthesized CDK template to convert to Terraform."""

    template: str = Field(
        ...,
        max_length=MAX_TEMPLATE_CHARS,
        description="CloudFormation, AWS SAM or `cdk synth` template, JSON or YAML",
    )
    provider: Literal["aws", "awscc"] = Field(
        "aws",
        description="'aws': hand-written aws provider mappings, awscc for other types. "
        "'awscc': every resource with the awscc provider.",
    )
    layout: Literal["files", "single"] = Field(
        "files", description="'files': versions.tf, variables.tf, main.tf... 'single': main.tf"
    )
    stack_name: str | None = Field(
        None,
        pattern=STACK_NAME_PATTERN,
        description="Default for var.stack_name, which replaces AWS::StackName",
    )
    include_imports: bool = Field(
        False, description="Also write imports.tf, to adopt resources the stack already created"
    )


class ValidateRequest(BaseModel):
    """YAML text to check, one document or several separated by ---."""

    yaml: str = Field(..., max_length=MAX_INPUT_CHARS)
