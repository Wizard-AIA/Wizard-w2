"""Static analysis gate for model-generated Python.

This is the single authority on whether generated code may run. It replaces the
previous split between a regex denylist and an inline AST walk in the agent,
which disagreed with each other (``open()`` was excluded from one because the
other handled it) and could only be applied on one of the two execution paths.

Design notes
------------
* AST-based, not regex-based. Regex matching on source text produced false
  positives on ordinary analysis code -- a column literally named ``"os"`` or a
  DataFrame column ``requests.`` inside a string tripped the old denylist.
* The sandbox container is the security boundary; this layer is defence in depth
  that also catches the *un*sandboxed paths (semantic cleaning, Docker-less
  fallback) which previously had no checks at all.
* Syntax errors are reported as a distinct verdict so the caller can route them
  into the self-correction loop instead of treating them as an attack.
"""

from __future__ import annotations

import ast
import posixpath
from dataclasses import dataclass, field


# Modules that give direct process, filesystem or network control.
BANNED_MODULES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "socket",
        "ctypes",
        "importlib",
        "imp",
        "pty",
        "signal",
        "multiprocessing",
        "threading",
        "asyncio",
        "pickle",
        "marshal",
        "shelve",
        "dill",
        "requests",
        "urllib",
        "urllib2",
        "urllib3",
        "http",
        "ftplib",
        "telnetlib",
        "smtplib",
        "paramiko",
        "webbrowser",
        "resource",
        "pwd",
        "grp",
        "tempfile",
    }
)

# Builtins that turn data into executable code or reach around the sandbox.
BANNED_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "memoryview",
        "exit",
        "quit",
    }
)

# Bare names that expose the interpreter regardless of how they are used.
BANNED_NAMES = frozenset({"__builtins__", "__loader__", "__spec__", "__debug__"})

# Reflection helpers. Safe with a literal attribute name, dangerous with a
# computed one: `getattr(__builtins__, 'ev' + 'al')` reconstructs a banned call
# out of fragments that individually look harmless.
REFLECTION_CALLS = frozenset({"getattr", "setattr", "delattr", "hasattr"})

# Attribute names used to walk from a harmless object to the interpreter internals.
BANNED_ATTRIBUTES = frozenset(
    {
        "__subclasses__",
        "__bases__",
        "__base__",
        "__mro__",
        "__globals__",
        "__code__",
        "__closure__",
        "__builtins__",
        "__loader__",
        "__reduce__",
        "__reduce_ex__",
        # `open.__self__` reaches the builtins module, and `__dict__` walks an
        # object's namespace; both are standard first hops out of a sandbox.
        "__self__",
        "__dict__",
        "__func__",
        "__wrapped__",
        "__getattribute__",
        "__init_subclass__",
        "system",
        "popen",
        "spawn",
        "fork",
        "execv",
        "execve",
        "kill",
    }
)

# Roots the generated code may read from / write to inside a container. The
# local subprocess runtime works out of the session's own directory instead, so
# callers pass that in as an extra root -- see `CodeGuard.scan(extra_roots=...)`.
ALLOWED_PATH_ROOTS = ("/workspace", "/tmp/wizard")

# Functions whose first positional argument is a filesystem path we must check.
PATH_ARG_FUNCTIONS = frozenset({"open", "read_csv", "read_parquet", "read_feather", "read_json", "read_excel"})
PATH_ARG_METHODS = frozenset(
    {
        "to_csv",
        "to_parquet",
        "to_feather",
        "to_json",
        "to_excel",
        "to_pickle",
        "savefig",
        "write_html",
        "write_image",
        "write_json",
    }
)


@dataclass
class GuardVerdict:
    """Outcome of a scan.

    ``ok`` means the code may execute. ``syntax_error`` distinguishes malformed
    output (retryable, feed the message back to the model) from a policy
    violation (not retryable, surface to the user).
    """

    ok: bool
    reason: str = ""
    syntax_error: bool = False
    violations: list[str] = field(default_factory=list)
    #: Literal paths the code wanted that lie outside the allowed roots, carried
    #: structurally rather than only inside a sentence. The permission layer has
    #: to know *which* paths to ask about, and reading them back out of an error
    #: message would make the message's wording load-bearing.
    paths: list[str] = field(default_factory=list)

    @property
    def retryable(self) -> bool:
        return self.syntax_error

    @property
    def only_paths(self) -> bool:
        """True when every violation is a path the user could legitimately allow.

        A banned import or a reflection escape is never up for negotiation; a
        write to a directory the user owns is. Only the second kind is worth
        asking about, and mixing them would let one consent prompt wave through
        an unrelated violation that happened to be in the same program.
        """
        return not self.ok and not self.syntax_error and bool(self.paths) and len(self.violations) == len(self.paths)


def _is_path_allowed(raw_path: str, roots: tuple[str, ...] = ALLOWED_PATH_ROOTS) -> bool:
    """True when ``raw_path`` resolves inside one of ``roots``.

    Relative paths resolve against the first root, which is the runtime's own
    working directory. ``posixpath`` is used explicitly so the decision does not
    change when the backend runs on Windows -- but backslashes are folded to
    forward slashes first, because a local runtime there really does hand out
    ``C:/...`` paths and a drive letter is not ``posixpath.isabs``.
    """
    if not raw_path:
        return False
    # Reject URL-ish targets outright (file://, http://, \\host\share).
    if "://" in raw_path or raw_path.startswith("\\\\"):
        return False

    cleaned = raw_path.replace("\\", "/")
    base = roots[0] if roots else "/workspace"
    rooted = posixpath.isabs(cleaned) or cleaned[1:3] == ":/"
    normalised = posixpath.normpath(cleaned if rooted else posixpath.join(base, cleaned))
    return any(normalised == root.rstrip("/") or normalised.startswith(root.rstrip("/") + "/") for root in roots)


class CodeGuard:
    """Scans generated Python and decides whether it is safe to execute."""

    @classmethod
    def scan(cls, code: str, extra_roots: tuple[str, ...] = ()) -> GuardVerdict:
        """Scans ``code``. ``extra_roots`` widens the writable path allowlist.

        The local runtime works out of the session's directory rather than
        ``/workspace``, and without this every absolute path the model wrote --
        including the chart path the prompt itself gave it -- would be rejected.
        """
        if not code or not code.strip():
            return GuardVerdict(ok=False, reason="Code generation produced an empty program.")

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            line = exc.lineno if exc.lineno is not None else "?"
            return GuardVerdict(
                ok=False,
                reason=f"Syntax Error: {exc.msg} on line {line}",
                syntax_error=True,
            )

        roots = tuple(dict.fromkeys((*extra_roots, *ALLOWED_PATH_ROOTS)))
        violations: list[str] = []
        paths: list[str] = []
        for node in ast.walk(tree):
            found, offending = cls._inspect_node(node, roots)
            violations.extend(found)
            paths.extend(offending)

        if violations:
            return GuardVerdict(ok=False, reason=violations[0], violations=violations, paths=paths)
        return GuardVerdict(ok=True, reason="Safe")

    # ------------------------------------------------------------------ #
    @classmethod
    def _inspect_node(cls, node: ast.AST, roots: tuple[str, ...] = ALLOWED_PATH_ROOTS) -> tuple[list[str], list[str]]:
        """Returns (violations, offending literal paths) for one node."""
        if isinstance(node, ast.Import):
            return [
                f"Import of restricted module '{alias.name}' is not permitted."
                for alias in node.names
                if alias.name.split(".")[0] in BANNED_MODULES
            ], []

        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_MODULES:
                return [f"Import from restricted module '{node.module}' is not permitted."], []
            return [], []

        if isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRIBUTES:
                return [f"Access to restricted attribute '{node.attr}' is not permitted."], []
            return [], []

        if isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                return [f"Reference to '{node.id}' is not permitted."], []
            return [], []

        if isinstance(node, ast.Call):
            return cls._inspect_call(node, roots)

        return [], []

    @classmethod
    def _inspect_call(cls, node: ast.Call, roots: tuple[str, ...] = ALLOWED_PATH_ROOTS) -> tuple[list[str], list[str]]:
        violations: list[str] = []
        paths: list[str] = []

        func = node.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
            if name in BANNED_CALLS:
                violations.append(f"Use of '{name}()' is not permitted.")
        elif isinstance(func, ast.Attribute):
            name = func.attr
            if name in BANNED_CALLS:
                violations.append(f"Use of '{name}()' is not permitted.")

        # Reflection with a computed attribute name defeats every name-based
        # check above, so only literal attribute names are allowed.
        if name in REFLECTION_CALLS and len(node.args) >= 2:
            attribute = node.args[1]
            if not (isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)):
                violations.append(f"'{name}()' with a computed attribute name is not permitted.")
            elif (
                attribute.value in BANNED_ATTRIBUTES
                or attribute.value in BANNED_CALLS
                # Reaching *any* dunder by reflection is never legitimate
                # analysis code, and enumerating them individually would always
                # lag behind the next interpreter internal someone finds.
                or (attribute.value.startswith("__") and attribute.value.endswith("__"))
            ):
                violations.append(f"'{name}()' cannot be used to reach '{attribute.value}'.")

        # Path arguments: a literal string, an f-string built only from
        # constants, or a `+` concatenation of such can be evaluated statically
        # and checked against the allowlist. Anything else -- a bare variable, a
        # function call, an f-string with a computed field -- cannot be proven
        # safe, so it is rejected outright rather than let through unchecked.
        checkable = (isinstance(func, ast.Name) and name in PATH_ARG_FUNCTIONS) or (
            isinstance(func, ast.Attribute) and name in (PATH_ARG_FUNCTIONS | PATH_ARG_METHODS)
        )
        if checkable:
            arg_node = cls._first_path_arg(node)
            if arg_node is not None and not (
                isinstance(arg_node, ast.Constant) and not isinstance(arg_node.value, str)
            ):
                path_literal = cls._static_string_value(arg_node)
                if path_literal is None:
                    violations.append(
                        f"'{name}()' path argument must be a literal string; "
                        "computed or dynamic paths are not permitted."
                    )
                elif not _is_path_allowed(path_literal, roots):
                    violations.append(f"File access outside the workspace is not permitted (path: '{path_literal}').")
                    paths.append(path_literal)

        return violations, paths

    @staticmethod
    def _first_path_arg(node: ast.Call) -> ast.expr | None:
        """Returns the AST node for the path-like argument, if any.

        Only the *node* is resolved here -- whether it can be evaluated to a
        literal string is a separate question, answered by
        ``_static_string_value``.
        """
        if node.args:
            return node.args[0]
        for kw in node.keywords:
            if kw.arg in {"path_or_buf", "filepath_or_buffer", "fname", "file", "path"}:
                return kw.value
        return None

    @classmethod
    def _static_string_value(cls, node: ast.expr) -> str | None:
        """Evaluates ``node`` to a string if it is built entirely from constants.

        Handles a plain string literal, an f-string whose every field is
        itself a constant (``f"/workspace/{'a'}"``), and `+` concatenations of
        either. Returns ``None`` the moment any piece depends on something
        computed at runtime -- a variable, a call, a non-constant f-string
        field -- which the caller must treat as unverifiable, not as safe.
        """
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None

        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif (
                    isinstance(value, ast.FormattedValue)
                    and value.format_spec is None
                    and value.conversion in (-1, ord("s"))
                    and isinstance(value.value, ast.Constant)
                    and isinstance(value.value.value, str)
                ):
                    # `{"ok"}` inside an f-string -- a constant field with no
                    # conversion/format-spec to apply, so it is just its value.
                    parts.append(value.value.value)
                else:
                    return None
            return "".join(parts)

        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = cls._static_string_value(node.left)
            right = cls._static_string_value(node.right)
            return None if left is None or right is None else left + right

        return None

    # ------------------------------------------------------------------ #
    @staticmethod
    def strip_markdown_fences(code: str) -> str:
        """Removes ```python fences an LLM sometimes leaves in the payload."""
        cleaned = code.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines)
        return cleaned.strip()

    #: Aliases small models routinely use without importing.
    COMMON_IMPORTS = {
        "pd": "import pandas as pd",
        "np": "import numpy as np",
        "plt": "import matplotlib.pyplot as plt",
        "sns": "import seaborn as sns",
        "px": "import plotly.express as px",
        "go": "import plotly.graph_objects as go",
    }

    @staticmethod
    def missing_alias_imports(code: str) -> list[str]:
        """Import statements for aliases the code uses but never imports."""
        return [
            statement
            for alias, statement in CodeGuard.COMMON_IMPORTS.items()
            if f"{alias}." in code and f"import {alias}" not in code and f"as {alias}" not in code
        ]

    @staticmethod
    def repair(code: str) -> tuple[bool, str]:
        """Best-effort deterministic repair of generated code.

        Returns ``(parses_cleanly, code)``. Two safe transformations are applied:
        fence stripping, and prepending imports for aliases the code uses but
        never imports.

        Import healing runs unconditionally rather than only after a parse
        failure. A missing import raises ``NameError`` at *runtime* and parses
        perfectly well, so gating the fix on a ``SyntaxError`` — as the original
        implementation did — meant it could never actually fire.
        """
        cleaned = CodeGuard.strip_markdown_fences(code)

        missing = CodeGuard.missing_alias_imports(cleaned)
        candidate = "\n".join(missing) + "\n" + cleaned if missing else cleaned

        try:
            ast.parse(candidate)
            return True, candidate
        except SyntaxError:
            # Prepending imports cannot introduce a syntax error, so the fault is
            # in the generated code itself. Hand back the un-prefixed source so
            # the error the model sees points at its own line numbers.
            return False, cleaned


def imported_modules(code: str) -> frozenset[str]:
    """Top-level module names ``code`` imports.

    Used to decide, *before* the code runs, whether executing it would make the
    runtime install something. The daemon installs reactively on
    ``ModuleNotFoundError`` — after execution has already begun — so a consent
    gate that waited for that signal would be asking permission for something
    that had already happened.

    Unparseable code yields nothing rather than raising: the guard reports the
    syntax error on its own, and this must not turn one into a crash.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return frozenset()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import has no module of its own to install.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return frozenset(name for name in names if name)


def is_safe_identifier(name: str) -> bool:
    """Guards values that get interpolated into generated source (e.g. variable export)."""
    return bool(name) and name.isidentifier() and not name.startswith("__") and len(name) <= 128
