"""Text shown to users: error messages on the pages and the notes file in the Terraform zip."""

from __future__ import annotations

UNAVAILABLE = "The converter is unavailable right now. Please try again in a minute."
NOT_UTF8 = "The uploaded file is not UTF-8 text."
EMPTY_YAML = "Paste some YAML or choose a file to upload."
EMPTY_TEMPLATE = "Paste a template or choose a file to upload."
TOO_LONG = "The input is longer than {limit:,} characters."
BAD_NAMESPACE = (
    "Namespace must be lowercase letters, digits and hyphens, at most 63 characters, "
    "starting and ending with a letter or digit."
)
BAD_OUTPUT = "Output must be manifests or helm."
BAD_STACK_NAME = (
    "Stack name must start with a letter and contain only letters, digits and hyphens, "
    "at most 128 characters."
)
BAD_PROVIDER = "Provider must be aws or awscc."
BAD_LAYOUT = "Layout must be files or single."
CONVERTER_FAILED = "The converter could not process this input."
VALIDATOR_FAILED = "The validator could not process this input."

NOT_FOUND = "There is no page at this address."
METHOD_NOT_ALLOWED = "That request method is not allowed here."
BAD_FORM = "The form was not filled in correctly."
HTTP_ERROR_MESSAGES = {404: NOT_FOUND, 405: METHOD_NOT_ALLOWED}

LENGTH_REQUIRED = "Content-Length required"
TOO_LARGE = "Request too large"

UNKNOWN_LOG_LEVEL = 'Unknown LOG_LEVEL "{value}": using info. Allowed: {allowed}'

NOTES_TITLE = "# Conversion notes"
NOTES_INTRO = (
    "Converted from {source} by DevToolkit. The items below need a decision; "
    "everything else was converted."
)
NOTES_NONE = "- Nothing: every resource was converted."
