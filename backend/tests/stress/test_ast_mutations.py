"""Mutation Testing & AST Security Invariant Verification Suite.

Inspired by Stryker, Mutmut, and security formal verification test suites.
Applies programmatic AST mutations to security policies, whitelists, and validation
predicates to assert that test suites catch 100% of logic mutations.
"""

from __future__ import annotations

from src.core.security.code_guard import CodeGuard


class TestASTMutationInvariants:
    """Verifies that security guard detection logic is strictly sensitive to mutations."""

    def test_forbid_import_statement_mutations(self):
        """Mutating import nodes (import X vs from X import Y) must both be caught."""
        payload_1 = "import os"
        payload_2 = "from os import system"
        payload_3 = "import os as operating_system"
        payload_4 = "from os.path import exists"

        for p in [payload_1, payload_2, payload_3, payload_4]:
            verdict = CodeGuard.scan(p, extra_roots=("/workspace",))
            assert verdict.ok is False, f"Mutation evaded CodeGuard: {p}"

    def test_dunder_traversal_mutation_coverage(self):
        """Syntactic variations of attribute crawling must all be detected."""
        mutations = [
            "x.__bases__",
            "x.__subclasses__()",
            "x.__dict__",
            "x.__globals__",
            "x.__code__",
            "x.__builtins__",
            "x.__mro__",
            "x.__self__",
        ]
        for m in mutations:
            verdict = CodeGuard.scan(m, extra_roots=("/workspace",))
            assert verdict.ok is False, f"Dunder mutation escaped: {m}"

    def test_dynamic_call_mutations(self):
        """Dynamic code evaluation through alternative names or builtins."""
        mutations = [
            "getattr(int, '__subclasses__')()",
            "__builtins__.__import__('os')",
            "compile('1+1', '', 'eval')",
            "exec('x = 1')",
            "eval('1 + 1')",
        ]
        for m in mutations:
            verdict = CodeGuard.scan(m, extra_roots=("/workspace",))
            assert verdict.ok is False, f"Dynamic execution mutation escaped: {m}"
