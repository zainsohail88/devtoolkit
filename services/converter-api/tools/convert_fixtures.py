"""Convert the CloudFormation fixtures in tests/fixtures/cfn into Terraform directories,
which scripts/validate-terraform.sh then checks with terraform fmt, init and validate.

Each fixture is converted twice: with the aws provider (separate files and import blocks)
and with the awscc provider only (one file, stack name demo). Local paths the configuration
reads (Lambda code, API definitions) are created as dummy files and folders, so validate
finds them. Writes OUTDIR/<fixture>/aws and OUTDIR/<fixture>/awscc.

Run from services/converter-api:

    python -m tools.convert_fixtures OUTDIR                 # every fixture
    python -m tools.convert_fixtures OUTDIR network.yaml    # some of them
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from converters.cfn.pipeline import convert_template

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "cfn"
# Local paths the configuration reads (Lambda code, API definitions): validate wants them.
LOCAL_PATH = re.compile(r'"\$\{path\.module\}/([^"$]+)"')
# Paths with these suffixes are files; any other path is a code folder.
FILE_SUFFIXES = (".zip", ".jar", ".yaml", ".yml", ".json")
# archive_file writes its zip under build/, so that folder is left for Terraform.
OUTPUT_PREFIX = "build/"
HANDLER = "def handler(event, context):\n    pass\n"


def convert(fixture: Path, provider: str) -> dict[str, Any]:
    """One conversion of one fixture: aws with separate files and import blocks, or awscc
    in one file."""
    text = fixture.read_text(encoding="utf-8")
    if provider == "aws":
        return convert_template(
            text, provider="aws", layout="files", stack_name="demo", include_imports=True
        )
    return convert_template(text, provider="awscc", layout="single", stack_name="demo")


def write(result: dict[str, Any], directory: Path) -> None:
    """Write the Terraform files of `result`, and dummies for the local paths they read."""
    directory.mkdir(parents=True, exist_ok=True)
    for file in result["files"]:
        (directory / file["path"]).write_text(file["content"], encoding="utf-8")
    text = "\n".join(file["content"] for file in result["files"])
    for path in sorted(set(LOCAL_PATH.findall(text))):
        target = directory / path
        if target.suffix in FILE_SUFFIXES:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}\n", encoding="utf-8")
        elif not path.startswith(OUTPUT_PREFIX):
            target.mkdir(parents=True, exist_ok=True)
            (target / "index.py").write_text(HANDLER, encoding="utf-8")


def main(arguments: list[str]) -> int:
    """Convert the fixtures named in `arguments[1:]` (all when none) into `arguments[0]`."""
    if not arguments:
        print(__doc__, file=sys.stderr)
        return 2
    outdir = Path(arguments[0])
    names = arguments[1:]
    fixtures = [FIXTURES / name for name in names] if names else sorted(FIXTURES.iterdir())
    for fixture in fixtures:
        for provider in ("aws", "awscc"):
            write(convert(fixture, provider), outdir / fixture.name / provider)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
