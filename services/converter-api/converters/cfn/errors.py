"""The error the converter raises for input it cannot convert."""

from __future__ import annotations


class CfnError(ValueError):
    """Raised when a template cannot be read or converted.

    `message` describes the user's input and is safe to show them. `line` and `column` are
    1-based, set for syntax errors.
    """

    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        """An error about the input, at an optional position."""
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column
