"""CloudFormation, AWS SAM and AWS CDK (synthesized) templates to Terraform.

Example:
    result = convert_template(open("template.yaml").read())
    result["files"]      # [{"path": "main.tf", "content": "..."}, ...]
    result["notes"]      # what needs a human decision
    result["resources"]  # each CloudFormation resource and where it went
    result["source"]     # "cloudformation", "sam" or "cdk"
"""

from __future__ import annotations

from typing import Any

from converters.cfn.cdk import prepare_cdk
from converters.cfn.convert import LAYOUTS, PROVIDERS, Converter
from converters.cfn.parse import (
    LANGUAGE_EXTENSIONS,
    MAX_TEMPLATE_CHARS,
    SAM_TRANSFORM,
    CfnError,
    detect_source,
    load_template,
    transforms,
    unknown_sections,
)
from converters.cfn.sam import expand_sam


def convert_template(
    text: str,
    provider: str = "aws",
    layout: str = "files",
    stack_name: str | None = None,
    include_imports: bool = False,
) -> dict[str, Any]:
    """Convert template text (JSON or YAML) to Terraform files.

    provider: "aws" (hand-written mappings, awscc for the rest) or "awscc" (awscc only).
    layout: "files" (versions.tf, variables.tf, main.tf, ...) or "single" (main.tf).

    Raises CfnError for input that can't be converted.
    """
    if provider not in PROVIDERS:
        raise CfnError(f"Unknown provider '{provider}'. Use one of: {', '.join(PROVIDERS)}")
    if layout not in LAYOUTS:
        raise CfnError(f"Unknown layout '{layout}'. Use one of: {', '.join(LAYOUTS)}")
    template = load_template(text)
    source = detect_source(template)
    notes: list[str] = []
    for name in unknown_sections(template):
        notes.append(
            f"Top-level section '{name}' is not part of a CloudFormation template; ignored"
        )
    for name in transforms(template):
        if name in (SAM_TRANSFORM, LANGUAGE_EXTENSIONS):
            continue
        notes.append(
            f"Transform '{name}' runs inside CloudFormation and was not applied; convert the "
            "template it produces instead"
        )

    names: dict[str, str] = {}
    hints: dict[str, dict[str, Any]] = {}
    if source == "sam":
        template, hints = expand_sam(template, notes)
    elif source == "cdk":
        template, names, hints = prepare_cdk(template, notes)
    if template.get("Rules"):
        notes.append(
            "The template's Rules were not converted; add the checks as variable validation "
            "blocks or preconditions"
        )

    converter = Converter(template, provider, stack_name, include_imports, names, hints)
    result = converter.run(layout)
    counts = {"aws": 0, "awscc": 0, "unsupported": 0}
    for entry in result["resources"]:
        counts[entry["status"]] += 1
    return {
        "source": source,
        "files": result["files"],
        "notes": notes + result["notes"],
        "resources": result["resources"],
        "summary": {"resources": len(result["resources"]), **counts},
    }


__all__ = ["LAYOUTS", "MAX_TEMPLATE_CHARS", "PROVIDERS", "CfnError", "convert_template"]
