"""Read the output of `terraform validate -json` on stdin and print each diagnostic.

Exits 1 when there is any diagnostic (warnings count as failures) or the output is not
JSON, so scripts/validate-terraform.sh can fail on it. Run from services/converter-api:

    terraform validate -json -no-color | python -m tools.validate_report
"""

from __future__ import annotations

import json
import sys
from typing import Any


def describe(diagnostic: dict[str, Any]) -> str:
    """One diagnostic as severity, summary, file:line and detail."""
    where = diagnostic.get("range", {})
    position = f"{where.get('filename', '?')}:{where.get('start', {}).get('line', '?')}"
    return (
        f"{diagnostic['severity']}: {diagnostic['summary']} ({position})\n"
        f"  {diagnostic.get('detail', '')}"
    )


def problems(text: str) -> list[str]:
    """Every problem in the validate output `text` (empty when the configuration is valid)."""
    try:
        report = json.loads(text)
    except ValueError:
        return [f"terraform validate:\n{text}"]
    return [describe(diagnostic) for diagnostic in report.get("diagnostics", [])]


def main() -> int:
    """Print the problems in the report on stdin. Returns 1 when there are any."""
    found = problems(sys.stdin.read())
    for problem in found:
        print(problem)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
