"""Turn routing: how much machinery a message actually needs.

Every message used to enter one pipeline (plan -> loop -> verify -> answer), so
``hi`` after an analysis planned, wrote code and ran it. Routing fixes the
*shape of the work*, not the wording of any one message: it reads evidence from
the message and the session, and picks the smallest workflow that can serve it.

Applied in this order:

1. **Signals** (`extract_signals`) are facts read from the message: which columns
   it names, which operations it asks for, whether it addresses Wizard's last
   answer, whether it asks for a plan. They are evidence with weights, never a
   verdict on their own, and they match whole tokens so ``hi`` does not fire
   inside ``which``.
2. **Route** (`route_turn`) turns signals plus session context into a workflow.
   A message with no task evidence is conversation whatever its wording, which
   is what makes ``cheers`` and ``lol nice`` work without being on a list.
3. **Escalation** covers the uncertain band. When a message is *probably*
   conversation but could be a request (or is in a language the signals do not
   know), the conversational reply is itself the classifier: the model answers,
   or replies with `ESCALATE_SENTINEL` to hand the turn to the analysis loop.
   One small call, no separate router model.

Mode is a *policy* applied after intent (`apply_mode`). It changes how much
work an analytic message gets; it never turns conversation into analysis or the
reverse. Nothing here calls a model, touches a session, or does I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from src.core.llm.router import TaskTier


#: What a converse-or-escalate reply says, alone, when the question needs the data.
ESCALATE_SENTINEL = "[[NEEDS_ANALYSIS]]"


class Intent(StrEnum):
    """What the user is doing with this message."""

    CONVERSATION = "conversation"  # greeting, thanks, acknowledgement, chat about Wizard
    FOLLOW_UP = "follow_up"  # about Wizard's previous answer; may or may not need new work
    DATA_QUESTION = "data_question"  # the dataset's structure: columns, rows, types
    COMPUTATION = "computation"  # one figure or table from the data
    VISUALIZATION = "visualization"  # one chart
    INVESTIGATION = "investigation"  # diagnose, compare, validate, model
    MULTI_STEP = "multi_step"  # several dependent deliverables in one message
    PLAN_REQUEST = "plan_request"  # asks for a plan, not for execution
    EXECUTE_PLAN = "execute_plan"  # confirms a plan that is waiting


class Workflow(StrEnum):
    """The machinery a route runs."""

    CONVERSE = "converse"  # one conversational reply; no tools, no dataset access
    INSPECT = "inspect"  # dataset facts straight from the frame, then a written answer
    DIRECT = "direct"  # write code, run it, answer. No planner, no verification
    AGENTIC = "agentic"  # the investigate loop, with verification
    PLAN_ONLY = "plan_only"  # produce a plan and stop for approval
    EXECUTE_PLAN = "execute_plan"  # run the plan the user confirmed


class Complexity(StrEnum):
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


class PlanPolicy(StrEnum):
    """Whether a planning call happens, and whether the user sees it first."""

    NONE = "none"  # the loop's first action is enough
    SELF = "self"  # the manager plans, then proceeds
    GATED = "gated"  # the manager plans and the user confirms before anything runs
    ONLY = "only"  # the manager plans and the turn ends there


@dataclass(frozen=True)
class TurnContext:
    """What the router may know about the session. Plain values, no session object."""

    has_dataset: bool = False
    columns: tuple[str, ...] = ()
    table_names: tuple[str, ...] = ()
    has_prior_turn: bool = False
    #: A plan produced by an earlier turn is waiting for the user's decision.
    pending_plan: bool = False


@dataclass(frozen=True)
class Signals:
    """Evidence read from one message. Counts and flags only; never the text."""

    n_tokens: int = 0
    n_clauses: int = 1
    is_question: bool = False
    data_refs: int = 0
    #: Distinct operation families the message asks for (aggregate, transform, ...).
    operations: frozenset[str] = frozenset()
    heavy: frozenset[str] = frozenset()  # investigative or statistical asks
    structure: bool = False  # asks about the dataset's shape, columns or types
    condition: bool = False  # a filter, threshold or comparison, so a frame fact will not do
    visual: bool = False
    sequencing: int = 0  # then / after that / finally ...
    deliverable: bool = False  # report / summary / recommendations
    open_analysis: bool = False  # "analyze", "explore", "assess"
    open_ended: bool = False  # ... with nothing specific to analyze
    social: bool = False  # greeting, thanks, acknowledgement, farewell
    addresses_you: bool = False  # asks about what Wizard did, said or is
    plan_requested: bool = False
    confirms_plan: bool = False
    #: Phrased as a request to do something ("show me..."), not a statement or a question.
    imperative: bool = False

    @property
    def computes(self) -> bool:
        """Whether the message asks for something to be calculated or changed."""
        return bool(self.operations & {"aggregate", "combine", "transform", "export"})

    @property
    def task_evidence(self) -> bool:
        return bool(
            self.data_refs
            or self.operations
            or self.heavy
            or self.structure
            or self.visual
            or self.deliverable
            or self.open_analysis
        )

    @property
    def steps(self) -> int:
        """Independent deliverables the message asks for."""
        return (
            len(self.operations)
            + int(self.visual)
            + int(self.deliverable)
            + int(self.open_analysis)
            + min(len(self.heavy), 3)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.n_tokens,
            "clauses": self.n_clauses,
            "question": self.is_question,
            "data_refs": self.data_refs,
            "operations": sorted(self.operations),
            "heavy": sorted(self.heavy),
            "structure": self.structure,
            "condition": self.condition,
            "visual": self.visual,
            "sequencing": self.sequencing,
            "open_ended": self.open_ended,
            "social": self.social,
            "addresses_you": self.addresses_you,
            "plan_requested": self.plan_requested,
        }


@dataclass(frozen=True)
class Route:
    """The routing decision for one turn, and why."""

    intent: Intent
    workflow: Workflow
    complexity: Complexity
    plan: PlanPolicy = PlanPolicy.NONE
    #: Whether a second execution re-derives the result. Costly; only where it pays.
    verify: bool = False
    #: The conversational reply may hand the turn to the analysis loop.
    escalate: bool = False
    #: Run the loop at its deep iteration budget.
    deep: bool = False
    #: The dataset was needed and is absent: converse, and say what is missing.
    needs_data: bool = False
    reasons: tuple[str, ...] = ()
    source: str = "rules"  # rules | mode | escalation | approval

    @property
    def task_tier(self) -> TaskTier:
        """The model tier this route justifies (see `LLMProvider.model_for_task`)."""
        if self.workflow is Workflow.CONVERSE:
            # An uncertain message may be a real request, so it keeps the normal
            # model; only a decided social turn is safe to send to a small one.
            return TaskTier.STANDARD if self.escalate else TaskTier.LIGHTWEIGHT
        if self.workflow is Workflow.INSPECT:
            return TaskTier.LIGHTWEIGHT
        if self.complexity is Complexity.COMPLEX or self.workflow is Workflow.PLAN_ONLY:
            return TaskTier.REASONING_HEAVY
        return TaskTier.STANDARD

    def budget_mode(self, mode: str) -> str:
        """The mode `settings.budget_for` should size this turn with.

        Direct and inspect turns are one shot by construction, so they take the
        one-shot budget unless the user asked for depth explicitly.
        """
        if self.workflow is Workflow.INSPECT:
            return "fast"  # a mode is how hard to work on a task, and this is not one
        if self.deep or mode == "deep":
            return "deep"
        if mode == "fast" or self.workflow is Workflow.DIRECT:
            return "fast"
        return "auto"

    def with_(self, **changes: Any) -> Route:
        return Route(**{**self.__dict__, **changes})

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "workflow": self.workflow.value,
            "complexity": self.complexity.value,
            "plan": self.plan.value,
            "verify": self.verify,
            "escalate": self.escalate,
            "deep": self.deep,
            "needs_data": self.needs_data,
            "reasons": list(self.reasons),
            "source": self.source,
        }


# --------------------------------------------------------------------------- #
# Evidence tables
#
# Entries of five or more letters match any token that starts with them
# (`correl` -> correlation, correlated); shorter entries must equal the token
# (after a plural `s` is dropped). `hi` therefore never fires inside `which`.
# These tables are *evidence* feeding a score. None decides a route on its own,
# and a message matching none of them is not "unknown": it is conversation, or
# an escalation for the model to settle.
# --------------------------------------------------------------------------- #

#: Operation families a single deliverable can ask for. Two different families
#: in one message means two dependent steps; two words of one family do not.
_OPERATIONS: dict[str, tuple[str, ...]] = {
    "aggregate": (
        "calculat",
        "comput",
        "count",
        "sum",
        "total",
        "mean",
        "average",
        "avg",
        "median",
        "min",
        "max",
        "minimum",
        "maximum",
        "percent",
        "ratio",
        "rate",
        "std",
        "varianc",
        "quantile",
        "percentile",
        "distribut",
        "frequenc",
    ),
    "select": (
        "filter",
        "sort",
        "rank",
        "top",
        "bottom",
        "group",
        "aggregat",
        "pivot",
        "select",
        "unique",
        "distinct",
        "highest",
        "lowest",
        "largest",
        "smallest",
        "biggest",
        "best",
        "worst",
        "most",
        "least",
    ),
    "combine": ("join", "merge", "concat", "append", "lookup"),
    "transform": (
        "clean",
        "impute",
        "dedup",
        "deduplic",
        "normaliz",
        "normalis",
        "standardiz",
        "standardis",
        "encode",
        "transform",
        "convert",
        "rename",
        "drop",
        "fill",
        "resample",
        "parse",
        "reshape",
    ),
    "export": ("export", "download"),
}

_VISUAL = (
    "plot",
    "chart",
    "graph",
    "visuali",
    "histogram",
    "heatmap",
    "scatter",
    "boxplot",
    "barplot",
    "pie",
    "dashboard",
    "diagram",
)

#: Investigative or statistical asks. Each is a step that needs its own evidence.
_HEAVY = (
    "why",
    "correlat",
    "regress",
    "cluster",
    "segment",
    "forecast",
    "predict",
    "classif",
    "model",
    "anomal",
    "outlier",
    "hypothes",
    "significan",
    "statistic",
    "anova",
    "causal",
    "cause",
    "driver",
    "factor",
    "contribut",
    "attribut",
    "validat",
    "seasonal",
    "cohort",
    "trend",
    "compar",
    "diagnos",
    "investigat",
    "root",
    "impact",
    "relationship",
)

_STRUCTURE = frozenset(
    {
        "column",
        "field",
        "schema",
        "dtype",
        "datatype",
        "shape",
        "dimension",
        "row",
        "head",
        "tail",
        "preview",
        "header",
        "variable",
        "missing",
        "null",
        "nan",
    }
)

_DELIVERABLE = ("report", "summar", "insight", "recommend", "finding", "writeup", "presentation")

#: Verbs that ask for open-ended analysis rather than one figure.
_OPEN_ANALYSIS = ("analy", "explor", "examin", "assess", "evaluat", "study", "profile", "eda")

#: Words that order steps, which is how a message says "several dependent things".
_SEQUENCING = frozenset({"then", "after", "afterwards", "next", "finally", "followed", "subsequently", "lastly"})

#: Speech acts that are complete without a task: greeting, thanks, acknowledgement, farewell.
#: A closed class, used as evidence only. A message with *no* task evidence is
#: conversation with or without a word from this list.
_SOCIAL = frozenset(
    {
        "hi",
        "hii",
        "hello",
        "hey",
        "hiya",
        "howdy",
        "yo",
        "greetings",
        "morning",
        "afternoon",
        "evening",
        "thanks",
        "thank",
        "thx",
        "ty",
        "cheers",
        "appreciate",
        "appreciated",
        "ok",
        "okay",
        "k",
        "kk",
        "sure",
        "cool",
        "great",
        "nice",
        "awesome",
        "perfect",
        "understood",
        "alright",
        "fine",
        "yes",
        "yep",
        "yeah",
        "no",
        "nope",
        "bye",
        "goodbye",
        "later",
        "lol",
        "haha",
        "wow",
        "hmm",
        "sorry",
    }
)

#: Approval of a waiting plan, when the user types it instead of pressing the button.
_CONFIRM = frozenset({"execute", "run", "go", "proceed", "yes", "yep", "yeah", "ok", "okay", "approve", "confirm"})

#: A "mean" that is the verb ("what does that mean"), not the statistic.
_MEAN_VERB_PREV = frozenset(
    {"do", "does", "did", "you", "it", "that", "this", "which", "they", "i", "we", "to", "would"}
)

_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_CLAUSE_SPLIT = re.compile(r"[;\n]|,\s+(?:and\s+|then\s+)?|\s+and\s+then\s+|\s+then\s+|\s+and\s+(?=\w)", re.IGNORECASE)

#: "data types" and "how big is this" are structure; a bare "type" is not ("what type of customer churned").
_TYPE_OF_COLUMNS = re.compile(
    r"\b(?:data|column|field|variable)\s*types?\b|\bhow\s+(?:big|large|long|wide)\b|\bsize\s+of\b"
    r"|\bnumber\s+of\s+(?:rows|columns|records|fields)\b",
    re.IGNORECASE,
)

#: A filter or comparison. A structural question that carries one ("rows where
#: revenue > 100") needs a computation, not a fact read off the frame. A number
#: after head/tail/first/last/top is a preview size, not a condition.
_CONDITION = re.compile(
    r"\bwhere\b|\bbetween\b|[<>=!]|\b(?:greater|less|more|fewer|over|under|above|below|equals?|contains?|starts?|ends?"
    r"|matching|matches|older|younger|larger|smaller|higher|lower)\b"
    r"|(?<!head )(?<!tail )(?<!first )(?<!last )(?<!top )(?<!bottom )\b\d+(?:\.\d+)?\b",
    re.IGNORECASE,
)

#: The user asked for a plan, or for nothing to run yet. An explicit instruction
#: is the one place wording *should* decide: it is a constraint the user stated,
#: not an intent to be inferred. The words between verb and noun may not
#: introduce a purpose ("build a model to plan inventory" is not a plan request).
_NOT_PURPOSE = r"(?:(?!to\b|for\b|and\b|that\b|which\b|of\b)\w+\s+)"
_PLAN_ASK = re.compile(
    r"\b(?:plan\s+first|plan\s+only|only\s+(?:a\s+|the\s+)?plan|just\s+(?:a\s+|the\s+)?plan"
    r"|(?:make|create|write|draft|outline|prepare|propose|design|give\s+me|come\s+up\s+with)"
    r"\s+(?:me\s+)?(?:an?\s+|the\s+|your\s+)?" + _NOT_PURPOSE + r"{0,3}(?:plan|roadmap|game\s?plan))\b",
    re.IGNORECASE,
)
_NO_EXECUTION = re.compile(
    r"\b(?:don'?t|do\s+not|without|no\s+need\s+to|not\s+yet)\s+(?:yet\s+)?(?:run|running|execut\w*|start\w*|implement\w*)\b",
    re.IGNORECASE,
)
#: The same instruction stated without the word "plan" ("outline the analysis but
#: do not run it"). `start` is left out here: "don't start with the null rows" is
#: about the data, not about running anything.
_NO_RUN = re.compile(
    r"\b(?:don'?t|do\s+not|without|no\s+need\s+to|not\s+yet)\s+(?:yet\s+)?(?:run|running|execut\w*|implement\w*)\b",
    re.IGNORECASE,
)
#: Asking how Wizard would go about something, which is a plan by another name.
_HOW_WOULD_YOU = re.compile(
    r"\b(?:outline|sketch|lay\s+out)\s+(?:the\s+|your\s+)?(?:analysis|approach|steps|strategy|method)\b"
    r"|\b(?:describe|explain|tell\s+me)\s+(?:what|how)\s+you(?:'d|\s+would|\s+will)\b"
    r"|\bwhat\s+would\s+you\s+do\b|\bhow\s+would\s+you\s+(?:approach|analy[sz]e|investigate|tackle)\b",
    re.IGNORECASE,
)

#: "what does X mean", "what do you mean": the verb, not the statistic.
_MEAN_VERB = re.compile(r"\b(?:does|do|did|would|will)\b[^?.!]*\bmeans?\b|\bmeans?\s+(?:what|by)\b", re.IGNORECASE)

#: Creating or changing a column is a transformation, whatever else the word
#: "column" says about structure.
_ALTERS_COLUMNS = re.compile(
    r"\b(?:creat\w*|add|insert\w*|deriv\w*|generat\w*|assign\w*|split\w*|remov\w*|delet\w*)\b"
    r"[^?.!]*\b(?:column|field|variable|feature)s?\b",
    re.IGNORECASE,
)

#: What people type first when they are asking for something rather than saying it.
_POLITE = frozenset(
    {
        "please",
        "pls",
        "kindly",
        "can",
        "could",
        "would",
        "will",
        "you",
        "i",
        "want",
        "need",
        "like",
        "to",
        "lets",
        "let",
        "us",
        "me",
        "just",
    }
)
_REQUEST_VERBS = frozenset(
    {
        "show",
        "list",
        "give",
        "get",
        "display",
        "print",
        "find",
        "tell",
        "pull",
        "fetch",
        "view",
        "see",
        "look",
        "check",
        "make",
        "build",
        "draw",
        "count",
        "calculate",
        "compute",
        "return",
        "provide",
    }
)

#: Asking about what Wizard did, said or is, rather than asking it to do something.
_ADDRESSES_YOU = re.compile(
    r"\b(?:why|how|what|which|when|who)\b[^?.!]*\b(?:you|your|yours|yourself)\b|\byou\s+(?:just|already)\b"
    r"|\b(?:explain|justify|clarify|elaborate\s+on|walk\s+me\s+through)\b[^?.!]*\b(?:that|this|it|previous|last|above|earlier)\b",
    re.IGNORECASE,
)


#: A whole message that only asks the running turn to stop. Consulted *only* while
#: a turn is running (see `is_interrupt_intent`), so it can never mistake data
#: for a command: with nothing running, "stop" is just a message like any other.
_INTERRUPT = re.compile(
    r"^\s*(?:please\s+)?(?:(?:stop|cancel|abort|halt|quit|end)(?:\s+(?:it|that|this|now|everything|please|"
    r"the\s+(?:analysis|run|task)|what\s+you(?:'re|\s+are)\s+doing))*|never\s?mind|forget\s+(?:it|that)|enough)"
    r"(?:\s+please)?\s*[.!]*\s*$",
    re.IGNORECASE,
)


def is_interrupt_intent(message: str) -> bool:
    """Whether a message sent *while a turn is running* asks it to stop.

    The transport asks this only when a turn is already in flight; anything else
    sent then is refused as busy. A message that merely contains the word stop
    ("stop losses by region") is not an interrupt: the whole message must be one.
    """
    return bool(_INTERRUPT.match(message or ""))


def _matches(token: str, stems: tuple[str, ...]) -> bool:
    singular = token[:-1] if token.endswith("s") and len(token) > 3 else token
    for stem in stems:
        if len(stem) >= 5:
            if token.startswith(stem):
                return True
        elif token == stem or singular == stem or (len(stem) == 4 and token.startswith(stem)):
            return True
    return False


def _named(message: str, name: str) -> bool:
    """Whether ``message`` names ``name`` on word boundaries (`id` is not in `provide`)."""
    cleaned = str(name).strip().lower()
    if not cleaned:
        return False
    # A plural is the same column ("list the regions" names `region`).
    return re.search(rf"(?<![a-z0-9_]){re.escape(cleaned)}(?:e?s)?(?![a-z0-9_])", message) is not None


def _operation_families(tokens: list[str]) -> frozenset[str]:
    found: set[str] = set()
    for index, token in enumerate(tokens):
        for family, stems in _OPERATIONS.items():
            if not _matches(token, stems):
                continue
            # "what does that mean" is a question about meaning, not the mean.
            if token in ("mean", "means") and index > 0 and tokens[index - 1] in _MEAN_VERB_PREV:
                continue
            found.add(family)
    return frozenset(found)


def extract_signals(message: str, context: TurnContext | None = None) -> Signals:
    """Reads evidence from one message. Pure; no model call."""
    ctx = context or TurnContext()
    text = (message or "").strip()
    lowered = text.lower()
    tokens = _TOKEN.findall(lowered)
    token_set = set(tokens)

    data_refs = sum(1 for name in (*ctx.columns, *ctx.table_names) if _named(lowered, name))
    # "what does churn mean?" asks for a meaning; the statistic is not in it.
    operation_tokens = [t for t in tokens if t not in ("mean", "means")] if _MEAN_VERB.search(lowered) else tokens
    operations = _operation_families(operation_tokens)
    if _ALTERS_COLUMNS.search(lowered):
        operations = operations | {"transform"}
    heavy = frozenset(t for t in tokens if _matches(t, _HEAVY))
    visual = any(_matches(t, _VISUAL) for t in tokens)
    structure = any(t in _STRUCTURE or (t.endswith("s") and t[:-1] in _STRUCTURE) for t in tokens) or bool(
        _TYPE_OF_COLUMNS.search(lowered)
    )
    deliverable = any(_matches(t, _DELIVERABLE) for t in tokens)
    open_analysis = any(_matches(t, _OPEN_ANALYSIS) for t in tokens)
    open_ended = open_analysis and data_refs == 0 and not operations and not (heavy - {"why"})
    is_question = text.endswith("?") or bool(
        tokens and tokens[0] in {"what", "which", "who", "when", "how", "why", "is", "are", "do", "does", "can"}
    )

    plan_requested = bool(
        _PLAN_ASK.search(lowered)
        or _HOW_WOULD_YOU.search(lowered)
        or _NO_RUN.search(lowered)
        or ("plan" in token_set and _NO_EXECUTION.search(lowered))
    )
    lead = [token for token in tokens if token not in _POLITE]
    # "which region is strongest?" picks one thing out of the data; "what about
    # churn?" and "what does churn mean?" do not say what is wanted.
    picks_from_data = bool(tokens and tokens[0] in {"which", "who", "whose", "when", "where", "how"} and is_question)
    imperative = bool(lead and lead[0] in _REQUEST_VERBS) or picks_from_data

    n_clauses = max(1, len([part for part in _CLAUSE_SPLIT.split(lowered) if part and part.strip()]))

    addresses_you = bool(_ADDRESSES_YOU.search(lowered))
    # A short question with nothing to compute is about what was just said.
    if (
        ctx.has_prior_turn
        and is_question
        and len(tokens) <= 4
        and data_refs == 0
        and not operations
        and not structure
        and not visual
    ):
        addresses_you = True

    confirms_plan = bool(
        ctx.pending_plan and tokens and len(tokens) <= 6 and token_set & _CONFIRM and data_refs == 0 and not heavy
    )

    return Signals(
        n_tokens=len(tokens),
        n_clauses=n_clauses,
        is_question=is_question,
        data_refs=data_refs,
        operations=operations,
        heavy=heavy,
        structure=structure,
        condition=bool(_CONDITION.search(lowered)),
        visual=visual,
        sequencing=sum(1 for t in tokens if t in _SEQUENCING),
        deliverable=deliverable,
        open_analysis=open_analysis,
        open_ended=open_ended,
        social=bool(token_set & _SOCIAL),
        addresses_you=addresses_you,
        plan_requested=plan_requested,
        confirms_plan=confirms_plan,
        imperative=imperative,
    )


def complexity_score(sig: Signals) -> float:
    """How much independent work the message asks for. Unitless; only thresholds matter.

    Counts *steps*, not words: two aggregations are one step, an aggregation
    plus a model plus a report are three.
    """
    score = 2.0 * min(len(sig.heavy), 3)
    score += 1.5 * max(0, sig.steps - min(len(sig.heavy), 3) - 1)
    if sig.open_analysis:
        score += 2.0
    if sig.open_ended:
        score += 1.5  # scope is unspecified, which is what a plan is for
    score += 1.0 * min(sig.sequencing, 2)
    score += 0.5 * max(0, sig.n_clauses - 1)
    if sig.n_tokens > 30:
        score += 1.0
    return score


_COMPLEX = 3.0
_MODERATE = 1.5


def _complexity(sig: Signals) -> Complexity:
    score = complexity_score(sig)
    if score >= _COMPLEX:
        return Complexity.COMPLEX
    if score >= _MODERATE:
        return Complexity.MODERATE
    return Complexity.SIMPLE if sig.task_evidence else Complexity.TRIVIAL


def route_turn(message: str, context: TurnContext | None = None, mode: str = "auto") -> Route:
    """Chooses the workflow for one message. Deterministic and free.

    ``mode`` is applied last, as policy (`apply_mode`); it cannot change what the
    message *is*.
    """
    ctx = context or TurnContext()
    return apply_mode(_route_intent(extract_signals(message, ctx), ctx), mode)


def _converse(sig: Signals, *, intent: Intent, decided: bool, why: str) -> Route:
    return Route(intent, Workflow.CONVERSE, Complexity.TRIVIAL, escalate=not decided, reasons=(why,))


def _route_intent(sig: Signals, ctx: TurnContext) -> Route:
    # A waiting plan and a message that confirms it. Nothing else can be meant.
    if sig.confirms_plan:
        return Route(
            Intent.EXECUTE_PLAN,
            Workflow.EXECUTE_PLAN,
            Complexity.MODERATE,
            reasons=("confirms the plan that is waiting",),
            source="approval",
        )

    # The user said what they want done. That is a constraint, not an inference.
    if sig.plan_requested:
        return _needs_dataset(
            Route(
                Intent.PLAN_REQUEST,
                Workflow.PLAN_ONLY,
                Complexity.COMPLEX,
                plan=PlanPolicy.ONLY,
                reasons=("the message asks for a plan, not for execution",),
            ),
            ctx,
        )

    # A question about Wizard itself or its last answer. Column names like `name`
    # or `type` must not make "what is your name" a computation.
    if sig.addresses_you and not sig.computes:
        return _converse(
            sig,
            intent=Intent.FOLLOW_UP if ctx.has_prior_turn else Intent.CONVERSATION,
            decided=False,
            why="asks about Wizard or its previous answer",
        )

    # No evidence of a task. Conversation; how sure we are decides the model.
    if not sig.task_evidence:
        decided = sig.social and sig.n_tokens <= 8
        return _converse(
            sig,
            intent=Intent.CONVERSATION,
            decided=decided,
            why=(
                "greeting, thanks or acknowledgement with nothing to compute"
                if decided
                else "no column, operation or question about the data"
            ),
        )

    # A social word with a stray task word and no question: the reply decides.
    if sig.social and not sig.is_question and sig.n_tokens <= 5 and sig.data_refs == 0 and not sig.structure:
        return _converse(
            sig,
            intent=Intent.CONVERSATION,
            decided=False,
            why="a social word with a stray task word; the reply decides",
        )

    # A column is named, and nothing is asked of it: "my department is sales",
    # "what about churn?". Running an analysis on that is the loud mistake; a
    # wrong light route costs one cheap reply that can hand the turn over.
    only_names_data = sig.data_refs and not (
        sig.operations or sig.heavy or sig.structure or sig.visual or sig.deliverable or sig.open_analysis
    )
    if only_names_data and not sig.imperative:
        return _converse(
            sig,
            intent=Intent.CONVERSATION,
            decided=False,
            why="names a column but asks for nothing specific; the reply decides",
        )

    complexity = _complexity(sig)

    # About the dataset's structure, with nothing to compute and no filter.
    if (
        sig.structure
        and not sig.condition
        and not sig.computes
        and not sig.heavy
        and not sig.visual
        and not sig.deliverable
        and not sig.open_analysis
    ):
        return _needs_dataset(
            Route(
                Intent.DATA_QUESTION,
                Workflow.INSPECT,
                Complexity.TRIVIAL,
                reasons=("asks about the dataset's structure",),
            ),
            ctx,
        )

    if complexity is Complexity.COMPLEX:
        multi = sig.n_clauses >= 3 or sig.steps >= 4 or (sig.sequencing >= 1 and sig.n_clauses >= 2)
        return _needs_dataset(
            Route(
                Intent.MULTI_STEP if multi else Intent.INVESTIGATION,
                Workflow.AGENTIC,
                Complexity.COMPLEX,
                plan=PlanPolicy.SELF,
                verify=True,
                deep=multi,
                reasons=("several dependent steps" if multi else "needs investigation, or the scope is open-ended",),
            ),
            ctx,
        )

    if complexity is Complexity.MODERATE:
        return _needs_dataset(
            Route(
                Intent.INVESTIGATION,
                Workflow.AGENTIC,
                Complexity.MODERATE,
                verify=True,
                reasons=("more than one step, but specific enough to start without a plan",),
            ),
            ctx,
        )

    if sig.visual and not sig.computes and not sig.heavy:
        return _needs_dataset(
            Route(Intent.VISUALIZATION, Workflow.DIRECT, Complexity.SIMPLE, reasons=("one chart",)), ctx
        )

    return _needs_dataset(
        Route(Intent.COMPUTATION, Workflow.DIRECT, Complexity.SIMPLE, reasons=("one figure or table from the data",)),
        ctx,
    )


def _needs_dataset(route: Route, ctx: TurnContext) -> Route:
    """A data workflow without data is a conversation about what is missing."""
    if ctx.has_dataset or route.workflow is Workflow.CONVERSE:
        return route
    return Route(
        route.intent,
        Workflow.CONVERSE,
        route.complexity,
        needs_data=True,
        reasons=(*route.reasons, "no dataset is loaded"),
        source=route.source,
    )


def _noted(route: Route, reason: str) -> tuple[str, ...]:
    """The route's reasons plus one more, once. Keeps `apply_mode` idempotent."""
    return route.reasons if reason in route.reasons else (*route.reasons, reason)


def apply_mode(route: Route, mode: str) -> Route:
    """Applies the user's chosen mode as policy over an already-decided intent.

    - Conversation and dataset inspection are never promoted: a mode is how hard
      to work on a task, and these are not tasks.
    - An explicit plan request is honoured in every mode: it is an instruction.
    - ``fast``: no planner, no verification, one iteration.
    - ``deep``: analytic work gets the full investigation and its deep budget.
    - ``planning``: analytic work is planned and shown to the user before it runs.
    """
    if route.workflow in (Workflow.CONVERSE, Workflow.INSPECT, Workflow.PLAN_ONLY, Workflow.EXECUTE_PLAN):
        return route

    if mode == "fast":
        if route.plan is PlanPolicy.NONE and not route.verify and not route.deep:
            return route
        return route.with_(
            plan=PlanPolicy.NONE,
            verify=False,
            deep=False,
            reasons=_noted(route, "fast mode: no planner, no verification"),
            source="mode",
        )

    if mode == "deep":
        plan = route.plan
        if plan is PlanPolicy.NONE and route.complexity is not Complexity.SIMPLE:
            plan = PlanPolicy.SELF
        return route.with_(
            workflow=Workflow.AGENTIC,
            plan=plan,
            verify=True,
            deep=True,
            reasons=_noted(route, "deep mode: full investigation"),
            source="mode",
        )

    if mode == "planning":
        return route.with_(
            workflow=Workflow.AGENTIC,
            plan=PlanPolicy.GATED,
            verify=route.verify,
            reasons=_noted(route, "plan mode: the plan is confirmed before anything runs"),
            source="mode",
        )

    return route


def escalate(route: Route, has_dataset: bool, mode: str = "auto") -> Route:
    """The conversational reply asked for the data. Re-route the same turn as analysis."""
    if not has_dataset:
        return route.with_(needs_data=True, escalate=False, reasons=(*route.reasons, "needs data, none is loaded"))
    fresh = Route(
        Intent.INVESTIGATION,
        Workflow.AGENTIC,
        Complexity.MODERATE,
        verify=True,
        reasons=("the conversational reply needed the data",),
        source="escalation",
    )
    return apply_mode(fresh, mode)


__all__ = [
    "ESCALATE_SENTINEL",
    "Complexity",
    "Intent",
    "PlanPolicy",
    "Route",
    "Signals",
    "TurnContext",
    "Workflow",
    "apply_mode",
    "complexity_score",
    "escalate",
    "extract_signals",
    "is_interrupt_intent",
    "route_turn",
]
