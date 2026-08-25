"""The ten adversarial scenarios of the analytical-control-plane benchmark plan.

Two layers, named honestly rather than claimed uniformly:

**Loop scenarios** (`LOOP_SCENARIOS`) run the real `AnalysisOrchestrator.run()` against a
`ScriptedLLM`, end to end -- see `backend/tests/benchmark/test_adversarial_scenarios.py`. These
exercise capabilities that are actually wired into the live turn: verification, data
understanding (always computed at `_orient`), the critic, confidence, and Phase 8's route
comparison.

**Deterministic-layer scenarios** call a `core.analysis.*` function directly (`methods.py`,
`understanding.py`, `objective.py`, `confidence.py`) rather than the orchestrator. Two gaps
inherited from earlier phases put these out of the live loop's reach today: target leakage needs
an explicit target column, which nothing in the current orchestrator ever supplies (leakage
checks require one, by design, rather than guessing which column is the target); and a named
method reaching the critic's "wrong test" detector needs `ValidationContext.method`, which is
never populated by `_verify`. Testing these at the function level is still a real, offline,
CI-gated regression check on the underlying capability -- it is just not (yet) proof that a live
turn reaches it, and this module says so rather than overclaiming a green check it cannot back.

Every fixture here is synthetic and small, built for one adversarial property, the same way
`reference_answers.py` is real data for content grading -- these are behaviour grading instead.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@dataclass
class LoopScenario:
    """One fixture run through the real orchestrator loop with a scripted model."""

    id: str
    category: str
    instruction: str
    responses: list[str]
    tier: str | None = None  # None = whatever AGENT_TIER settings already resolve to
    dataframe: pd.DataFrame | None = None
    #: name -> dataframe, for a scenario needing more than one table (e.g. a dirty join key).
    #: The first entry becomes the session's active dataset.
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)


def _obvious_analysis_wrong() -> LoopScenario:
    """An independent recomputation disagrees with the analysis -- the plainest case of "wrong"."""
    df = pd.DataFrame({"A": [1, 2, 3, 4, 5], "B": list("vwxyz"), "C": [0.1, 0.2, 0.3, 0.4, 0.5]})
    return LoopScenario(
        id="obvious_analysis_wrong",
        category="Obvious analysis wrong",
        instruction="total of A",
        dataframe=df,
        responses=[
            "1. Compute",
            "```python\nprint('total', df['A'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('MISMATCH: got 15 expected 99')\n```",
            "The total is 15.",
        ],
    )


def _dirty_join_key() -> LoopScenario:
    """Two tables share a join key, but one side is zero-padded and the other is a bare int --
    the same entities, formatted differently (`understanding.referential_consistency`)."""
    orders = pd.DataFrame({"customer_id": [1, 2, 3, 1, 2], "amount": [10, 20, 30, 40, 50]})
    customers = pd.DataFrame({"customer_id": ["01", "02", "03"], "name": ["Alice", "Bob", "Cara"]})
    return LoopScenario(
        id="dirty_join_key",
        category="Dirty join key",
        instruction="join orders and customers on customer_id and total the amount per customer",
        tables={"orders.csv": orders, "customers.csv": customers},
        responses=[
            "1. Join and total",
            "```python\n"
            "merged = tables['orders'].merge(tables['customers'], on='customer_id', how='left')\n"
            "print(merged.groupby('customer_id')['amount'].sum())\n"
            "```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: ok')\n```",
            "Here is the total amount per customer.",
        ],
    )


def _missingness_flips_the_conclusion() -> LoopScenario:
    """A dataset too sparse to support its own reported figure, combined with a disagreeing
    recomputation -- confidence's data-completeness and verification components should both
    catch it (Phase 9's `cannot_answer`)."""
    df = pd.DataFrame({"value": [1.0, None, None, None, None]})
    return LoopScenario(
        id="missingness_flips_the_conclusion",
        category="Missingness flips the conclusion",
        instruction="total of value",
        dataframe=df,
        responses=[
            "1. Compute",
            "```python\nprint('total', df['value'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('MISMATCH: got 1 expected 99')\n```",
            "The total is 1.",
        ],
    )


def _two_methods_disagree() -> LoopScenario:
    """Pearson and Spearman correlation on the same pair of columns land on materially different
    figures once an outlier is in the mix -- Pearson's normality assumption breaks, Spearman's
    does not, so Phase 8's comparison should name Spearman as the better-fitting route."""
    df = pd.DataFrame({"x": list(range(1, 11)), "y": [2, 4, 6, 8, 10, 12, 14, 16, 18, 1000]})
    return LoopScenario(
        id="two_methods_disagree",
        category="Two methods disagree",
        instruction="compare two correlation methods for x and y",
        tier="full",
        dataframe=df,
        responses=[
            "1. Compare two correlation methods.",
            "```python\nprint(df['x'].sum())\n```",
            "ACTION: parallel\nGOAL: pearson_correlation of x and y | spearman_correlation of x and y",
            "```python\nprint('pearson_correlation:', df['x'].corr(df['y']))\n```",
            "```python\nprint('spearman_correlation:', df['x'].corr(df['y'], method='spearman'))\n```",
            "ACTION: answer\nGOAL: report it",
            "```python\nprint('VERIFIED: ok')\n```",
            "Done.",
        ],
    )


def _simpsons_paradox() -> LoopScenario:
    """The textbook Charig et al. 1986 kidney-stone reversal, exactly as Phase 7's acceptance
    fixture uses it: treatment A wins in every segment, but B wins overall."""
    counts = [
        ("A", "small", 1, 81),
        ("A", "small", 0, 6),
        ("A", "large", 1, 192),
        ("A", "large", 0, 71),
        ("B", "small", 1, 234),
        ("B", "small", 0, 36),
        ("B", "large", 1, 55),
        ("B", "large", 0, 25),
    ]
    rows = [
        {"treatment": treatment, "stone_size": size, "success": outcome}
        for treatment, size, outcome, n in counts
        for _ in range(n)
    ]
    return LoopScenario(
        id="simpsons_paradox",
        category="Simpson's paradox",
        instruction="which treatment works better",
        dataframe=pd.DataFrame(rows),
        responses=[
            "1. Compute the overall success rate",
            "```python\nprint(df['success'].mean())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: ok')\n```",
            "Treatment B has the higher success rate overall.",
        ],
    )


def _multi_step_investigation() -> LoopScenario:
    """A question that genuinely needs two dependent steps -- the second step's goal only makes
    sense after seeing the first step's real output."""
    df = pd.DataFrame({"A": [1, 2, 3, 4, 5], "B": list("vwxyz"), "C": [0.1, 0.2, 0.3, 0.4, 0.5]})
    return LoopScenario(
        id="multi_step_investigation",
        category="Multi-step investigation",
        instruction="summarise column A",
        dataframe=df,
        responses=[
            "1. Investigate the data",
            "```python\nprint('sum', df['A'].sum())\n```",
            "ACTION: code\nGOAL: now compute the mean",
            "```python\nprint('mean', df['A'].mean())\n```",
            "ACTION: answer\nGOAL: report both",
            "```python\nprint('VERIFIED: ok')\n```",
            "The sum is 15 and the mean is 3.",
        ],
    )


LOOP_SCENARIOS: list[LoopScenario] = [
    _obvious_analysis_wrong(),
    _dirty_join_key(),
    _missingness_flips_the_conclusion(),
    _two_methods_disagree(),
    _simpsons_paradox(),
    _multi_step_investigation(),
]


# --------------------------------------------------------------------------- #
# Deterministic-layer scenarios -- see the module docstring for why these four
# are not (yet) exercised through the live orchestrator loop.
# --------------------------------------------------------------------------- #
def target_leakage_fixture() -> pd.DataFrame:
    """A feature that is, in effect, a copy of the target -- perfect information the model would
    never see in production (`understanding.leakage_indicators`)."""
    return pd.DataFrame(
        {
            "label": [0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
            "leaky": [0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
            "other": list(range(1, 11)),
        }
    )


def inappropriate_test_fixture() -> pd.DataFrame:
    """Three groups, but the scenario asks for a two-sample test -- `methods.run_method` must
    refuse by name and offer the alternative that actually fits (`one_way_anova`)."""
    return pd.DataFrame({"value": [1, 2, 3, 4, 5, 6, 6, 7, 8, 9], "group": list("aaabbbcccc")})


AMBIGUOUS_QUESTIONS: tuple[str, ...] = (
    "Which region should we focus on?",
    "What should we do next?",
    "Tell me something interesting about this data.",
)

UNAMBIGUOUS_QUESTION = "What is the significant difference between groups A and B?"
