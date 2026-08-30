"""Separating a reasoning model's private thinking from what it actually said.

Reasoning models -- DeepSeek-R1 and its distills, QwQ, and increasingly everything
else -- wrap a chain of thought in ``<think>...</think>`` before answering. That
text is not the answer. It is frequently longer than the answer by an order of
magnitude, and on a small distill it is *far* longer: a 1.5B R1 will spend two
thousand tokens deciding whether to pick `code` or `answer`.

Why this module exists rather than a regex at one call site
-----------------------------------------------------------
The orchestrator knew about ``<thought>`` -- the tag its own planning prompt asks
for -- and about nothing else. With ``deepseek-r1:1.5b`` in the manager role that
meant the raw chain of thought was captured verbatim as ``state.plan``, and the
plan is embedded in *every* subsequent decision prompt and in the answer prompt.
So one unrecognised tag pair did not cost one bad plan; it prepended a thousand
tokens of thinking to five later prompts, and every one of those had to be
re-read by a CPU-bound model before it could emit a token. It also broke action
parsing, because the discarded reasoning says words like "answer" and "inspect"
while it deliberates.

Total by design
---------------
Nothing here raises. A block that never closes -- which is what a hard
``num_predict`` cap produces when a model thinks past its budget -- yields empty
visible text, and the caller treats that as "the model returned nothing usable"
rather than pasting half a monologue into the UI.
"""

from __future__ import annotations

import re


#: Tag names that delimit private reasoning. ``thought`` is included because it
#: is what this codebase's own planning prompt asks for, so one function handles
#: both the model's native habit and our instruction to it.
REASONING_TAGS: tuple[str, ...] = ("think", "thinking", "thought", "reasoning", "reflection")

_NAMES = "|".join(REASONING_TAGS)

#: A complete block. Non-greedy so consecutive blocks are matched individually.
BLOCK = re.compile(rf"<({_NAMES})\b[^>]*>(.*?)</\1\s*>", re.DOTALL | re.IGNORECASE)

#: Any opening tag. Also used to spot one with no matching close, which is what
#: a hard ``num_predict`` cap produces when a model thinks past its budget.
OPEN_TAG = re.compile(rf"<({_NAMES})\b[^>]*>", re.IGNORECASE)

#: A closing tag with no opening one. Several servers prefill the open tag into
#: the prompt rather than letting the model emit it, so the response begins in
#: the middle of a thought and the first thing we see is ``</think>``.
CLOSE_TAG = re.compile(rf"</({_NAMES})\s*>", re.IGNORECASE)

#: Longest possible tag, plus slack. Streaming holds back a partial ``<`` prefix
#: shorter than this rather than leaking ``<thin`` into the UI as content.
MAX_TAG_CHARS = len("</reflection>") + 4


def split_reasoning(text: str) -> tuple[str, str]:
    """Splits a model response into ``(reasoning, visible)``.

    ``visible`` is what the user asked for and what should be parsed. It is
    empty when the model spent its whole budget thinking, which is a real
    outcome worth reporting rather than papering over.
    """
    if not text:
        return "", ""

    # An orphaned close tag means the open tag was prefilled by the server, so
    # everything up to it is thinking. Handled first: rewriting it into a
    # complete block lets the ordinary path below deal with the rest.
    orphan = CLOSE_TAG.search(text)
    if orphan and not OPEN_TAG.search(text):
        return text[: orphan.start()].strip(), text[orphan.end() :].strip()

    thoughts: list[str] = []

    def capture(match: re.Match[str]) -> str:
        thoughts.append(match.group(2).strip())
        return ""

    visible = BLOCK.sub(capture, text)

    # Whatever follows an unclosed opening tag is thinking that ran out of room.
    dangling = OPEN_TAG.search(visible)
    if dangling:
        thoughts.append(visible[dangling.end() :].strip())
        visible = visible[: dangling.start()]

    return "\n\n".join(part for part in thoughts if part).strip(), visible.strip()


def strip_reasoning(text: str) -> str:
    """Just the visible part. The common case at call sites that only parse."""
    return split_reasoning(text)[1]


class ReasoningStream:
    """Classifies a token stream as reasoning or visible text *as it arrives*.

    Waiting for the whole response and splitting it afterwards would work, but it
    would also give up token streaming for every reasoning model -- the UI would
    sit empty for the length of a chain of thought and then print everything at
    once. Tracking the tag boundary incrementally lets the thinking panel fill
    live and hand over to the answer at the right moment.

    A partial tag is held back rather than emitted, so ``<thi`` never reaches the
    UI as content only to be retracted when the rest of the tag arrives.
    """

    __slots__ = ("_inside", "_pending", "_start_time", "_first_token_emitted", "_provider", "_model")

    def __init__(self, provider: str = "unknown", model: str = "unknown") -> None:
        self._pending = ""
        self._inside = False
        import time

        self._start_time = time.monotonic()
        self._first_token_emitted = False
        self._provider = provider
        self._model = model

    def feed(self, delta: str) -> list[tuple[bool, str]]:
        """Consumes a delta, returning ``(is_reasoning, text)`` chunks."""
        out = self._feed_inner(delta)
        if out and not self._first_token_emitted:
            import time

            from src.core.infra.metrics import metrics

            self._first_token_emitted = True
            metrics.record_ttft(self._provider, self._model, time.monotonic() - self._start_time)
        return out

    def _feed_inner(self, delta: str) -> list[tuple[bool, str]]:
        self._pending += delta
        out: list[tuple[bool, str]] = []

        while self._pending:
            if not self._inside:
                opening = OPEN_TAG.search(self._pending)
                if opening:
                    before = self._pending[: opening.start()]
                    if before:
                        out.append((False, before))
                    self._pending = self._pending[opening.end() :]
                    self._inside = True
                    continue
                # A close with no open: the server prefilled the opening tag into
                # the prompt, so the response began mid-thought.
                orphan = CLOSE_TAG.search(self._pending)
                if orphan:
                    head = self._pending[: orphan.start()]
                    if head:
                        out.append((True, head))
                    self._pending = self._pending[orphan.end() :]
                    continue
                self._drain(out)
                return out

            closing = CLOSE_TAG.search(self._pending)
            if closing:
                inner = self._pending[: closing.start()]
                if inner:
                    out.append((True, inner))
                self._pending = self._pending[closing.end() :]
                self._inside = False
                continue
            self._drain(out)
            return out

        return out

    def flush(self) -> list[tuple[bool, str]]:
        """Emits whatever was held back when the stream ended."""
        if not self._pending:
            return []
        remainder, self._pending = self._pending, ""
        out = [(self._inside, remainder)]
        if out and not self._first_token_emitted:
            import time

            from src.core.infra.metrics import metrics

            self._first_token_emitted = True
            metrics.record_ttft(self._provider, self._model, time.monotonic() - self._start_time)
        return out

    def _drain(self, out: list[tuple[bool, str]]) -> None:
        """Emits everything that cannot still turn out to be part of a tag.

        Only the trailing partial tag is held, not the whole buffer. Holding all
        of it would stall real text behind an unresolved ``<`` -- and stall it
        inconsistently, since a buffer that happened to be longer than a tag was
        released while a shorter one was not.
        """
        marker = self._pending.rfind("<")
        if marker == -1 or len(self._pending) - marker >= MAX_TAG_CHARS or ">" in self._pending[marker:]:
            # No candidate, long enough that a real tag would already have
            # matched, or a complete element that is not a reasoning tag.
            marker = len(self._pending)
        if marker:
            out.append((self._inside, self._pending[:marker]))
            self._pending = self._pending[marker:]


def looks_like_reasoning_model(model: str) -> bool:
    """Whether this model is expected to think out loud before answering.

    Used to size an output budget, not to change behaviour: a reasoning model
    needs headroom to finish a thought, and cutting it off mid-``<think>``
    produces nothing at all rather than a shorter answer.
    """
    lowered = (model or "").lower()
    return any(hint in lowered for hint in ("r1", "qwq", "-think", "reason", "o1-", "marco-o1"))


__all__ = [
    "BLOCK",
    "CLOSE_TAG",
    "MAX_TAG_CHARS",
    "OPEN_TAG",
    "REASONING_TAGS",
    "ReasoningStream",
    "looks_like_reasoning_model",
    "split_reasoning",
    "strip_reasoning",
]
