"""Checks the house rules that ruff has no setting for. Run by `make lint` and CI.

Python, every file:
  - no function or lambda defined inside another function, and no lambdas at all
  - no `*` or `**` syntax: star imports, *args, **kwargs, keyword-only markers, unpacking
  - imports only at the top of the module, before any other statement
  - no comments that silence a checker: noqa, type: ignore, pragma: no cover, nosec...
Python, outside tests/:
  - a docstring on every module, class and function, and a return type on every function
Markdown (README.md, CONTRIBUTING.md, SECURITY.md, docs/):
  - no apostrophes, no comma before "and" or "or", no em dashes (code is left alone)

Usage: python scripts/check_code_rules.py [path ...]   (default: the whole repository)
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIPPED_DIRECTORIES = frozenset({".venv", "node_modules", ".git", "__pycache__", "vendor"})
MARKDOWN_FILES = ("README.md", "CONTRIBUTING.md", "SECURITY.md")
MARKDOWN_DIRECTORY = "docs"
SILENCERS = re.compile(
    r"#\s*(noqa|type:\s*ignore|pragma:\s*no\s*cover|nosec|fmt:\s*(off|skip)|pylint:\s*disable)",
    re.IGNORECASE,
)
FENCED_CODE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
LINK_TARGET = re.compile(r"\]\([^)]*\)")
MARKDOWN_RULES = (
    (re.compile(r"['’]"), "apostrophe"),
    (re.compile(r",\s+(and|or)\b"), "comma before 'and' or 'or'"),
    (re.compile("—"), "em dash"),
)


@dataclass(frozen=True)
class Problem:
    """One broken rule at one place."""

    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        """`path:line: message`, relative to the repository."""
        return f"{self.path.relative_to(ROOT)}:{self.line}: {self.message}"


class PythonRules(ast.NodeVisitor):
    """Walks one Python module and collects the rules it breaks."""

    def __init__(self, path: Path, source: str) -> None:
        """Prepare to check `source`, read from `path`."""
        self.path = path
        self.source = source
        self.problems: list[Problem] = []
        self.function_depth = 0
        self.is_test = "tests" in path.parts

    def check(self) -> list[Problem]:
        """Every problem in the module."""
        tree = ast.parse(self.source, filename=str(self.path))
        self.check_imports(tree)
        self.check_silencers()
        if not self.is_test and ast.get_docstring(tree) is None:
            self.add(1, "module has no docstring")
        self.visit(tree)
        return self.problems

    def add(self, line: int, message: str) -> None:
        """Record a problem."""
        self.problems.append(Problem(self.path, line, message))

    def check_imports(self, tree: ast.Module) -> None:
        """Imports come first: only a docstring may precede them, and none come later."""
        body = list(tree.body)
        if body and ast.get_docstring(tree) is not None:
            body = body[1:]
        seen_code = False
        for statement in body:
            is_import = isinstance(statement, ast.Import | ast.ImportFrom)
            if is_import and seen_code:
                self.add(statement.lineno, "import after other statements")
            if not is_import:
                seen_code = True
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom) and node not in tree.body:
                self.add(node.lineno, "import inside a function, class or block")

    def check_silencers(self) -> None:
        """No comment may switch a checker off."""
        for number, line in enumerate(self.source.splitlines(), start=1):
            if SILENCERS.search(line):
                self.add(number, "comment that silences a checker")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Functions: not nested, documented, typed, no star parameters."""
        self.check_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Async functions follow the same rules as functions."""
        self.check_function(node)

    def check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """The rules for one function or method, then its body."""
        if self.function_depth:
            self.add(node.lineno, f"function '{node.name}' defined inside another function")
        arguments = node.args
        if arguments.vararg or arguments.kwarg or arguments.kwonlyargs:
            self.add(node.lineno, f"'{node.name}' uses * or ** in its parameters")
        if not self.is_test:
            if ast.get_docstring(node) is None:
                self.add(node.lineno, f"function '{node.name}' has no docstring")
            if node.returns is None:
                self.add(node.lineno, f"function '{node.name}' has no return type")
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Classes are documented outside tests."""
        if not self.is_test and ast.get_docstring(node) is None:
            self.add(node.lineno, f"class '{node.name}' has no docstring")
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """No lambdas: name the function."""
        self.add(node.lineno, "lambda")
        self.generic_visit(node)

    def visit_Starred(self, node: ast.Starred) -> None:
        """No `*` unpacking."""
        self.add(node.lineno, "* unpacking")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """No `**` in calls."""
        for keyword in node.keywords:
            if keyword.arg is None:
                self.add(node.lineno, "** unpacking in a call")
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        """No `**` in dict literals."""
        if any(key is None for key in node.keys):
            self.add(node.lineno, "** unpacking in a dict")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """No star imports."""
        if any(alias.name == "*" for alias in node.names):
            self.add(node.lineno, "star import")


class MarkdownRules:
    """Checks the writing rules on one Markdown file, outside code."""

    def __init__(self, path: Path, text: str) -> None:
        """Prepare to check `text`, read from `path`."""
        self.path = path
        self.text = text

    def prose(self) -> str:
        """The text with code blocks, inline code and link targets replaced by filler of the
        same length, keeping line numbers. The filler is letters, so `a`, `b` and c still
        reads as a list."""
        text = FENCED_CODE.sub(self.blank, self.text)
        text = INLINE_CODE.sub(self.blank, text)
        return LINK_TARGET.sub(self.blank, text)

    @staticmethod
    def blank(match: re.Match[str]) -> str:
        """The matched text with everything but newlines replaced by x."""
        return re.sub(r"[^\n]", "x", match.group(0))

    def check(self) -> list[Problem]:
        """Every problem in the file."""
        problems: list[Problem] = []
        for number, line in enumerate(self.prose().splitlines(), start=1):
            for pattern, message in MARKDOWN_RULES:
                if pattern.search(line):
                    problems.append(Problem(self.path, number, message))
        return problems


class Repository:
    """Finds the files to check."""

    def __init__(self, root: Path) -> None:
        """A repository rooted at `root`."""
        self.root = root

    def python_files(self) -> list[Path]:
        """Every Python file outside virtual environments and build output."""
        return sorted(path for path in self.root.rglob("*.py") if self.included(path))

    def markdown_files(self) -> list[Path]:
        """The top-level documents and everything in docs/."""
        files = [self.root / name for name in MARKDOWN_FILES if (self.root / name).exists()]
        return files + sorted((self.root / MARKDOWN_DIRECTORY).rglob("*.md"))

    def included(self, path: Path) -> bool:
        """Whether `path` is outside the skipped directories."""
        return not SKIPPED_DIRECTORIES.intersection(path.relative_to(self.root).parts)


class Checker:
    """Runs every rule on a set of files."""

    def __init__(self, paths: list[Path]) -> None:
        """Check `paths`, or the whole repository when it is empty."""
        repository = Repository(ROOT)
        if paths:
            self.python = [path for path in paths if path.suffix == ".py"]
            self.markdown = [path for path in paths if path.suffix == ".md"]
        else:
            self.python = repository.python_files()
            self.markdown = repository.markdown_files()

    def run(self) -> list[Problem]:
        """Every problem found."""
        problems: list[Problem] = []
        for path in self.python:
            problems.extend(PythonRules(path, path.read_text(encoding="utf-8")).check())
        for path in self.markdown:
            problems.extend(MarkdownRules(path, path.read_text(encoding="utf-8")).check())
        return problems


def main(arguments: list[str]) -> int:
    """Print every problem; exit status 1 when there is any."""
    problems = Checker([Path(argument).resolve() for argument in arguments]).run()
    for problem in problems:
        print(problem)
    print(f"{len(problems)} problem(s)" if problems else "Code rules: all checks passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
