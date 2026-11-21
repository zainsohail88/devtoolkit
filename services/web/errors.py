"""Exceptions of the web service."""

from __future__ import annotations


class InputError(Exception):
    """The input of the user cannot be sent to converter-api. The message is shown on the page."""
