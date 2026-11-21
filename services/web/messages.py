"""Text shown to users: error messages on the pages and the notes file in the Terraform zip."""

from __future__ import annotations

UNAVAILABLE = "The converter is unavailable right now. Please try again in a minute."
NOT_UTF8 = "DevToolkit reads UTF-8 text files. Save the file as UTF-8 and upload it again."
EMPTY_YAML = "Paste some YAML or choose a file to upload."
EMPTY_TEMPLATE = "Paste a template or choose a file to upload."
TOO_LONG = "This is too long to convert. The limit is {limit:,} characters."
BAD_NAMESPACE = (
    "Use a namespace of up to 63 lowercase letters, digits and hyphens "
    "that starts and ends with a letter or digit."
)
BAD_OUTPUT = "Choose Manifests or Helm chart as the output."
BAD_STACK_NAME = (
    "Use a stack name of up to 128 letters, digits and hyphens that starts with a letter."
)
BAD_PROVIDER = "Choose aws or awscc as the Terraform provider."
BAD_LAYOUT = "Choose split files or one main.tf as the file layout."
CONVERTER_FAILED = (
    "Something went wrong while converting this file. Try again. "
    "If it keeps happening, please open an issue on GitHub."
)
VALIDATOR_FAILED = (
    "Something went wrong while checking this file. Try again. "
    "If it keeps happening, please open an issue on GitHub."
)

NOT_FOUND = "We could not find that page."
METHOD_NOT_ALLOWED = "This address only works through the form on its page."
BAD_FORM = "Some fields are missing or invalid. Check the form and try again."
HTTP_ERROR_MESSAGES = {404: NOT_FOUND, 405: METHOD_NOT_ALLOWED}

LENGTH_REQUIRED = "Content-Length required"
TOO_LARGE = "The request is too large"

UNKNOWN_LOG_LEVEL = 'LOG_LEVEL "{value}" is unknown, so the service logs at info. Use one of: {allowed}.'

NOTES_TITLE = "# Conversion notes"
NOTES_INTRO = (
    "DevToolkit converted this {source} template to Terraform. "
    "Review these items before you apply it."
)
NOTES_NONE = "- Nothing to review. Every resource converted as it is."
