from __future__ import annotations

import pytest

from src.config import settings
from src.core.llm.provider import LLMProvider, LLMRole
from src.core.llm.registry import ModelInfo
from src.core.llm.router import TaskTier, classify_task_complexity


@pytest.mark.parametrize(
    "instruction",
    [
        "show the schema",
        "list columns",
        "how many rows are there",
        "preview the dataset",
        "what are the data types",
        "how big is this data",
    ],
)
def test_metadata_questions_are_lightweight(instruction: str) -> None:
    assert classify_task_complexity(instruction) == TaskTier.LIGHTWEIGHT


@pytest.mark.parametrize(
    "instruction",
    [
        "calculate the average revenue",
        "plot revenue by month",
        "join orders and customers",
        "clean the missing values",
    ],
)
def test_normal_analysis_is_standard(instruction: str) -> None:
    assert classify_task_complexity(instruction) == TaskTier.STANDARD


@pytest.mark.parametrize(
    "instruction",
    [
        "investigate anomalies and explain the root cause",
        "validate the hypothesis that churn is caused by price",
        "forecast next quarter and compare the scenarios",
        "find outliers and explain why they occur",
    ],
)
def test_investigations_are_reasoning_heavy(instruction: str) -> None:
    assert classify_task_complexity(instruction) == TaskTier.REASONING_HEAVY


def test_lightweight_routing_uses_an_installed_small_model(monkeypatch) -> None:
    from src.core.llm import registry

    monkeypatch.setattr(settings, "FAST_MODEL_NAME", "", raising=False)
    monkeypatch.setattr(
        registry.model_registry,
        "list_models",
        lambda **_: [
            ModelInfo(name="reasoning-14b", parameter_size="14B", capabilities=["reasoning"]),
            ModelInfo(name="qwen2.5:1.5b", parameter_size="1.5B", capabilities=["chat"]),
        ],
    )

    assert LLMProvider().model_for_task(LLMRole.MANAGER, TaskTier.LIGHTWEIGHT) == "qwen2.5:1.5b"


def test_explicit_model_is_never_downscaled() -> None:
    assert (
        LLMProvider().model_for_task(
            LLMRole.MANAGER, TaskTier.LIGHTWEIGHT, model="my-pinned-manager", provider="ollama"
        )
        == "my-pinned-manager"
    )
