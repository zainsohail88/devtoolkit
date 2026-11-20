"""Hand-written mappings from CloudFormation resource types to the `aws` Terraform provider.

Each module registers its converters with @resource. A type without one here is converted
with the awscc provider, which mirrors CloudFormation's own schema (see awscc.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from converters.cfn.builder import Builder
from converters.cfn.context import ResourceInfo


@dataclass(frozen=True)
class Mapping:
    cfn_type: str
    tf_type: str
    convert: Callable[[Builder], None]
    ref: str = "id"  # attribute `Ref` returns
    attrs: dict[str, str] = field(default_factory=dict)  # Fn::GetAtt name -> attribute
    import_id: str | None = None  # property whose literal value is the import ID
    # Adjusts the ResourceInfo before any conversion, when references depend on properties.
    prepare: Callable[[ResourceInfo, dict[str, Any]], None] | None = None


REGISTRY: dict[str, Mapping] = {}


def resource(
    cfn_type: str,
    tf_type: str,
    ref: str = "id",
    attrs: dict[str, str] | None = None,
    import_id: str | None = None,
    prepare: Callable[[ResourceInfo, dict[str, Any]], None] | None = None,
) -> Callable[[Callable[[Builder], None]], Callable[[Builder], None]]:
    def register(convert: Callable[[Builder], None]) -> Callable[[Builder], None]:
        REGISTRY[cfn_type] = Mapping(
            cfn_type, tf_type, convert, ref, attrs or {}, import_id, prepare
        )
        return convert

    return register


# Imported for their @resource registrations.
from converters.cfn.resources import (  # noqa: E402, F401
    apigateway,
    compute,
    data,
    iam,
    messaging,
    network,
    observability,
    storage,
)
