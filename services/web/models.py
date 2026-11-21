"""The forms of the tool pages and the outcome of handling one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from settings import SOURCE_LABELS


@dataclass
class ComposeForm:
    """The fields of the Compose to Kubernetes form."""

    compose_yaml: str
    namespace: str = "default"
    include_service: bool = True
    output: str = "manifests"

    def context(self) -> dict[str, Any]:
        """The template context that fills the form with these values."""
        return {
            "compose_yaml": self.compose_yaml,
            "namespace": self.namespace,
            "include_service": self.include_service,
            "output": self.output,
        }


@dataclass
class CfnForm:
    """The fields of the CloudFormation to Terraform form."""

    template: str
    provider: str = "aws"
    layout: str = "files"
    stack_name: str = ""
    include_imports: bool = False

    def context(self) -> dict[str, Any]:
        """The template context that fills the form with these values."""
        return {
            "template": self.template,
            "provider": self.provider,
            "layout": self.layout,
            "stack_name": self.stack_name,
            "include_imports": self.include_imports,
            "source_labels": SOURCE_LABELS,
        }


@dataclass
class Outcome:
    """What a submitted form leads to: the template context and the HTTP status."""

    context: dict[str, Any]
    status: int = 200

    @property
    def ok(self) -> bool:
        """Whether the input was handled without an error."""
        return self.status == 200
