"""Which hand-written converter handles which CloudFormation type."""

from __future__ import annotations

from converters.cfn.resources import (
    apigateway,
    compute,
    data,
    iam,
    messaging,
    network,
    observability,
    storage,
)
from converters.cfn.resources.base import ResourceConverter


class ResourceRegistry:
    """The hand-written converters, by CloudFormation type. A type without one is
    converted with the awscc provider."""

    def __init__(self, converters: tuple[type[ResourceConverter], ...]) -> None:
        """One instance of each converter class."""
        self.by_type = {converter.cfn_type: converter() for converter in converters}

    def get(self, cfn_type: str) -> ResourceConverter | None:
        """The converter for `cfn_type`, or None."""
        return self.by_type.get(cfn_type)


REGISTRY = ResourceRegistry(
    apigateway.CONVERTERS
    + compute.CONVERTERS
    + data.CONVERTERS
    + iam.CONVERTERS
    + messaging.CONVERTERS
    + network.CONVERTERS
    + observability.CONVERTERS
    + storage.CONVERTERS
)
