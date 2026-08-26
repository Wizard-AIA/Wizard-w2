from __future__ import annotations

import pytest

from src.core.security.code_guard import (
    BANNED_ATTRIBUTES,
    BANNED_CALLS,
    BANNED_MODULES,
    CodeGuard,
)


DANGEROUS_ATTRIBUTES_SUBSET = sorted(BANNED_ATTRIBUTES)

ACCESS_PATTERNS = [
    "obj.{attr}",
    "getattr(obj, '{attr}')",
    "getattr(obj, '{part1}' + '{part2}')",
    "getattr(obj, f'{{{attr}}}')",
    "obj.parent.{attr}.child",
]

CALL_PATTERNS = ["{call}(arg)", "{call}()", "x = {call}(arg)"]

PATH_ESCAPE_PATTERNS = [
    "open('../../etc/passwd')",
    "open('/etc/shadow')",
    "open('/proc/self/environ')",
    "open('/root/secret.txt')",
    "open('/var/log/syslog')",
    "open('/bin/bash')",
    "open('/workspace/../out')",
    "open('/tmp/wizard/../../etc')",
    "open('..\\\\..\\\\windows\\\\system32')",
    "open('/usr/bin/python')",
    "open('/workspace/../../')",
    "open('/tmp/..')",
    "open('C:\\\\Windows')",
    "open('/etc/hosts')",
    "import pandas as pd; pd.read_csv('/etc/shadow')",
    "import pandas as pd; pd.read_csv('../../etc/passwd')",
    "with open('/etc/passwd') as f: f.read()",
    "open('/dev/random')",
]

MULTI_LAYER_EVASION = [
    "getattr(obj, '__glo' + 'bals__')",
    "getattr(().__class__.__base__, '__subc' + 'lasses__')()",
    "func_ref = getattr(builtins, 'ev' + 'al'); func_ref('1+1')",
    "key = '\\x5f\\x5f\\x67\\x6c\\x6f\\x62\\x61\\x6c\\x73\\x5f\\x5f'; getattr(f, key)",
    "getattr(obj, f'__{\"glo\"}bals__')",
    "cls = getattr(obj, '__class__'); getattr(cls, '__ba' + 'ses__')",
]

SAFE_ANALYTICAL_SCRIPTS = [
    "import pandas as pd\ndf = pd.DataFrame({'A': [1,2,3]})\ndf['B'] = df['A'] * 2",
    "import numpy as np\narr = np.array([1, 2, 3])\nres = np.mean(arr)",
    "import matplotlib.pyplot as plt\nplt.plot([1,2,3])",
    "import seaborn as sns\nsns.histplot([1,2,3])",
    "from sklearn.linear_model import LinearRegression\nmodel = LinearRegression()",
    "import math\nval = math.sin(math.pi / 2)",
    "res = [x**2 for x in range(10) if x % 2 == 0]",
    "d = {'a': 1, 'b': 2}\nres = {k: v*2 for k, v in d.items()}",
    "with open('/workspace/data.csv', 'r') as f:\n    data = f.read()",
    "with open('/workspace/output.txt', 'w') as f:\n    f.write('hello')",
    "import datetime\nnow = datetime.datetime.now()",
    "import json\nd = json.loads('{\"a\": 1}')",
    "import re\nmatch = re.search(r'\\d+', 'abc 123')",
    "import collections\nc = collections.Counter([1, 1, 2])",
    "import itertools\nlist(itertools.combinations([1,2,3], 2))",
    "import functools\n@functools.lru_cache\ndef f(x): return x",
    "import hashlib\nh = hashlib.md5(b'test').hexdigest()",
    "import random\nval = random.randint(1, 10)",
    "import string\nchars = string.ascii_letters",
    "import csv\nres = list(csv.reader(['a,b,c']))",
    "x = 10\ny = 20\nassert x + y == 30",
    "def my_func(a, b):\n    return a + b\nres = my_func(5, 5)",
    "class MyClass:\n    def __init__(self, val):\n        self.val = val\nobj = MyClass(10)",
    "import pandas as pd\ndf = pd.read_csv('/workspace/input.csv')\ndf.groupby('col').mean()",
    "import numpy as np\nnp.linalg.inv([[1, 2], [3, 4]])",
    "from sklearn.preprocessing import StandardScaler\nsc = StandardScaler()",
    "import math\nmath.factorial(5)",
    "import datetime\ntdelta = datetime.timedelta(days=1)",
    "import json\njson.dumps({'a': [1,2,3]})",
    "s = 'hello world'\nres = s.upper().split()",
]


class TestASTSecurityMutations:
    @pytest.mark.parametrize("module", sorted(BANNED_MODULES))
    @pytest.mark.parametrize(
        "style", ["import {mod}", "from {mod} import something", "import {mod} as mymod", "__import__('{mod}')"]
    )
    def test_all_banned_module_import_variants(self, module, style):
        code = style.format(mod=module)
        verdict = CodeGuard.scan(code, extra_roots=("/workspace",))
        assert not verdict.ok, f"Failed to block module import: {code}\nViolations: {verdict.violations}"

    @pytest.mark.parametrize("attr", DANGEROUS_ATTRIBUTES_SUBSET)
    @pytest.mark.parametrize("pattern", ACCESS_PATTERNS)
    def test_all_banned_attribute_access_patterns(self, attr, pattern):
        if "{part1}" in pattern:
            part1 = attr[: len(attr) // 2]
            part2 = attr[len(attr) // 2 :]
            code = pattern.format(attr=attr, part1=part1, part2=part2)
        else:
            code = pattern.format(attr=attr)

        # provide a dummy obj in context
        full_code = f"class Dummy:\n    pass\nobj = Dummy()\n{code}"
        verdict = CodeGuard.scan(full_code, extra_roots=("/workspace",))
        assert not verdict.ok, f"Failed to block attribute access: {code}\nViolations: {verdict.violations}"

    @pytest.mark.parametrize("call", sorted(BANNED_CALLS))
    @pytest.mark.parametrize("pattern", CALL_PATTERNS)
    def test_all_banned_builtin_calls(self, call, pattern):
        code = pattern.format(call=call)
        verdict = CodeGuard.scan(code, extra_roots=("/workspace",))
        assert not verdict.ok, f"Failed to block builtin call: {code}\nViolations: {verdict.violations}"

    @pytest.mark.parametrize("code", PATH_ESCAPE_PATTERNS)
    def test_path_escape_attempts(self, code):
        verdict = CodeGuard.scan(code, extra_roots=("/workspace",))
        assert not verdict.ok, f"Failed to block path escape attempt: {code}\nViolations: {verdict.violations}"

    @pytest.mark.parametrize("code", MULTI_LAYER_EVASION)
    def test_multi_layer_evasion_chains(self, code):
        full_code = f"class Dummy:\n    pass\nobj = Dummy()\nbuiltins = None\nf = None\n{code}"
        verdict = CodeGuard.scan(full_code, extra_roots=("/workspace",))
        assert not verdict.ok, f"Failed to block evasion chain: {code}\nViolations: {verdict.violations}"

    @pytest.mark.parametrize("func", ["getattr", "setattr", "delattr"])
    @pytest.mark.parametrize("expr", ["f'__sub' + 'classes__'", "'__glo' + 'bals__'", "chr(95)*2 + 'dict' + chr(95)*2"])
    def test_reflection_with_computed_arguments(self, func, expr):
        code = f"class Dummy:\n    pass\nobj = Dummy()\n{func}(obj, {expr})"
        verdict = CodeGuard.scan(code, extra_roots=("/workspace",))
        assert not verdict.ok, f"Failed to block reflection with computed arg: {code}\nViolations: {verdict.violations}"

    @pytest.mark.parametrize("code", SAFE_ANALYTICAL_SCRIPTS)
    def test_safe_analytical_code_never_rejected(self, code):
        verdict = CodeGuard.scan(code, extra_roots=("/workspace",))
        assert verdict.ok, f"Falsely blocked safe code: {code}\nViolations: {verdict.violations}"

    def test_mutation_verdict_consistency(self):
        # 10 safe, 10 unsafe
        snippets = SAFE_ANALYTICAL_SCRIPTS[:10] + PATH_ESCAPE_PATTERNS[:10]
        for snippet in snippets:
            first_verdict = CodeGuard.scan(snippet, extra_roots=("/workspace",))
            for _ in range(10):
                subsequent_verdict = CodeGuard.scan(snippet, extra_roots=("/workspace",))
                assert first_verdict.ok == subsequent_verdict.ok
                assert len(first_verdict.violations) == len(subsequent_verdict.violations)
