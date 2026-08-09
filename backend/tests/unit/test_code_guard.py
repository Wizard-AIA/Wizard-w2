"""Unit + negative tests for the static code guard.

The guard is the only thing standing between model output and an interpreter on
the paths where Docker is unavailable, so both directions matter: it must block
escapes, and it must not block ordinary analysis code.
"""

from __future__ import annotations

import pytest

from src.core.security.code_guard import (
    ALLOWED_PATH_ROOTS,
    CodeGuard,
    _is_path_allowed,
    imported_modules,
    is_safe_identifier,
)


# --------------------------------------------------------------------------- #
# Positive: legitimate analysis code must pass
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "code",
    [
        "print(df.head())",
        "import pandas as pd\nprint(pd.DataFrame({'a': [1, 2]}).mean())",
        "import matplotlib.pyplot as plt\nplt.plot([1, 2], [3, 4])",
        "import numpy as np\nprint(np.mean(df['A']))",
        "result = df.groupby('B')['A'].mean()\nprint(result)",
        "with open('/workspace/out.csv', 'w') as handle:\n    handle.write('a,b')",
        "df.to_csv('/workspace/result.csv', index=False)",
        "df.to_csv('nested/result.csv')",
        "import plotly.express as px\nfig = px.bar(df, x='B', y='A')\nfig.write_html('/workspace/plot.html')",
        "from scipy import stats\nprint(stats.ttest_1samp(df['A'], 0))",
        "import seaborn as sns\nsns.heatmap(df.corr(numeric_only=True))",
        # A column literally named "os" must not trip the guard; the previous
        # regex denylist matched the bare word anywhere in the source.
        "print(df['os'].value_counts())",
        "requests_per_day = df['A'].sum()\nprint(requests_per_day)",
    ],
)
def test_allows_legitimate_analysis(code: str) -> None:
    verdict = CodeGuard.scan(code)
    assert verdict.ok, f"unexpectedly blocked: {verdict.reason}"


# --------------------------------------------------------------------------- #
# Negative: escapes must be blocked
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "code",
    [
        "import os\nos.system('id')",
        "import subprocess\nsubprocess.run(['ls'])",
        "from os import system\nsystem('id')",
        "__import__('os').system('id')",
        "eval('1 + 1')",
        "exec('import os')",
        "compile('x=1', '<s>', 'exec')",
        "import socket\nsocket.socket()",
        "import requests\nrequests.get('http://example.com')",
        "import urllib.request\nurllib.request.urlopen('http://x')",
        "import importlib\nimportlib.import_module('os')",
        "import ctypes",
        "import pickle\npickle.loads(b'')",
        "import shutil\nshutil.rmtree('/')",
        "import multiprocessing",
        # Interpreter-internals walks used to reach os from a benign object.
        "print(().__class__.__bases__[0].__subclasses__())",
        "print((lambda: 0).__globals__)",
        "print(open.__self__.__dict__)",
        "x = globals()",
        "import sys\nsys.exit(1)",
    ],
)
def test_blocks_escape_attempts(code: str) -> None:
    verdict = CodeGuard.scan(code)
    assert not verdict.ok
    assert not verdict.syntax_error, "an escape must be a policy violation, not a syntax error"
    assert verdict.reason


@pytest.mark.parametrize(
    "code",
    [
        "getattr(__builtins__, 'ev' + 'al')('1')",
        "getattr(obj, name)()",
        "setattr(obj, key, value)",
        "getattr(df, '__class__')",
        "print(__builtins__)",
        "x = __loader__",
    ],
)
def test_blocks_reflection_used_to_rebuild_banned_calls(code: str) -> None:
    """A computed attribute name defeats every name-based check, so reflection
    is only permitted with a literal, non-restricted attribute."""
    assert not CodeGuard.scan(code).ok


@pytest.mark.parametrize(
    "code",
    [
        "print(getattr(df, 'shape'))",
        "value = getattr(row, 'name', None)",
        "hasattr(df, 'columns')",
    ],
)
def test_allows_reflection_with_literal_safe_attributes(code: str) -> None:
    assert CodeGuard.scan(code).ok


@pytest.mark.parametrize(
    "code",
    [
        "open('/etc/passwd')",
        "open('../../etc/passwd')",
        "open('/workspace/../etc/shadow')",
        "df.to_csv('/etc/cron.d/payload')",
        "open('file:///etc/passwd')",
        "open('\\\\\\\\server\\\\share\\\\file')",
    ],
)
def test_blocks_path_traversal(code: str) -> None:
    verdict = CodeGuard.scan(code)
    assert not verdict.ok
    assert "workspace" in verdict.reason.lower() or "not permitted" in verdict.reason.lower()


# --------------------------------------------------------------------------- #
# Syntax errors are retryable, not policy violations
# --------------------------------------------------------------------------- #
def test_syntax_error_is_flagged_as_retryable() -> None:
    verdict = CodeGuard.scan("def broken(:\n  pass")
    assert not verdict.ok
    assert verdict.syntax_error
    assert verdict.retryable


def test_empty_code_is_rejected() -> None:
    assert not CodeGuard.scan("").ok
    assert not CodeGuard.scan("   \n  ").ok


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/workspace/a.csv", True),
        ("/workspace/nested/deep/a.csv", True),
        ("relative.csv", True),
        ("./relative.csv", True),
        ("/workspace/../workspace/ok.csv", True),
        ("/etc/passwd", False),
        ("../escape.csv", False),
        ("/workspacefoo/a.csv", False),
        ("", False),
        ("http://example.com/a.csv", False),
    ],
)
def test_path_allowlist(path: str, expected: bool) -> None:
    assert _is_path_allowed(path) is expected


def test_allowed_roots_are_absolute_posix() -> None:
    assert all(root.startswith("/") for root in ALLOWED_PATH_ROOTS)


# --------------------------------------------------------------------------- #
# Repair
# --------------------------------------------------------------------------- #
def test_repair_strips_markdown_fences() -> None:
    parses, code = CodeGuard.repair("```python\nprint(1)\n```")
    assert parses
    assert code == "print(1)"


def test_repair_adds_missing_alias_imports() -> None:
    """A missing import is a runtime NameError, not a SyntaxError.

    Healing therefore has to run on code that already parses, which is the case
    the original implementation could never reach.
    """
    parses, code = CodeGuard.repair("result = pd.DataFrame({'a': [1]})\nprint(result)")
    assert parses
    assert code.startswith("import pandas as pd")
    assert "result = pd.DataFrame" in code


def test_repair_does_not_duplicate_existing_imports() -> None:
    original = "import pandas as pd\nprint(pd.DataFrame({'a': [1]}))"
    parses, code = CodeGuard.repair(original)
    assert parses
    assert code.count("import pandas as pd") == 1


def test_repair_heals_several_aliases_at_once() -> None:
    parses, code = CodeGuard.repair("plt.plot(np.arange(3))")
    assert parses
    assert "import numpy as np" in code
    assert "import matplotlib.pyplot as plt" in code


def test_repair_leaves_valid_code_untouched() -> None:
    original = "x = 1\nprint(x)"
    parses, code = CodeGuard.repair(original)
    assert parses
    assert code == original


def test_repair_reports_failure_for_unfixable_code() -> None:
    parses, _ = CodeGuard.repair("def f(:\n pass")
    assert not parses


# --------------------------------------------------------------------------- #
# Identifier validation (guards values interpolated into generated source)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["df", "result_2", "myVar"])
def test_accepts_plain_identifiers(name: str) -> None:
    assert is_safe_identifier(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "__builtins__",
        "a b",
        "a'; import os; x='",
        "../etc/passwd",
        "1abc",
        "x" * 200,
    ],
)
def test_rejects_unsafe_identifiers(name: str) -> None:
    assert not is_safe_identifier(name)


# --------------------------------------------------------------------------- #
# Import discovery (what the permission layer gates an install on)
# --------------------------------------------------------------------------- #
def test_reports_top_level_names_from_both_import_forms() -> None:
    code = "import pandas as pd\nimport matplotlib.pyplot as plt\nfrom sklearn.metrics import r2_score\n"
    assert imported_modules(code) == {"pandas", "matplotlib", "sklearn"}


def test_a_relative_import_names_nothing_to_install() -> None:
    """`from . import helpers` has no distribution behind it."""
    assert imported_modules("from . import helpers\nfrom .. import other\n") == frozenset()


def test_unparseable_code_yields_no_imports_rather_than_raising() -> None:
    """The guard reports the syntax error itself; this must not turn it into a crash."""
    assert imported_modules("import pandas as\n") == frozenset()


def test_imports_inside_a_function_still_count() -> None:
    """A lazy import installs exactly the same package when it runs."""
    assert imported_modules("def go():\n    import lifelines\n    return lifelines\n") == {"lifelines"}


# --------------------------------------------------------------------------- #
# Offending paths travel structurally, not inside the error sentence
# --------------------------------------------------------------------------- #
def test_a_path_violation_reports_the_path_it_objected_to() -> None:
    """The permission layer has to know *which* path to ask about.

    Reading it back out of the message would make the message's wording
    load-bearing for a security decision.
    """
    verdict = CodeGuard.scan("df.to_csv('/etc/passwd')")
    assert not verdict.ok
    assert verdict.paths == ["/etc/passwd"]
    assert verdict.only_paths


def test_a_banned_import_is_never_offered_as_a_path_to_approve() -> None:
    """Only a path is negotiable. An import escape is not up for consent."""
    verdict = CodeGuard.scan("import os\ndf.to_csv('/etc/passwd')")
    assert not verdict.ok
    assert not verdict.only_paths


def test_a_syntax_error_is_not_a_path_question() -> None:
    verdict = CodeGuard.scan("df.to_csv(")
    assert verdict.syntax_error
    assert not verdict.only_paths


def test_an_approved_root_makes_the_same_write_pass() -> None:
    """How the guard is told yes, rather than how it is bypassed."""
    assert not CodeGuard.scan("df.to_csv('/data/reports/out.csv')").ok
    assert CodeGuard.scan("df.to_csv('/data/reports/out.csv')", extra_roots=("/data/reports",)).ok


def test_an_approved_root_does_not_widen_anything_else() -> None:
    """Consent for one directory is consent for that directory only."""
    verdict = CodeGuard.scan("df.to_csv('/etc/passwd')", extra_roots=("/data/reports",))
    assert not verdict.ok


# --------------------------------------------------------------------------- #
# Dynamic path expressions must not bypass the check (issue #87 / C-5)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "code",
    [
        # f-string with a computed field
        "open(f'/etc/{name}')",
        'open(f"{base}/passwd")',
        # string concatenation involving a variable
        "open(prefix + '/etc/shadow')",
        "df.to_csv(dest)",
        "df.to_csv(get_path())",
        "with open(user_supplied) as f:\n    pass",
        "df.to_csv(path_or_buf=dest)",
        # nested inside an f-string that also has a constant piece
        "open(f'/workspace/{sub}/out.csv')",
    ],
)
def test_blocks_dynamic_path_expressions(code: str) -> None:
    """A path built at runtime cannot be proven safe statically, so it must be
    rejected rather than silently let through -- the C-5 bypass."""
    verdict = CodeGuard.scan(code)
    assert not verdict.ok
    assert not verdict.syntax_error


def test_evaluates_constant_f_string_paths_statically() -> None:
    """An f-string whose fields are themselves constants can be evaluated, so
    it is checked like any other literal instead of being rejected outright."""
    assert CodeGuard.scan("open(f'/workspace/{\"ok\"}/out.csv')").ok
    verdict = CodeGuard.scan("open(f'/etc/{\"passwd\"}')")
    assert not verdict.ok
    assert verdict.paths == ["/etc/passwd"]


def test_evaluates_constant_string_concatenation_statically() -> None:
    assert CodeGuard.scan("open('/workspace/' + 'out.csv')").ok
    verdict = CodeGuard.scan("open('/etc/' + 'passwd')")
    assert not verdict.ok
    assert verdict.paths == ["/etc/passwd"]


def test_an_approved_root_does_not_move_where_a_relative_path_lands() -> None:
    """A relative path resolves against the *first* root, so order is load-bearing.

    `CodeExecutor.guard` passes the session workspace first for this reason. If a
    consented directory were prepended instead, `to_csv("out.csv")` would quietly
    start meaning a file in that directory — consent to write somewhere is not a
    request to move the working directory there.
    """
    workspace_first = CodeGuard.scan("df.to_csv('../reports/out.csv')", extra_roots=("/workspace", "/data/reports"))
    assert not workspace_first.ok, "a relative escape resolved against the granted root, not the workspace"

    # The same grant still admits the path when it is written out in full.
    assert CodeGuard.scan("df.to_csv('/data/reports/out.csv')", extra_roots=("/workspace", "/data/reports")).ok
