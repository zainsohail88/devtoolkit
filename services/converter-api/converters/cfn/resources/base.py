"""The base class of every hand-written CloudFormation to `aws` provider mapping."""

from __future__ import annotations

from typing import Any, ClassVar

from converters.cfn.builder import Builder
from converters.cfn.models import ResourceInfo


class ResourceConverter:
    """Converts one CloudFormation resource type to the `aws` provider.

    A subclass names the types it maps between and how references read it:

        class SqsQueue(ResourceConverter):
            cfn_type = "AWS::SQS::Queue"
            tf_type = "aws_sqs_queue"
            ref = "url"
            attrs = {"Arn": "arn", "QueueName": "name"}

            def convert(self, b: Builder) -> None:
                b.props.number("visibility_timeout_seconds", "VisibilityTimeout")

    and lists itself in its module's CONVERTERS, which ResourceRegistry reads.
    """

    cfn_type: ClassVar[str] = ""
    tf_type: ClassVar[str] = ""
    # The attribute `Ref` returns.
    ref: ClassVar[str] = "id"
    # Fn::GetAtt attribute name -> Terraform attribute.
    attrs: ClassVar[dict[str, str]] = {}
    # The property whose literal value is the resource's import ID, if there is one.
    import_id: ClassVar[str | None] = None

    def prepare(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """Adjust where references point, before any resource is converted. Runs for every
        resource of this type; the default changes nothing."""

    def convert(self, b: Builder) -> None:
        """Turn the resource's properties into Terraform. Properties left in `b.props` are
        reported as not converted."""
