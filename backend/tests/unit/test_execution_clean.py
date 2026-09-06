import pandas as pd
import pytest
from src.core.execution import CodeExecutor
from src.core.prompts import create_prompt


def test_clean_execution_safe_imports():
    executor = CodeExecutor("test-session")
    
    code = """
import math
import numpy as np
val = math.sqrt(16)
arr = np.array([1, 2, 3])
print(f"VAL={val} SUM={arr.sum()}")
"""
    result = executor.execute(code, pd.DataFrame({"a": [1]}))
    assert result.ok, f"Execution failed: {result.output}"
    assert "VAL=4.0 SUM=6" in result.output


def test_clean_execution_blocks_banned_modules():
    executor = CodeExecutor("test-session")
    
    code = """
import os
"""
    result = executor.execute(code, pd.DataFrame({"a": [1]}))
    assert not result.ok
    assert "Importing 'os' is restricted" in result.output or "blocked" in result.output.lower() or "rejected" in result.output.lower()


def test_create_prompt_enriched_error_diagnostics():
    df = pd.DataFrame({
        "age": [25, 30, None, 40],
        "category": ["A", "B", "A", "B"],
        "income": [50000.0, 60000.0, 75000.0, 80000.0]
    })
    
    failed_code = "df['missing_col'].mean()"
    previous_error = "KeyError: 'missing_col'"
    
    prompt = create_prompt(
        instruction="Calculate mean",
        df=df,
        previous_error=previous_error,
        failed_code=failed_code,
    )
    
    assert "<failed_code>" in prompt
    assert failed_code in prompt
    assert "<runtime_diagnostics>" in prompt
    assert "age" in prompt
    assert "income" in prompt
    assert "Missing Values" in prompt or "nulls" in prompt
