"""The whole conversion: template text in, Terraform files and notes out.

result = convert_template(open("template.yaml").read())
result["files"]      # [{"path": "main.tf", "content": "..."}, ...]
result["notes"]      # what needs a human decision
result["resources"]  # each CloudFormation resource and where it went
result["source"]     # "cloudformation", "sam" or "cdk"
"""

from __future__ import annotations

from typing import Any

from converters.cfn.cdk import prepare_cdk
from converters.cfn.constants import LANGUAGE_EXTENSIONS, LAYOUTS, PROVIDERS, SAM_TRANSFORM
from converters.cfn.convert import Converter
from converters.cfn.errors import CfnError
from converters.cfn.parse import detect_source, load_template, transforms, unknown_sections
from converters.cfn.sam import expand_sam


class TemplateConverter:
    """Reads a template, expands SAM or cleans up CDK output, and converts the result."""

    def __init__(
        self,
        provider: str = "aws",
        layout: str = "files",
        stack_name: str | None = None,
        include_imports: bool = False,
    ) -> None:
        """Conversion options. provider: "aws" (hand-written mappings, awscc for the rest)
        or "awscc" (awscc only). layout: "files" (versions.tf, variables.tf, main.tf...) or
        "single" (main.tf)."""
        if provider not in PROVIDERS:
            raise CfnError(
                f"The provider '{provider}' is unknown. Use one of: {', '.join(PROVIDERS)}."
            )
        if layout not in LAYOUTS:
            raise CfnError(f"The layout '{layout}' is unknown. Use one of: {', '.join(LAYOUTS)}.")
        self.provider = provider
        self.layout = layout
        self.stack_name = stack_name
        self.include_imports = include_imports

    def convert(self, text: str) -> dict[str, Any]:
        """Convert template text (JSON or YAML). Raises CfnError for input it cannot
        convert."""
        template = load_template(text)
        source = detect_source(template)
        notes = self.template_notes(template)
        names: dict[str, str] = {}
        hints: dict[str, dict[str, Any]] = {}
        if source == "sam":
            template, hints = expand_sam(template, notes)
        elif source == "cdk":
            template, names, hints = prepare_cdk(template, notes)
        if template.get("Rules"):
            notes.append(
                "The Rules section of the template was left out. Add its checks as validation "
                "blocks on the variables or as preconditions."
            )
        converter = Converter(
            template, self.provider, self.stack_name, self.include_imports, names, hints
        )
        result = converter.run(self.layout)
        return {
            "source": source,
            "files": result["files"],
            "notes": notes + result["notes"],
            "resources": result["resources"],
            "summary": self.summary(result["resources"]),
        }

    def template_notes(self, template: dict[str, Any]) -> list[str]:
        """Notes about the template as a whole: unknown sections and transforms."""
        notes = [
            (
                f"The top-level section '{name}' is unknown to CloudFormation, so the converter "
                "ignored it. Check the spelling if you meant a CloudFormation section."
            )
            for name in unknown_sections(template)
        ]
        for name in transforms(template):
            if name not in (SAM_TRANSFORM, LANGUAGE_EXTENSIONS):
                notes.append(
                    f"The transform '{name}' runs inside CloudFormation, so the converter cannot "
                    "apply it. Convert the processed template instead. Get it with `aws "
                    "cloudformation get-template --stack-name <stack> --template-stage "
                    "Processed`."
                )
        return notes

    def summary(self, resources: list[dict[str, Any]]) -> dict[str, int]:
        """How many resources went to each provider, and how many were not converted."""
        counts = {"resources": len(resources), "aws": 0, "awscc": 0, "unsupported": 0}
        for entry in resources:
            counts[entry["status"]] += 1
        return counts


def convert_template(
    text: str,
    provider: str = "aws",
    layout: str = "files",
    stack_name: str | None = None,
    include_imports: bool = False,
) -> dict[str, Any]:
    """Convert template text with the given options. See TemplateConverter."""
    return TemplateConverter(provider, layout, stack_name, include_imports).convert(text)
