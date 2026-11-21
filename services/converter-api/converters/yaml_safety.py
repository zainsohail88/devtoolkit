"""Safe, bounded YAML loading shared by the compose and CloudFormation converters.

Every loader here is a SafeLoader subclass, so YAML can only build the standard types
(mappings, lists, strings, numbers, booleans, null), never Python objects. The limits stop
oversized input, deep nesting and alias bombs before they cost real CPU time.
"""

from __future__ import annotations

from typing import Any

import yaml

# Upper bound on YAML values, both as written and after aliases are expanded. A real compose
# file has a few hundred; this stops oversized or "billion laughs" input from eating CPU.
MAX_NODES = 10_000
# Real compose files nest lists and mappings fewer than 10 levels deep.
MAX_DEPTH = 100

# The C parser of libyaml ships in the PyYAML wheel and is 10-100x faster than the pure-Python
# one (50,000 nested brackets: ~3ms vs ~0.3s). Fall back if PyYAML was built without it.
FAST_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

NULL_TAG = "tag:yaml.org,2002:null"
MERGE_TAG = "tag:yaml.org,2002:merge"


class YamlLimitError(ValueError):
    """Raised when YAML input is too large or nested too deeply.

    `message` is written for the user and is safe to send back to them.
    """

    def __init__(self, message: str) -> None:
        """Keep the user text as `message`."""
        super().__init__(message)
        self.message = message


class SafeMergeLoader(FAST_LOADER):
    """Safe loader whose `<<` merge keys cost linear, not exponential, time.

    PyYAML copies every merged key/value pair once per reference before de-duplicating, so
    `a2: {<<: [*a1, *a1, ...]}` chained a few levels deep grows 9x per level (a 500-char
    input can take minutes). Dropping duplicate keys after each merge keeps it linear. The
    last occurrence of a key wins, which is the same result PyYAML gives.
    """

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        """Merge the `<<` keys of `node`, then keep only the last value of each key."""
        super().flatten_mapping(node)
        seen: set[tuple[str, str]] = set()
        kept: list[tuple[yaml.Node, yaml.Node]] = []
        for key_node, value_node in reversed(node.value):
            if isinstance(key_node, yaml.ScalarNode):
                key = (key_node.tag, key_node.value)
                if key in seen:
                    continue
                seen.add(key)
            kept.append((key_node, value_node))
        kept.reverse()
        node.value = kept


class PlainScalarLoader(SafeMergeLoader):
    """Loader that keeps plain scalars as strings, except null and `<<` merge keys.

    PyYAML follows YAML 1.1, where `on` means True, `0755` is octal 493 and an unquoted
    `22:22` is the base-60 number 1342. Compose uses YAML 1.2, so those values must reach
    the container unchanged. Keeping the text as it is gives the same result, and numeric
    fields (ports, replicas, cpus) are converted explicitly by the caller.
    """


PlainScalarLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag in (NULL_TAG, MERGE_TAG)]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_single(text: str, loader_class: type[yaml.SafeLoader]) -> Any:
    """Build the single document in `text` with one of the safe loaders above.

    This is what yaml.load() does internally, spelled out so the loader is visibly a
    SafeLoader subclass.
    """
    loader = loader_class(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def load_yaml_documents(text: str) -> list[Any]:
    """Build every document in a multi-document YAML string (separated by ---).

    Uses SafeMergeLoader: safe types only, and linear-time `<<` merges. Raises yaml.YAMLError
    on syntax errors.
    """
    loader = SafeMergeLoader(text)
    try:
        documents: list[Any] = []
        while loader.check_data():
            documents.append(loader.get_data())
        return documents
    finally:
        loader.dispose()


def check_yaml_limits(text: str, max_depth: int = MAX_DEPTH, max_nodes: int = MAX_NODES) -> None:
    """Raise YamlLimitError if YAML nests deeper than `max_depth` or has over `max_nodes`
    values.

    Building objects is the expensive part of loading YAML, and it recurses, so `[[[[...]]]]`
    thousands deep or a 60 KB list of 30,000 items costs about a second of CPU. The parser's
    event stream is flat and runs in C, so checking it first rejects both in milliseconds.
    Aliases are counted once here; check_node_count() covers their expanded size.

    Raises yaml.YAMLError for syntax errors, so callers can report them as usual.
    """
    depth = 0
    nodes = 0
    for event in yaml.parse(text, Loader=FAST_LOADER):
        if isinstance(event, yaml.NodeEvent):
            nodes += 1
            if nodes > max_nodes:
                raise YamlLimitError(f"Input too large: more than {max_nodes} values")
        if isinstance(event, yaml.CollectionStartEvent):
            depth += 1
            if depth > max_depth:
                raise YamlLimitError("Invalid YAML: nested too deeply")
        elif isinstance(event, yaml.CollectionEndEvent):
            depth -= 1


def check_node_count(data: Any, limit: int = MAX_NODES) -> None:
    """Raise YamlLimitError if `data` has more than `limit` nodes once aliases are expanded.

    Stops counting as soon as the limit is passed, so even a huge alias bomb is rejected fast.
    """
    count = 0
    stack = [data]
    while stack:
        node = stack.pop()
        count += 1
        if count > limit:
            raise YamlLimitError(
                f"Input too large: more than {limit} values after expanding YAML aliases"
            )
        if isinstance(node, dict):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
