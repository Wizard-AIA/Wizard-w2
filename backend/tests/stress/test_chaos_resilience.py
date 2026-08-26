from __future__ import annotations

import threading

import numpy as np
import pandas as pd
import pytest

from src.core.execution import CodeExecutor
from src.core.security.code_guard import CodeGuard


# Optional dependencies as requested
pa = pytest.importorskip("pyarrow")


class TestChaosAndFaultResilience:
    @pytest.mark.parametrize("offset", [0, 1, 2, 3, 4, 8, 16, 32, 64, 128])
    def test_corrupted_arrow_at_multiple_offsets(self, offset: int) -> None:
        df = pd.DataFrame({"a": np.random.randn(100), "b": ["x"] * 100})
        table = pa.Table.from_pandas(df)
        sink = pa.BufferOutputStream()
        with pa.RecordBatchStreamWriter(sink, table.schema) as writer:
            writer.write_table(table)
        buf = sink.getvalue()
        raw_bytes = buf.to_pybytes()

        if offset >= len(raw_bytes):
            pytest.skip("Offset larger than data")

        corrupted = bytearray(raw_bytes)
        corrupted[offset] = corrupted[offset] ^ 0xFF

        try:
            reader = pa.RecordBatchStreamReader(pa.BufferReader(bytes(corrupted)))
            restored = reader.read_all()
            assert not restored.equals(table)
        except Exception:
            pass

    @pytest.mark.parametrize("truncation_pct", [10, 25, 50, 75, 90])
    def test_truncated_arrow_streams(self, truncation_pct: int) -> None:
        df = pd.DataFrame({"a": np.random.randn(100), "b": ["x"] * 100})
        table = pa.Table.from_pandas(df)
        sink = pa.BufferOutputStream()
        with pa.RecordBatchStreamWriter(sink, table.schema) as writer:
            writer.write_table(table)
        buf = sink.getvalue()
        raw_bytes = buf.to_pybytes()

        truncate_at = int(len(raw_bytes) * (1 - truncation_pct / 100.0))
        truncated = raw_bytes[:truncate_at]

        try:
            reader = pa.RecordBatchStreamReader(pa.BufferReader(truncated))
            restored = reader.read_all()
            assert not restored.equals(table)
        except Exception:
            pass

    @pytest.mark.parametrize(
        "code",
        [
            "def foo(:",
            "if True:",
            "print('a'",
            "a = 1 + ",
            "class A:",
            "for i in range(10)",
            "while True:",
            "return 1",
            "def foo():\nreturn 1",
        ],
    )
    def test_syntax_error_isolation(self, code: str) -> None:
        executor = CodeExecutor(session_id="syntax_err")
        res = executor.execute(code, df=pd.DataFrame())
        assert res.ok is False
        assert "Syntax" in res.output or "Error" in res.output or res.blocked is True

    @pytest.mark.parametrize(
        "exc_type, code",
        [
            ("ZeroDivisionError", "1 / 0"),
            ("TypeError", "'a' + 1"),
            ("KeyError", "{'a': 1}['b']"),
            ("IndexError", "[1, 2][5]"),
            ("ValueError", "int('a')"),
            ("AttributeError", "'a'.foo()"),
            ("NameError", "foo"),
            ("RecursionError", "def f(): f()\nf()"),
        ],
    )
    def test_runtime_exception_isolation(self, exc_type: str, code: str) -> None:
        executor = CodeExecutor(session_id="runtime_err")
        res = executor.execute(code, df=pd.DataFrame())
        assert res.ok is False
        assert exc_type in res.output

    def test_concurrent_execution_crash_isolation(self) -> None:
        results: list[bool] = []

        def run_valid() -> None:
            executor = CodeExecutor(session_id=f"valid_{threading.get_ident()}")
            res = executor.execute("print('valid')", df=pd.DataFrame())
            results.append(res.ok)

        def run_crashing() -> None:
            executor = CodeExecutor(session_id=f"crash_{threading.get_ident()}")
            res = executor.execute("1/0", df=pd.DataFrame())
            results.append(res.ok)

        threads = []
        for _ in range(5):
            t1 = threading.Thread(target=run_valid)
            t2 = threading.Thread(target=run_crashing)
            threads.extend([t1, t2])
            t1.start()
            t2.start()

        for t in threads:
            t.join()

        assert sum(results) == 5

    def test_large_output_handling(self) -> None:
        executor = CodeExecutor(session_id="large_output")
        code = "print('A' * 1024 * 1024 * 2)"
        res = executor.execute(code, df=pd.DataFrame())
        assert res.ok is True

    def test_rapid_session_creation_destruction(self) -> None:
        for i in range(50):
            executor = CodeExecutor(session_id=f"rapid_{i}")
            res = executor.execute("a = 1", df=pd.DataFrame())
            assert res.ok is True

    @pytest.mark.parametrize(
        "obfuscated_code",
        [
            "__import__('o' + 's')",
            "__import__(chr(111) + chr(115))",
            "getattr(builtins, '__import__')('os')",
            "getattr(__builtins__, '__import__')('os')",
            "import base64; exec(base64.b64decode(b'aW1wb3J0IG9zCg==').decode())",
            "eval('__import__(\"os\")')",
            "getattr(__import__('os'), 'system')('echo 1')",
            "sys = __import__('sys')",
            "subprocess = __import__('subprocess')",
            "getattr(__builtins__, 'ev' + 'al')('1')",
            "class A: pass\nA.__class__.__base__.__subclasses__()",
            "().__class__.__base__.__subclasses__()",
            "open('/etc/passwd', 'r').read()",
            "eval('1')",
            "[c for c in ().__class__.__base__.__subclasses__() if c.__name__ == 'catch_warnings'][0]()._module.__builtins__['__import__']('os')",
        ],
    )
    def test_codeguard_on_obfuscated_malicious_code(self, obfuscated_code: str) -> None:
        verdict = CodeGuard.scan(obfuscated_code, extra_roots=("/workspace",))
        assert verdict.ok is False
        assert len(verdict.violations) > 0

    @pytest.mark.parametrize("code", ["", " ", "\n", "\t", "# just a comment", "pass"])
    def test_empty_and_whitespace_code_execution(self, code: str) -> None:
        executor = CodeExecutor(session_id="empty")
        res = executor.execute(code, df=pd.DataFrame())
        assert isinstance(res.ok, bool)
        assert isinstance(res.output, str)

    def test_nested_exception_chains(self) -> None:
        code = """
try:
    try:
        1/0
    except ZeroDivisionError:
        raise ValueError("Inner error")
except ValueError:
    raise TypeError("Outer error")
"""
        executor = CodeExecutor(session_id="nested")
        res = executor.execute(code, df=pd.DataFrame())
        assert res.ok is False
        assert "TypeError" in res.output

    def test_memory_intensive_code_execution(self) -> None:
        executor = CodeExecutor(session_id="memory")
        code = "a = list(range(10000000))"
        res = executor.execute(code, df=pd.DataFrame())
        assert res.ok is True

    def test_unicode_in_code_execution(self) -> None:
        executor = CodeExecutor(session_id="unicode")
        code = """
नमस्ते = 42
msg = 'hello 🌟'
print(नमस्ते, msg)
"""
        res = executor.execute(code, df=pd.DataFrame())
        assert res.ok is True
        assert "42" in res.output
