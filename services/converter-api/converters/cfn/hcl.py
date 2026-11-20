"""A small HCL model and a renderer whose output already matches `terraform fmt`.

Values are plain Python data (None, bool, int, float, str, list, dict) plus three wrappers:
Raw for an expression that is already HCL (`var.name`, `"${var.a}-x"`), Call for a function
call whose argument is rendered as HCL (`jsonencode({...})`), and Comment for a comment line
inside an object or a body.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

INDENT = "  "
# A list of plain values stays on one line when it fits in this many characters.
_INLINE_WIDTH = 80
_BARE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_KEYWORDS = frozenset({"true", "false", "null", "for", "in", "if"})


@dataclass(frozen=True)
class Raw:
    """An HCL expression, written exactly as given."""

    text: str


@dataclass(frozen=True)
class Call:
    """`fn(arg)`, with `arg` rendered as an HCL value (so it can span several lines)."""

    fn: str
    arg: Any


@dataclass(frozen=True)
class Comment:
    """A `#` comment line in a body or an object."""

    text: str


@dataclass
class Attr:
    name: str
    value: Any


@dataclass
class Block:
    type: str
    labels: list[str] = field(default_factory=list)
    body: list[Any] = field(default_factory=list)


BLANK = Comment("\0blank")


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
    """Literal text for the inside of a template string (same escapes as quote, no quotes)."""
    return quote(text)[1:-1]


def key(name: str) -> str:
    """An object key: bare when it is a plain identifier, quoted otherwise."""
    if _BARE_KEY.match(name) and name not in _KEYWORDS:
        return name
    return quote(name)


def _number(value: int | float) -> str:
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
        return _number(value)
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, Raw):
        return value.text
    if isinstance(value, Call):
        return f"{value.fn}({render(value.arg, level)})"
    if isinstance(value, list | tuple):
        return _render_list(list(value), level)
    if isinstance(value, dict):
        return _render_object(value, level)
    # Anything else (dates and the like from YAML) is written as its text.
    return quote(json.dumps(value, default=str).strip('"'))


def inline(value: Any) -> str:
    """Render a value on one line, for use inside a larger expression (a function argument,
    a conditional branch, a template interpolation)."""
    if isinstance(value, Call):
        return f"{value.fn}({inline(value.arg)})"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(inline(item) for item in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = [
            f"{_object_key(name)} = {inline(item)}"
            for name, item in value.items()
            if not isinstance(item, Comment)
        ]
        return "{ " + ", ".join(parts) + " }"
    return render(value, 0)


def _object_key(name: Any) -> str:
    if isinstance(name, Raw):
        return f"({name.text})"
    return key(str(name))


def _render_list(items: list[Any], level: int) -> str:
    if not items:
        return "[]"
    parts = [render(item, level + 1) for item in items]
    one_line = "[" + ", ".join(parts) + "]"
    if all("\n" not in p for p in parts) and len(one_line) + level * 2 <= _INLINE_WIDTH:
        if all(not isinstance(item, dict | list | tuple) or not item for item in items):
            return one_line
    pad = INDENT * (level + 1)
    lines = ["["] + [f"{pad}{part}," for part in parts] + [INDENT * level + "]"]
    return "\n".join(lines)


def _render_object(mapping: dict[str, Any], level: int) -> str:
    if not mapping:
        return "{}"
    items: list[Any] = []
    for name, value in mapping.items():
        if isinstance(value, Comment):
            items.append(value)
        else:
            items.append(Attr(_object_key(name), value))
    lines = ["{", *_render_items(items, level + 1), INDENT * level + "}"]
    return "\n".join(lines)


def _render_items(items: list[Any], level: int) -> list[str]:
    """Attributes, blocks and comments of one body, with `=` aligned like terraform fmt:
    consecutive single-line attributes form a group; anything else ends the group."""
    pad = INDENT * level
    lines: list[str] = []
    group: list[tuple[str, str]] = []

    def flush() -> None:
        if not group:
            return
        width = max(len(name) for name, _ in group)
        lines.extend(f"{pad}{name.ljust(width)} = {text}" for name, text in group)
        group.clear()

    for item in items:
        if isinstance(item, Attr):
            name = item.name
            text = render(item.value, level)
            if "\n" in text:
                flush()
                lines.append(f"{pad}{name} = {text}")
            else:
                group.append((name, text))
        elif isinstance(item, Block):
            flush()
            lines.extend(render_block(item, level).split("\n"))
        elif item is BLANK:
            flush()
            if lines and lines[-1] != "":
                lines.append("")
        elif isinstance(item, Comment):
            flush()
            lines.extend(f"{pad}# {line}".rstrip() for line in item.text.split("\n"))
        else:  # pragma: no cover - a programming error, not an input error
            raise TypeError(f"Unexpected body item {item!r}")
    flush()
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def render_block(block: Block, level: int = 0) -> str:
    pad = INDENT * level
    labels = "".join(f" {quote(label)}" for label in block.labels)
    if not block.body:
        return f"{pad}{block.type}{labels} {{}}"
    body = _render_items(block.body, level + 1)
    return "\n".join([f"{pad}{block.type}{labels} {{", *body, f"{pad}}}"])


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
