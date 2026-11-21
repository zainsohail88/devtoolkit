#!/usr/bin/env bash
# Convert the CloudFormation fixtures to Terraform and check each result with
# `terraform fmt -check`, `terraform init` and `terraform validate`. Warnings count as
# failures. Each fixture is converted twice (aws provider with separate files and import
# blocks, awscc provider in one file) by services/converter-api/tools/convert_fixtures.py.
#
# Usage: scripts/validate-terraform.sh [fixture ...]   (default: every fixture)
# Needs terraform on PATH and access to the Terraform registry. Set TF_PLUGIN_CACHE_DIR to
# download each provider once. PYTHON names the interpreter (default: python3).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="${ROOT_DIR}/services/converter-api"
PYTHON="${PYTHON:-python3}"
# A relative interpreter path is relative to where the script was started.
if [[ "$PYTHON" == */* ]]; then
  PYTHON="$(cd "$(dirname "$PYTHON")" && pwd)/$(basename "$PYTHON")"
fi

OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cfn-tf-XXXXXX")"
trap 'rm -rf "$OUT_DIR"' EXIT

(cd "$SERVICE_DIR" && "$PYTHON" -m tools.convert_fixtures "$OUT_DIR" "$@")

failed=0
for dir in "$OUT_DIR"/*/*/; do
  dir="${dir%/}"
  label="$(basename "$(dirname "$dir")") ($(basename "$dir"))"
  problems=""
  if ! output="$(terraform -chdir="$dir" fmt -check -diff -no-color 2>&1)"; then
    problems+="terraform fmt:"$'\n'"${output}"$'\n'
  fi
  if ! output="$(terraform -chdir="$dir" init -backend=false -input=false -no-color 2>&1)"; then
    problems+="terraform init:"$'\n'"${output}"$'\n'
  else
    # validate exits 1 on errors; the JSON report on stdout lists errors and warnings.
    report="$(terraform -chdir="$dir" validate -json -no-color 2>"${OUT_DIR}/stderr" || true)"
    if ! output="$(cd "$SERVICE_DIR" && printf '%s' "$report" | "$PYTHON" -m tools.validate_report)"; then
      problems+="${output}"$'\n'"$(cat "${OUT_DIR}/stderr")"$'\n'
    fi
  fi
  if [[ -n "$problems" ]]; then
    echo "FAIL ${label}"
    printf '%s' "$problems" | sed 's/^/  /'
    failed=1
  else
    echo "ok   ${label}"
  fi
done
exit "$failed"
