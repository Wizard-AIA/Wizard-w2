"""The agent's action space, and the parser that reads a model's choice.

Why this exists
---------------
The previous design fixed a plan before touching the data and then executed it
step by step. That cannot recover when the data disagrees with the plan, which
is the ordinary case for a real analytical question: you discover the join key
is dirty, or that "active customer" means three different things in three
tables, only once you have looked.

So the agent picks its next move each iteration, from real output. This module
defines the moves and — far more importantly — decodes the model's answer
robustly, because the whole loop is worthless if a 1.5B model saying
``Action: CODE.`` instead of ``ACTION: code`` derails the run.

Parsing philosophy
------------------
**A malformed decision is never fatal.** Every unparseable response resolves to
a sensible default rather than an error: mid-run that is ``code`` (keep working),
and on the final permitted iteration it is forced to ``answer``. A model that
cannot follow the format still completes the analysis; it just gets less say in
how. DABstep found prompt-format sensitivity to be a top failure mode for
exactly this reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class ActionKind(StrEnum):
    """What the agent can do with one iteration."""

    #: Look at the data — schema, distributions, sample rows, null structure.
    #: Answered deterministically from the frame, costing no LLM call.
    INSPECT = "inspect"
    #: Write Python for a stated subgoal and run it. The workhorse.
    CODE = "code"
    #: Retrieve from the session's context documents (data dictionary, rules).
    CONSULT = "consult"
    #: Search the web. Always requires explicit user consent.
    SEARCH = "search"
    #: Revise the plan from what has been learned so far.
    REFLECT = "reflect"
    #: Split into independent sub-questions and investigate them at once, each
    #: in its own isolated subagent. Only offered above the compact tier.
    PARALLEL = "parallel"
    #: Stop investigating; synthesise the final answer.
    ANSWER = "answer"


#: Actions a model may select. ``SEARCH`` is excluded — it leaves the machine,
#: so it is only reachable through the plan's explicit ``SEARCH:`` directive,
#: which routes through the consent gate.
SELECTABLE = (
    ActionKind.INSPECT,
    ActionKind.CODE,
    ActionKind.CONSULT,
    ActionKind.REFLECT,
    ActionKind.PARALLEL,
    ActionKind.ANSWER,
)

#: Words a model reaches for when it means one of the above. Small models
#: paraphrase constantly; matching only the exact token throws away most of
#: their correct decisions.
SYNONYMS: dict[str, ActionKind] = {
    "inspect": ActionKind.INSPECT,
    "examine": ActionKind.INSPECT,
    "explore": ActionKind.INSPECT,
    "look": ActionKind.INSPECT,
    "profile": ActionKind.INSPECT,
    "understand": ActionKind.INSPECT,
    "describe": ActionKind.INSPECT,
    "code": ActionKind.CODE,
    "compute": ActionKind.CODE,
    "calculate": ActionKind.CODE,
    "execute": ActionKind.CODE,
    "run": ActionKind.CODE,
    "analyze": ActionKind.CODE,
    "analyse": ActionKind.CODE,
    "plot": ActionKind.CODE,
    "visualize": ActionKind.CODE,
    "visualise": ActionKind.CODE,
    "consult": ActionKind.CONSULT,
    "docs": ActionKind.CONSULT,
    "documentation": ActionKind.CONSULT,
    "lookup": ActionKind.CONSULT,
    "reference": ActionKind.CONSULT,
    "reflect": ActionKind.REFLECT,
    "replan": ActionKind.REFLECT,
    "revise": ActionKind.REFLECT,
    "rethink": ActionKind.REFLECT,
    "parallel": ActionKind.PARALLEL,
    "parallelize": ActionKind.PARALLEL,
    "parallelise": ActionKind.PARALLEL,
    # No "split": prose reasoning routinely says "split the data by region"
    # meaning an ordinary `code` step, and the prose-fallback matcher below
    # would read that as a chosen `parallel` action.
    "fanout": ActionKind.PARALLEL,
    "delegate": ActionKind.PARALLEL,
    "subagents": ActionKind.PARALLEL,
    "answer": ActionKind.ANSWER,
    "finish": ActionKind.ANSWER,
    "done": ActionKind.ANSWER,
    "conclude": ActionKind.ANSWER,
    "respond": ActionKind.ANSWER,
    "complete": ActionKind.ANSWER,
}


# Markdown decoration is tolerated on either side of the colon: models emit
# `**ACTION:** code` at least as often as the `ACTION: code` they were asked for,
# and rejecting the bold form throws away a correct decision over formatting.
def _labelled(*names: str) -> re.Pattern[str]:
    alternatives = "|".join(names)
    return re.compile(
        rf"^\s*[*_`]*\s*(?:{alternatives})\s*[*_`]*\s*[:=\-]\s*[*_`]*\s*(.+)$",
        re.IGNORECASE | re.MULTILINE,
    )


ACTION_LINE = _labelled("ACTION", "NEXT", "STEP")
GOAL_LINE = _labelled("GOAL", "SUBGOAL", "TASK")
WHY_LINE = _labelled("WHY", "REASON", "BECAUSE", "RATIONALE")

#: Strips decoration a model wraps its choice in: `**code**`, `"code"`, `[code]`.
DECORATION = re.compile(r"[^a-z]+")


@dataclass
class Decision:
    """One resolved choice of what to do next."""

    kind: ActionKind
    goal: str = ""
    rationale: str = ""
    #: True when the model's output could not be read and a default was applied.
    #: Surfaced so the run's quality can be judged, and so tests can assert that
    #: garbage in does not become a crash.
    inferred: bool = False

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "kind": self.kind.value,
            "goal": self.goal,
            "rationale": self.rationale,
            "inferred": self.inferred,
        }


def _match_word(word: str) -> ActionKind | None:
    cleaned = DECORATION.sub("", word.lower())
    return SYNONYMS.get(cleaned)


def _clean(text: str) -> str:
    """Strips markdown decoration and surrounding punctuation from a field."""
    return text.strip().strip("`\"'*_ -—:,.").strip()


def parse_decision(
    raw: str,
    *,
    allowed: tuple[ActionKind, ...] = SELECTABLE,
    default: ActionKind = ActionKind.CODE,
) -> Decision:
    """Reads a next-action decision out of a model response.

    Tried in order, most explicit first:

    1. An ``ACTION:`` line, which is what the prompt asks for.
    2. The first recognisable action word anywhere in the text — covers "I will
       now compute the monthly totals", which is what a model that ignored the
       format actually writes.
    3. ``default``.

    Never raises. ``allowed`` narrows the menu (a compact-tier run has no
    reflection, and a session with no documents has nothing to consult), and a
    choice outside it falls through to the next strategy rather than being
    honoured.
    """
    text = (raw or "").strip()
    goal = ""
    rationale = ""

    goal_match = GOAL_LINE.search(text)
    if goal_match:
        goal = _clean(goal_match.group(1))
    why_match = WHY_LINE.search(text)
    if why_match:
        rationale = _clean(why_match.group(1))

    action_match = ACTION_LINE.search(text)
    if action_match:
        candidate = action_match.group(1).strip()
        # The line may be "code — compute revenue"; the action is the first
        # *word-like* token. Leading decoration is skipped rather than treated
        # as a failed match, since `**` is a token under this split.
        for token in re.split(r"[\s,;:.\-—]+", candidate):
            if not token or not DECORATION.sub("", token.lower()):
                continue  # leading `**`, `` ` `` and friends are not the action
            kind = _match_word(token)
            if kind is not None and kind in allowed:
                if not goal:
                    # Everything after the action word is a usable goal.
                    goal = _clean(candidate.split(token, 1)[-1])
                return Decision(kind=kind, goal=goal, rationale=rationale)
            # Only the first real word can be the action. Scanning further would
            # match a verb inside the goal text and pick the wrong action.
            break

    # No usable ACTION line. Fall back to the first action word in the prose.
    for token in re.findall(r"[A-Za-z]+", text):
        kind = _match_word(token)
        if kind is not None and kind in allowed:
            return Decision(kind=kind, goal=goal or text.splitlines()[0][:200], rationale=rationale, inferred=True)

    resolved = default if default in allowed else allowed[0]
    return Decision(
        kind=resolved,
        goal=goal or (text.splitlines()[0][:200] if text else ""),
        rationale=rationale,
        inferred=True,
    )


@dataclass
class Step:
    """One completed iteration, as it will be shown back to the model."""

    index: int
    kind: ActionKind
    goal: str
    #: What actually came back — stdout, a schema summary, a retrieved passage.
    observation: str
    ok: bool = True
    code: str = ""
    duration_ms: int = 0
    retries: int = 0

    def render(self, limit: int) -> str:
        """The transcript form fed into the next decision."""
        body = self.observation.strip()
        if len(body) > limit:
            head = body[: limit // 2]
            tail = body[-limit // 4 :]
            body = f"{head}\n... [{len(body) - len(head) - len(tail)} characters omitted] ...\n{tail}"
        status = "" if self.ok else " (FAILED)"
        goal = f" — {self.goal}" if self.goal else ""
        return f"[{self.index}] {self.kind.value}{goal}{status}\n{body}"


@dataclass
class Investigation:
    """The running record of one turn.

    This is the agent's working memory. It is rendered into every decision
    prompt, so it is deliberately bounded: recent steps in full, older ones
    collapsed to their first line. Without that a long investigation grows its
    own prompt until the model loses the question at the top of it.
    """

    #: How many trailing steps are shown at full length.
    RECENT_IN_FULL = 3

    steps: list[Step] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def record(self, step: Step) -> None:
        self.steps.append(step)

    def note_finding(self, text: str) -> None:
        cleaned = text.strip()
        if cleaned and cleaned not in self.findings:
            self.findings.append(cleaned)

    def note_assumption(self, text: str) -> None:
        cleaned = text.strip()
        if cleaned and cleaned not in self.assumptions:
            self.assumptions.append(cleaned)

    @property
    def last_successful_code(self) -> str:
        for step in reversed(self.steps):
            if step.kind is ActionKind.CODE and step.ok and step.code:
                return step.code
        return ""

    @property
    def executed_output(self) -> str:
        """Every successful execution's output, oldest first.

        This is what the answer is grounded against — a number that appears
        nowhere in here was not computed, it was invented.
        """
        return "\n".join(step.observation for step in self.steps if step.ok)

    def render(self, observation_chars: int) -> str:
        if not self.steps:
            return "*Nothing has been run yet.*"

        recent = self.steps[-self.RECENT_IN_FULL :]
        older = self.steps[: -self.RECENT_IN_FULL] if len(self.steps) > self.RECENT_IN_FULL else []

        lines: list[str] = []
        for step in older:
            first = step.observation.strip().splitlines()
            summary = first[0][:160] if first else ""
            lines.append(f"[{step.index}] {step.kind.value} — {step.goal}: {summary}")
        for step in recent:
            lines.append(step.render(observation_chars))
        return "\n\n".join(lines)
