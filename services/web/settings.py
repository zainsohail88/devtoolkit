"""Settings of the web service: paths, input limits, accepted form values and log levels."""

from __future__ import annotations

import re
from pathlib import Path

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Where converter-api runs and how to authenticate to it, read from the environment.
API_URL_VARIABLE = "CONVERTER_API_URL"
API_URL_DEFAULT = "http://localhost:8000"
API_AUTH_VARIABLE = "CONVERTER_API_AUTH"
API_AUTH_DEFAULT = "none"

LOG_LEVEL_VARIABLE = "LOG_LEVEL"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOGGER_NAME = "web"

# Same limits as converter-api, checked here first so users get a clear message.
MAX_INPUT_CHARS = 100_000
MAX_TEMPLATE_CHARS = 500_000
# Room for the form fields and multipart framing around the largest input.
MAX_BODY_BYTES = 4 * MAX_TEMPLATE_CHARS + 16_384
# An upload may hold up to four bytes per character.
BYTES_PER_CHAR = 4

NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
STACK_NAME_RE = re.compile(r"^[A-Za-z][-A-Za-z0-9]{0,127}$")
OUTPUTS = ("manifests", "helm")
PROVIDERS = ("aws", "awscc")
LAYOUTS = ("files", "single")

# converter-api paths.
COMPOSE_API_PATH = "/convert/compose-to-k8s/files"
CFN_API_PATH = "/convert/cfn-to-terraform"
VALIDATE_API_PATH = "/validate/yaml"

# How the page script asks for the result markup alone.
FRAGMENT_HEADER = "x-requested-with"
FRAGMENT_VALUE = "fetch"

SOURCE_LABELS = {
    "cloudformation": "CloudFormation",
    "sam": "AWS SAM",
    "cdk": "AWS CDK (synthesized)",
}
DEFAULT_SOURCE = "cloudformation"

# Zip downloads.
HELM_ZIP_NAME = "helm-chart.zip"
MANIFESTS_ZIP_NAME = "kubernetes-manifests.zip"
TERRAFORM_ZIP_NAME = "terraform.zip"
NOTES_FILE_NAME = "CONVERSION_NOTES.md"

# Templates.
INDEX_PAGE = "index.html"
ERROR_PAGE = "error.html"
COMPOSE_PAGE = "compose_to_k8s.html"
COMPOSE_RESULT = "_result.html"
CFN_PAGE = "cfn_to_terraform.html"
CFN_RESULT = "_terraform_result.html"
YAML_PAGE = "validate_yaml.html"
YAML_RESULT = "_validation.html"
