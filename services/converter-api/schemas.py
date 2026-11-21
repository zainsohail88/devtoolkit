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

    compose_yaml: str = Field(
        ..., max_length=MAX_INPUT_CHARS, description="The docker-compose file to convert, as YAML."
    )
    namespace: str = Field(
        "default",
        pattern=NAMESPACE_PATTERN,
        description="Kubernetes namespace of the resources. Any namespace other than 'default' "
        "also gets a Namespace manifest.",
    )
    include_service: bool = Field(
        True, description="Add a ClusterIP Service for each compose service that has ports."
    )


class ConvertFilesRequest(ConvertRequest):
    """A docker-compose file to convert, and the kind of files to return."""

    output: Literal["manifests", "helm"] = Field(
        "manifests",
        description="'manifests' returns one YAML file per resource. 'helm' returns a Helm chart.",
    )


class CfnRequest(BaseModel):
    """A CloudFormation, AWS SAM or synthesized CDK template to convert to Terraform."""

    template: str = Field(
        ...,
        max_length=MAX_TEMPLATE_CHARS,
        description="The CloudFormation, AWS SAM or `cdk synth` template to convert, as JSON "
        "or YAML.",
    )
    provider: Literal["aws", "awscc"] = Field(
        "aws",
        description="'aws' uses the aws provider for every type the converter maps by hand "
        "and awscc for the rest. 'awscc' uses the awscc provider for every resource.",
    )
    layout: Literal["files", "single"] = Field(
        "files",
        description="'files' splits the output into versions.tf, variables.tf, main.tf and more "
        "files. 'single' puts everything in main.tf.",
    )
    stack_name: str | None = Field(
        None,
        pattern=STACK_NAME_PATTERN,
        description="Default value of var.stack_name. The Terraform code uses it wherever the "
        "template uses AWS::StackName.",
    )
    include_imports: bool = Field(
        False,
        description="Also write imports.tf, so Terraform can take over the resources the stack "
        "already created.",
    )


class ValidateRequest(BaseModel):
    """YAML text to check, one document or several separated by ---."""

    yaml: str = Field(
        ...,
        max_length=MAX_INPUT_CHARS,
        description="The YAML to check. Separate several documents with ---.",
    )
