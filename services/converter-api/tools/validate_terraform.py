"""Convert every template in tests/fixtures/cfn to Terraform and check the result with
`terraform fmt -check` and `terraform validate` (warnings count as failures).

Each fixture is converted twice: with the aws provider (separate files, import blocks) and
with the awscc provider only (one file). Needs `terraform` on PATH and access to the
Terraform registry. Set TF_PLUGIN_CACHE_DIR to download each provider once.

    python tools/validate_terraform.py                 # every fixture
    python tools/validate_terraform.py network.yaml    # some of them
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from converters.cfn import convert_template  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "cfn"
RUNS = (
    {"provider": "aws", "layout": "files", "include_imports": True, "stack_name": "demo"},
    {"provider": "awscc", "layout": "single", "stack_name": "demo"},
)
# Local paths the configuration reads (Lambda code, API definitions): validate wants them.
_LOCAL_PATH = re.compile(r'"\$\{path\.module\}/([^"$]+)"')


def _terraform(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed command, our own arguments
        ["terraform", *args],  # noqa: S607 - terraform from PATH, as a user would run it
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )


def _write(result: dict, directory: Path) -> None:
    for file in result["files"]:
        (directory / file["path"]).write_text(file["content"], encoding="utf-8")
    text = "\n".join(file["content"] for file in result["files"])
    for path in set(_LOCAL_PATH.findall(text)):
        target = directory / path
        if target.suffix in (".zip", ".jar", ".yaml", ".yml", ".json"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}\n", encoding="utf-8")
        elif not path.startswith("build/"):
            target.mkdir(parents=True, exist_ok=True)
            (target / "index.py").write_text("def handler(event, context):\n    pass\n")


def check(fixture: Path, options: dict) -> list[str]:
    """Problems found in one conversion of one fixture (empty when it is valid)."""
    result = convert_template(fixture.read_text(encoding="utf-8"), **options)
    with tempfile.TemporaryDirectory(prefix="cfn-tf-") as tmp:
        directory = Path(tmp)
        _write(result, directory)
        problems = []
        fmt = _terraform(directory, "fmt", "-check", "-diff", "-no-color")
        if fmt.returncode != 0:
            problems.append(f"terraform fmt:\n{fmt.stdout}{fmt.stderr}")
        init = _terraform(directory, "init", "-backend=false", "-input=false", "-no-color")
        if init.returncode != 0:
            return problems + [f"terraform init:\n{init.stdout}{init.stderr}"]
        validate = _terraform(directory, "validate", "-json", "-no-color")
        try:
            report = json.loads(validate.stdout)
        except ValueError:
            return problems + [f"terraform validate:\n{validate.stdout}{validate.stderr}"]
        for diagnostic in report.get("diagnostics", []):
            where = diagnostic.get("range", {})
            position = f"{where.get('filename', '?')}:{where.get('start', {}).get('line', '?')}"
            problems.append(
                f"{diagnostic['severity']}: {diagnostic['summary']} ({position})\n"
                f"  {diagnostic.get('detail', '')}"
            )
        return problems


def main(names: list[str]) -> int:
    fixtures = [FIXTURES / name for name in names] if names else sorted(FIXTURES.iterdir())
    failed = 0
    for fixture in fixtures:
        for options in RUNS:
            label = f"{fixture.name} ({options['provider']})"
            problems = check(fixture, options)
            print(("FAIL " if problems else "ok   ") + label, flush=True)
            for problem in problems:
                print("  " + problem.replace("\n", "\n  "))
            failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
