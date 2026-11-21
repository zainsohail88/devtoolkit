"""Renders the HCL syntax tree (converters/cfn/models.py) as text that already matches
`terraform fmt`.

Values are plain Python data (None, bool, int, float, str, list, dict) plus Raw for an
expression that is already HCL, Call for a function call whose argument is rendered as HCL
and Comment for a comment line inside an object or a body.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from converters.cfn.models import BLANK, Attr, Block, Call, Comment, Raw

INDENT = "  "
# A list of plain values stays on one line when it fits in this many characters.
INLINE_WIDTH = 80
BARE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
KEYWORDS = frozenset({"true", "false", "null", "for", "in", "if"})


def quote(text: str) -> str:
    """A literal string as an HCL quoted string. `${` and `%{` are escaped so they stay text."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("${", "$${")
        .replace("%{", "%%{")
    )
    return f'"{escaped}"'


def template_text(text: str) -> str:
    """Literal text for the inside of a template string: the escapes of quote(), without
    the quotes."""
    return quote(text)[1:-1]


def key(name: str) -> str:
    """An object key: bare when it is a plain identifier, quoted otherwise."""
    if BARE_KEY.match(name) and name not in KEYWORDS:
        return name
    return quote(name)


def object_key(name: Any) -> str:
    """An object key, or `(expression)` for a key computed by an expression."""
    if isinstance(name, Raw):
        return f"({name.text})"
    return key(str(name))


def number(value: int | float) -> str:
    """A number as HCL writes it: 3 for 3.0, quoted text for infinity and NaN."""
    if isinstance(value, float):
        if not math.isfinite(value):
            return quote(str(value))
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        return repr(value)
    return str(value)


def render(value: Any, level: int = 0) -> str:
    """Render a value. Lines after the first are indented for nesting depth `level`."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return number(value)
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, Raw):
        return value.text
    if isinstance(value, Call):
        return f"{value.fn}({render(value.arg, level)})"
    if isinstance(value, list | tuple):
        return render_list(list(value), level)
    if isinstance(value, dict):
        return render_object(value, level)
    # Anything else (dates and the like from YAML) is written as its text.
    return quote(json.dumps(value, default=str).strip('"'))


def inline(value: Any) -> str:
    """Render a value on one line, for use inside a larger expression: a function argument,
    a conditional branch, a template interpolation."""
    if isinstance(value, Call):
        return f"{value.fn}({inline(value.arg)})"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(inline(item) for item in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = [
            f"{object_key(name)} = {inline(item)}"
            for name, item in value.items()
            if not isinstance(item, Comment)
        ]
        return "{ " + ", ".join(parts) + " }"
    return render(value, 0)


def render_list(items: list[Any], level: int) -> str:
    """A list on one line when it is short and flat, otherwise one item per line."""
    if not items:
        return "[]"
    parts = [render(item, level + 1) for item in items]
    one_line = "[" + ", ".join(parts) + "]"
    flat = all(not isinstance(item, dict | list | tuple) or not item for item in items)
    single = all("\n" not in part for part in parts)
    if flat and single and len(one_line) + level * 2 <= INLINE_WIDTH:
        return one_line
    pad = INDENT * (level + 1)
    lines = ["["] + [f"{pad}{part}," for part in parts] + [INDENT * level + "]"]
    return "\n".join(lines)


def render_object(mapping: dict[Any, Any], level: int) -> str:
    """An object, one `key = value` per line, aligned like a body."""
    if not mapping:
        return "{}"
    items: list[Any] = []
    for name, value in mapping.items():
        if isinstance(value, Comment):
            items.append(value)
        else:
            items.append(Attr(object_key(name), value))
    lines = ["{"] + BodyWriter(level + 1).write(items) + [INDENT * level + "}"]
    return "\n".join(lines)


def render_block(block: Block, level: int = 0) -> str:
    """A block with its labels and body."""
    pad = INDENT * level
    labels = "".join(f" {quote(label)}" for label in block.labels)
    if not block.body:
        return f"{pad}{block.type}{labels} {{}}"
    body = BodyWriter(level + 1).write(block.body)
    return "\n".join([f"{pad}{block.type}{labels} {{"] + body + [f"{pad}}}"])


def render_file(blocks: list[Block | Comment]) -> str:
    """Top-level blocks separated by blank lines, ending with a newline. A comment sits
    directly above the block that follows it."""
    out = ""
    previous_was_comment = False
    for block in blocks:
        if out:
            out += "\n" if previous_was_comment else "\n\n"
        if isinstance(block, Comment):
            out += "\n".join(f"# {line}".rstrip() for line in block.text.split("\n"))
            previous_was_comment = True
        else:
            out += render_block(block)
            previous_was_comment = False
    return out + "\n"


class BodyWriter:
    """Writes the attributes, blocks and comments of one body, with `=` aligned the way
    terraform fmt aligns it: consecutive single-line attributes form a group, anything else
    ends the group."""

    def __init__(self, level: int) -> None:
        """A body at nesting depth `level`."""
        self.level = level
        self.pad = INDENT * level
        self.lines: list[str] = []
        self.group: list[tuple[str, str]] = []

    def write(self, items: list[Any]) -> list[str]:
        """The lines of the body."""
        for item in items:
            self.add(item)
        self.flush()
        while self.lines and self.lines[-1] == "":
            self.lines.pop()
        return self.lines

    def add(self, item: Any) -> None:
        """One item of the body."""
        if isinstance(item, Attr):
            text = render(item.value, self.level)
            if "\n" in text:
                self.flush()
                self.lines.append(f"{self.pad}{item.name} = {text}")
            else:
                self.group.append((item.name, text))
        elif isinstance(item, Block):
            self.flush()
            self.lines.extend(render_block(item, self.level).split("\n"))
        elif item is BLANK:
            self.flush()
            if self.lines and self.lines[-1] != "":
                self.lines.append("")
        elif isinstance(item, Comment):
            self.flush()
            self.lines.extend(f"{self.pad}# {line}".rstrip() for line in item.text.split("\n"))
        else:
            raise TypeError(f"Unexpected body item {item!r}")

    def flush(self) -> None:
        """Write the pending group of single-line attributes, aligned on `=`."""
        if not self.group:
            return
        width = max(len(name) for name, _ in self.group)
        self.lines.extend(f"{self.pad}{name.ljust(width)} = {text}" for name, text in self.group)
        self.group = []
