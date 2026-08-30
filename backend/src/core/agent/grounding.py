"""Checks that make a generated answer trustworthy enough to act on.

Two deterministic passes, both cheap and neither needing an LLM call:

**Grounding.** Every number in the answer must trace back to something that was
actually computed. A language model asked to "explain this result" will happily
round, re-derive or invent a figure, and the more fluent the prose the less
visible that is. DABstep found hallucinated and mis-formatted results to be a
leading cause of wrong answers on multi-step tasks.

**Assumptions.** Analysis code makes silent decisions — dropping nulls, taking a
head, coercing bad dates to NaT — and every one of them changes what the number
means. These are read back out of the code that ran and reported alongside the
answer, rather than left for the user to discover by reading the source.

Both *report*; neither rewrites the answer. Editing model output after the fact
is precisely the mistake this codebase already made once, when the frontend
regex-stripped tracebacks and numeric rows out of responses and deleted real
results along with them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


#: A number as it appears in prose or in output: 1,234.56  -3.2e4  99%  $12.00
NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")

#: Values too common to carry information. Matching them against output would
#: succeed by coincidence far more often than it would mean anything, and
#: flagging them would bury the real findings in noise.
TRIVIAL = frozenset({"0", "1", "2", "3", "4", "5", "10", "100"})

#: An answer that rounds is reporting, not inventing: "3.14" for an output of
#: 3.14159265 is correct. Tolerance is therefore taken from the *answer's* own
#: precision — a figure written to two decimals is compared to two decimals —
#: rather than at some fixed width, which would reject every legitimate rounding
#: whose precision happened not to match.
#:
#: Large round numbers get a relative tolerance too, so "revenue was 1.2 million"
#: against an output of 1,234,567 is not flagged as fabricated.
RELATIVE_TOLERANCE = 0.005


def _normalise(raw: str) -> str | None:
    """Canonical form of a numeric literal, or None when it is not one."""
    cleaned = raw.replace(",", "").strip()
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _as_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _decimals(raw: str) -> int:
    """Decimal places the literal was written to."""
    cleaned = raw.replace(",", "").strip()
    if "e" in cleaned.lower():
        return 0
    _, _, fraction = cleaned.partition(".")
    return len(fraction)


def _matches(stated: str, observed: float, scale: float = 1.0) -> bool:
    """Whether ``observed`` could have been reported as ``stated``.

    Three ways it can: exactly; by rounding ``observed`` to the precision the
    answer was written to; or within a small relative margin. ``scale`` carries
    a magnitude word, so "1.23 million" is compared against 1,230,000.
    """
    value = _as_float(stated)
    if value is None:
        return False
    value *= scale

    if value == observed:
        return True

    # A scaled figure is rounded in the scaled units: "1.23 million" is precise
    # to 10,000, not to 0.01.
    places = _decimals(stated)
    if scale == 1.0:
        if round(observed, places) == round(value, places):
            return True
    else:
        step = scale / (10**places)
        if abs(value - observed) <= step / 2:
            return True

    magnitude = max(abs(value), abs(observed))
    return magnitude > 0 and abs(value - observed) / magnitude <= RELATIVE_TOLERANCE


#: Magnitude words an answer uses where the output printed the full figure.
#: Bare "m" and "b" are deliberately absent — they collide with units (metres,
#: bytes) far too often to be worth the extra coverage.
SCALES: dict[str, float] = {
    "thousand": 1e3,
    "k": 1e3,
    "million": 1e6,
    "mn": 1e6,
    "billion": 1e9,
    "bn": 1e9,
    "trillion": 1e12,
}

SCALED_NUMBER = re.compile(
    NUMBER.pattern + r"\s*(thousand|million|billion|trillion|bn|mn|k)\b",
    re.IGNORECASE,
)


#: Scientific notation in LaTeX, text, or math formats:
#: 4.3650 \times 10^{-94}, 1.23 * 10^-5, 1.23 \cdot 10^{4}
LATEX_SCIENTIFIC = re.compile(
    r"(-?\d[\d,]*(?:\.\d+)?)\s*(?:\\times|\\cdot|\*|x|·)\s*10\^\{?([-+]?\d+)\}?",
    re.IGNORECASE,
)

#: Inequality prefixes for statistical p-value thresholds: < 0.0001, <= 0.05, p < 0.001
INEQUALITY_PREFIX = re.compile(r"(?:<|<=|≤|\\le|\\lt|p\s*<|p-value\s*<)\s*$", re.IGNORECASE)
STANDARD_THRESHOLDS = frozenset({"0.05", "0.01", "0.001", "0.0001", "0.00001"})


def extract_numbers(text: str) -> list[str]:
    """Every numeric literal in ``text``, in order of appearance."""
    return [match.group(0) for match in NUMBER.finditer(text or "")]


def _extract_all_floats(text: str) -> list[float]:
    """Extracts all float values including LaTeX scientific notation."""
    floats: list[float] = []
    sci_spans: list[tuple[int, int]] = []
    for m in LATEX_SCIENTIFIC.finditer(text or ""):
        sci_spans.append((m.start(), m.end()))
        try:
            floats.append(float(f"{m.group(1).replace(',', '')}e{m.group(2)}"))
        except ValueError:
            pass
    for m in NUMBER.finditer(text or ""):
        if any(s <= m.start() and m.end() <= e for s, e in sci_spans):
            continue
        v = _as_float(m.group(0))
        if v is not None:
            floats.append(v)
    return floats


def _scale_at(text: str, position: int) -> float:
    """Multiplier for a magnitude word directly following ``position``."""
    match = SCALED_NUMBER.match(text, position)
    if not match:
        return 1.0
    return SCALES.get(match.group(1).lower(), 1.0)


@dataclass
class GroundingReport:
    """Which of the answer's numbers were traceable to real output."""

    checked: int = 0
    grounded: int = 0
    ungrounded: list[str] = field(default_factory=list)
    #: The literals that did ground, in answer order -- what provenance turns into claim nodes.
    grounded_values: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.ungrounded

    @property
    def ratio(self) -> float:
        return 1.0 if self.checked == 0 else self.grounded / self.checked

    def warning(self) -> str | None:
        """One sentence for the user, or None when nothing is wrong."""
        if self.ok:
            return None
        shown = ", ".join(self.ungrounded[:5])
        more = f" (and {len(self.ungrounded) - 5} more)" if len(self.ungrounded) > 5 else ""
        return (
            f"These figures in the answer do not appear in any execution output: {shown}{more}. "
            "Treat them as unverified."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "grounded": self.grounded,
            "ungrounded": self.ungrounded,
            "grounded_values": self.grounded_values,
            "ok": self.ok,
            "ratio": round(self.ratio, 3),
        }


def check_grounding(answer: str, executed_output: str, instruction: str = "") -> GroundingReport:
    """Verifies each number in ``answer`` traces to output or to the question.

    A number counts as grounded when it appears in the executed output exactly,
    when some output value rounds to it, when formatted in LaTeX scientific
    notation matching calculated values, when expressing a bounding inequality
    for zero/near-zero p-values (< 0.0001), or when the user put it in the question
    themselves ("show me the top 20" legitimises a 20 in the reply).
    """
    report = GroundingReport()
    if not answer.strip():
        return report

    observed = _extract_all_floats(executed_output)
    asked = {n for n in (_normalise(t) for t in extract_numbers(instruction)) if n}

    seen: set[str] = set()
    sci_spans: list[tuple[int, int]] = []

    # First pass: check LaTeX scientific notation as single numbers
    for match in LATEX_SCIENTIFIC.finditer(answer):
        sci_spans.append((match.start(), match.end()))
        token = match.group(0)
        try:
            val = float(f"{match.group(1).replace(',', '')}e{match.group(2)}")
        except ValueError:
            continue
        normalised = f"{val:.4e}"
        if normalised in seen:
            continue
        seen.add(normalised)
        report.checked += 1
        if any(abs(val - obs) <= max(1e-12, abs(obs) * 0.01) for obs in observed):
            report.grounded += 1
            report.grounded_values.append(token)
        else:
            report.ungrounded.append(token)

    # Second pass: standard numbers not enclosed in scientific spans
    for match in NUMBER.finditer(answer):
        if any(s <= match.start() and match.end() <= e for s, e in sci_spans):
            continue
        token = match.group(0)
        normalised = _normalise(token)
        if normalised is None or normalised in TRIVIAL or normalised in seen:
            continue
        seen.add(normalised)
        report.checked += 1

        scale = _scale_at(answer, match.start())
        if normalised in asked or any(_matches(token, value, scale) for value in observed):
            report.grounded += 1
            report.grounded_values.append(token)
            continue

        # Check inequality threshold bounds (e.g. p < 0.0001 against 0.0000)
        prefix = answer[: match.start()]
        if INEQUALITY_PREFIX.search(prefix):
            val = _as_float(token)
            if val is not None and (normalised in STANDARD_THRESHOLDS or any(obs <= val for obs in observed)):
                report.grounded += 1
                report.grounded_values.append(token)
                continue

        report.ungrounded.append(token)

    return report


def check_table_grounding(answer_text: str, dataframes: list[Any]) -> dict[str, Any]:
    """Extracts Markdown tables from answer_text and checks grounding against dataframes."""

    unmatched: list[str] = []
    result: dict[str, Any] = {
        "tables_found": 0,
        "grounded": True,
        "unmatched_cells": unmatched,
        "grounding_ratio": 1.0,
    }

    if not answer_text.strip():
        return result

    tables = []
    lines = answer_text.split("\n")
    current_table = []

    for line in lines:
        if line.strip().startswith("|") and line.strip().endswith("|"):
            current_table.append(line.strip())
        else:
            if current_table:
                tables.append(current_table)
                current_table = []
    if current_table:
        tables.append(current_table)

    if not tables:
        return result

    result["tables_found"] = len(tables)

    import pandas as pd

    all_values = []
    for df in dataframes:
        if isinstance(df, pd.DataFrame):
            all_values.extend([str(c) for c in df.columns])
            for _, row in df.iterrows():
                all_values.extend([str(v) for v in row.values])
        elif isinstance(df, pd.Series):
            all_values.append(str(df.name))
            all_values.extend([str(v) for v in df.values])
        else:
            all_values.append(str(df))

    normalized_values = {v.strip().lower() for v in all_values}

    total_cells = 0
    matched_cells = 0

    for table in tables:
        for row in table:
            if set(row.replace("|", "").replace(" ", "").replace("-", "").replace(":", "")) == set():
                continue

            cells = [cell.strip() for cell in row.split("|")[1:-1]]
            for cell in cells:
                if not cell:
                    continue
                total_cells += 1
                cell_normalized = cell.lower()

                if cell_normalized in normalized_values:
                    matched_cells += 1
                else:
                    cell_num = _as_float(cell)
                    if cell_num is not None:
                        matched = False
                        for val in normalized_values:
                            val_num = _as_float(val)
                            if val_num is not None and _matches(cell, val_num):
                                matched = True
                                break
                        if matched:
                            matched_cells += 1
                        else:
                            unmatched.append(cell)
                    else:
                        unmatched.append(cell)

    if total_cells > 0:
        result["grounding_ratio"] = matched_cells / total_cells
        result["grounded"] = result["grounding_ratio"] == 1.0

    return result


# ---------------------------------------------------------------------- #
# Assumption ledger
# ---------------------------------------------------------------------- #

#: (code marker, what it silently did to the result). Ordered so the more
#: specific pattern is reported instead of the general one it contains.
CODE_ASSUMPTIONS: tuple[tuple[str, str], ...] = (
    ("errors='coerce'", "Values that could not be parsed were turned into nulls rather than raising."),
    ('errors="coerce"', "Values that could not be parsed were turned into nulls rather than raising."),
    ("dropna(", "Rows with missing values were excluded, so this is computed on a subset."),
    ("fillna(", "Missing values were substituted, which moves any mean, sum or count computed after it."),
    ("drop_duplicates(", "Duplicate rows were removed before aggregating."),
    ("nlargest(", "Only the top-ranked rows are represented in this result."),
    ("nsmallest(", "Only the bottom-ranked rows are represented in this result."),
    (".sample(", "Computed on a random sample, not the full table."),
    ("how='inner'", "An inner join was used, so rows without a match on both sides were dropped."),
    ('how="inner"', "An inner join was used, so rows without a match on both sides were dropped."),
    ("clip(", "Extreme values were clamped to a bound before aggregating."),
    ("astype(", "A column's type was converted, which can truncate or round values."),
    ("resample(", "Rows were re-bucketed onto a different time grain."),
    ("interpolate(", "Gaps were filled by interpolation, producing values that were not measured."),
)


def assumptions_from_code(code: str) -> list[str]:
    """Silent analytical decisions made by the code that ran.

    Deliberately literal: it reports what the code demonstrably did, not what a
    model claims it did. A false positive here is a redundant caveat; a false
    negative is a number whose meaning the reader has misunderstood.
    """
    if not code:
        return []
    found: list[str] = []
    for marker, description in CODE_ASSUMPTIONS:
        if marker in code and description not in found:
            found.append(description)
    return found


def assumptions_from_profile(profile: dict) -> list[str]:
    """Caveats owed to how the dataset itself was loaded."""
    notes: list[str] = []
    if profile.get("truncated"):
        original = profile.get("original_rows")
        total = f" of {original:,}" if isinstance(original, int) else ""
        notes.append(
            f"The table was down-sampled to {profile.get('rows', 0):,} rows{total} at load time, "
            "so counts and totals are not the full population."
        )
    dropped = profile.get("dropped_columns") or []
    if dropped:
        notes.append(
            f"These columns were dropped during loading and are not available: {', '.join(map(str, dropped))}."
        )
    renamed = profile.get("renamed_columns") or {}
    if renamed:
        pairs = ", ".join(f"{before} → {after}" for before, after in list(renamed.items())[:5])
        notes.append(f"Column names were sanitised on load: {pairs}.")
    return notes
