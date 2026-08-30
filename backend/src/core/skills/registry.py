"""Every skill this install can reach, across three layers.

Layers, in ascending precedence:

===========  =======================================  ===========================
layer        where                                    who owns it
===========  =======================================  ===========================
``builtin``  ``backend/skills/`` in the checkout      Wizard, replaced on update
``user``     ``config_dir()/skills``                  the person, across projects
``project``  ``./.wizard/skills``                     the checkout being analysed
===========  =======================================  ===========================

**On the user-global path.** The spec writes ``~/.wizard/skills/``. Milestone 1
established :func:`~src.utils.appdirs.config_dir` as the single answer for
user-level state -- ``%APPDATA%\\Wizard``, ``~/Library/Application Support/Wizard``,
``$XDG_CONFIG_HOME/wizard`` -- and Milestone 8's CLI manages that same directory.
Hardcoding a Linux-shaped dotfile path on Windows is exactly what guiding
principle 5 forbids, so skills live beside ``credentials.json`` and
``connections.json`` instead.

A name defined in two layers resolves to the more specific one, and the shadowed
copy is still listed with ``shadowed_by`` set -- otherwise editing the built-in
copy appears to do nothing and there is no way to find out why.

**A malformed skill is logged and skipped, never fatal.** The same rule
``ConnectionStore._read`` follows: one bad file on disk must not stop the app
answering questions.
"""

from __future__ import annotations

import builtins
import threading
from collections.abc import Sequence
from pathlib import Path

from src.config import settings
from src.core.embeddings import embedding_service
from src.utils.appdirs import config_dir
from src.utils.logging import logger

from .loader import load_skill, render_skill_file, skill_paths
from .spec import (
    SKILL_FILENAME,
    InvalidSkill,
    Skill,
    SkillError,
    SkillLayer,
    SkillMatch,
    SkillNotWritable,
    is_valid_skill_name,
)


def builtin_root() -> Path:
    """Where the shipped skills live.

    Derived from this file's location rather than the working directory: the
    backend is started from ``backend/`` by uvicorn, from the repo root by the
    test suite, and from ``/app`` in the image.
    """
    override = (settings.SKILLS_BUILTIN_DIR or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / "skills"


def user_root() -> Path:
    return config_dir() / "skills"


def project_root() -> Path:
    override = (settings.SKILLS_PROJECT_DIR or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.cwd() / ".wizard" / "skills"


#: Resolved per access rather than at import, because the test suite pins the
#: config directory and the settings fields after `src` is imported.
ROOTS = {
    SkillLayer.BUILTIN: builtin_root,
    SkillLayer.USER: user_root,
    SkillLayer.PROJECT: project_root,
}


#: Told to the model rather than cutting silently: a skill that stops mid-sentence
#: reads as a skill that has nothing more to say.
TRUNCATION_MARKER = "\n… [truncated]"


def _rank_by_coverage(query: str, passages: list[str]) -> list[tuple[float, int]]:
    """Scores passages by how much of the question they contain, best first.

    Normalised by the *query's* token count rather than the union, unlike
    :func:`~src.core.rag.retriever.lexical_overlap`. That function is right where
    it is used -- comparing a query against a column name, where both sides are
    short -- but dividing by the union punishes a passage for being long, and a
    skill body is two orders of magnitude longer than a question. The union form
    scored a perfect topical match at 0.04, which no floor can tell from noise.
    """
    from src.core.rag.retriever import tokenize

    wanted = tokenize(query)
    if not wanted:
        return []

    scored = [(len(wanted & tokenize(text)) / len(wanted), index) for index, text in enumerate(passages)]
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


class SkillRegistry:
    """The layered skill store, scanned once and cached in memory."""

    def __init__(self) -> None:
        self._cache: dict[str, Skill] | None = None
        self._shadowed: list[Skill] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    def roots(self) -> dict[SkillLayer, Path]:
        return {layer: resolve() for layer, resolve in ROOTS.items()}

    def _load(self) -> dict[str, Skill]:
        if self._cache is not None:
            return self._cache
        with self._lock:
            if self._cache is None:
                self._cache, self._shadowed = self._scan()
            return self._cache

    def _scan(self) -> tuple[dict[str, Skill], list[Skill]]:
        resolved: dict[str, Skill] = {}
        shadowed: list[Skill] = []

        # Ascending precedence, so a later layer overwrites an earlier one and
        # the displaced skill is recorded rather than dropped.
        for layer in sorted(ROOTS, key=lambda item: item.precedence):
            root = ROOTS[layer]()
            for path in skill_paths(root):
                try:
                    skill = load_skill(path, layer)
                except InvalidSkill as exc:
                    logger.warning("Skipped an unreadable skill", path=str(path), layer=layer.value, error=str(exc))
                    continue
                except OSError as exc:
                    logger.warning("Could not read a skill", path=str(path), error=str(exc))
                    continue

                previous = resolved.get(skill.name)
                if previous is not None:
                    previous.shadowed_by = layer.value
                    shadowed.append(previous)
                resolved[skill.name] = skill

        # Provenance is stamped on here rather than read out of each file's own
        # frontmatter, because Milestone 6 fetches those files from strangers and
        # a claim written by the payload is not a record. The index is a file this
        # machine wrote. See `skills/index.py`.
        from .index import install_index

        install_index.overlay([*resolved.values(), *shadowed])

        if resolved:
            logger.info("Skills loaded", count=len(resolved), shadowed=len(shadowed))
        return resolved, shadowed

    def reload(self) -> None:
        """Drops the cache so the next read goes back to disk."""
        with self._lock:
            self._cache = None
            self._shadowed = []

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def list(self, *, include_shadowed: bool = False) -> list[Skill]:
        skills = list(self._load().values())
        if include_shadowed:
            skills = skills + self._shadowed
        return sorted(skills, key=lambda skill: (skill.layer.precedence, skill.name))

    def get(self, name: str) -> Skill | None:
        return self._load().get((name or "").strip().lower())

    def __len__(self) -> int:
        return len(self._load())

    @property
    def any_installed(self) -> bool:
        """Whether consulting skills could return anything at all.

        Read by the orchestrator to decide whether to offer ``consult``: an
        action that cannot succeed should not be on the menu.
        """
        return bool(self._load())

    def search(self, query: str, limit: int | None = None) -> builtins.list[SkillMatch]:
        """The passages most relevant to ``query``, best first.

        With an encoder loaded this is :meth:`embedding_service.rank`, the same
        path reference documents use.

        **Without one it is question-coverage, not the hashing encoder.** That
        substitution is deliberate and was made after measuring: the fallback
        encoder is a signed bag-of-words sketch, and cosine between a six-word
        question and a 1,200-character passage is dominated by sketch collisions.
        Measured against the two shipped skills, "what is the capital of France"
        scored **0.368** and "which cohorts are driving churn" scored **0.172** --
        it ranked the irrelevant query higher, so no floor could separate them.
        Coverage -- what fraction of the question's content words the passage
        actually contains -- gives 0.0 and 0.667 for the same pair, discriminates
        correctly, and lands on the same scale as a transformer's cosine, which is
        why one ``SKILLS_MIN_SIMILARITY`` serves both.

        At most one passage per skill: two chunks of the same skill are two views
        of one piece of know-how, and spending the whole budget on them crowds out
        the second skill that might have been the relevant one.
        """
        if not settings.SKILLS_ENABLED:
            return []

        top_k = limit if limit is not None else settings.SKILLS_TOP_K
        if top_k <= 0 or not (query or "").strip():
            return []

        skills = self._load()
        chunks = [(chunk, skill) for skill in skills.values() for chunk in skill.chunks]
        if not chunks:
            return []

        if embedding_service.is_semantic:
            ranked = embedding_service.rank(query, [(chunk.text, chunk.embedding) for chunk, _ in chunks])
        else:
            ranked = _rank_by_coverage(query, [chunk.text for chunk, _ in chunks])

        matches: builtins.list[SkillMatch] = []
        claimed: set[str] = set()
        for score, index in ranked:
            if len(matches) >= top_k:
                break
            if score < settings.SKILLS_MIN_SIMILARITY:
                break
            chunk, skill = chunks[index]
            if skill.name in claimed:
                continue
            claimed.add(skill.name)
            matches.append(SkillMatch(skill=skill, text=chunk.text, score=float(score)))
        return matches

    def render_block(self, matches: Sequence[SkillMatch], limit: int | None = None) -> str:
        """The ``<skills>`` block injected into the planning prompt.

        Hard-capped: this is the one place a skill's text costs prompt budget, and
        an unbounded block would let a long skill push the schema and the question
        out of a small model's context. The cap is applied across the whole block
        rather than per skill, so two matches share one budget.
        """
        if not matches:
            return ""

        budget = limit if limit is not None else settings.SKILLS_MAX_CHARS
        preamble = (
            "Known-good practice for this kind of question. Follow it where it applies; "
            "it is guidance, not a substitute for looking at the data."
        )
        headings = [f"### {match.skill.heading()}" for match in matches]

        # The cap covers the whole block, so the fixed parts are subtracted
        # before the bodies are budgeted. Charging only the bodies is how a
        # "1,800 character" block was measured at 2,200.
        overhead = len("\n<skills>\n\n\n</skills>\n") + len(preamble) + sum(len(h) + 3 for h in headings)
        per_skill = max(200, (budget - overhead) // len(matches))

        sections: list[str] = []
        for heading, match in zip(headings, matches, strict=True):
            body = match.skill.body.strip()
            if len(body) > per_skill:
                # The marker is inside the allowance, not added to it -- adding
                # it is how the block came out 14 characters over its own cap.
                body = body[: per_skill - len(TRUNCATION_MARKER)].rstrip() + TRUNCATION_MARKER
            sections.append(f"{heading}\n{body}")

        joined = "\n\n".join(sections)
        return f"\n<skills>\n{preamble}\n\n{joined}\n</skills>\n"

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    def path_for(self, name: str, layer: SkillLayer) -> Path:
        return ROOTS[layer]() / name / SKILL_FILENAME

    def write(
        self,
        name: str,
        description: str,
        body: str,
        *,
        layer: SkillLayer = SkillLayer.USER,
        tags: Sequence[str] | None = None,
        overwrite: bool = True,
    ) -> Skill:
        """Creates or replaces a skill in a writable layer.

        Always the directory form, even for a one-file skill: that is what
        Milestone 6 installs into and what its update flow diffs, and having
        promotion produce the other shape would mean two code paths for the same
        object a milestone later.
        """
        if not layer.writable:
            raise SkillNotWritable(
                f"{layer.label} skills ship with Wizard and are replaced on update. "
                "Copy it to your user skills first, then edit the copy."
            )

        cleaned = (name or "").strip().lower()
        if not is_valid_skill_name(cleaned):
            raise SkillError(
                f"'{name}' is not a usable skill name. Use lowercase letters, digits, '-', '_' or '.', up to 64 characters."
            )
        if not (description or "").strip():
            raise SkillError("A skill needs a one-line description; it is what retrieval matches against.")
        if not (body or "").strip():
            raise SkillError("A skill needs instructions.")

        existing = self.get(cleaned)
        if existing is not None and existing.layer is layer and not overwrite:
            raise SkillError(f"A skill called '{cleaned}' already exists.")

        target = self.path_for(cleaned, layer)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                render_skill_file(cleaned, description.strip(), body, {"tags": tags or []}),
                encoding="utf-8",
            )
        except OSError as exc:
            raise SkillError(f"Could not write the skill: {exc}") from exc

        self.reload()
        written = self.get(cleaned)
        if written is None:  # pragma: no cover - only reachable if the write raced a delete
            raise SkillError("The skill was written but could not be read back.")
        logger.info("Skill written", skill=cleaned, layer=layer.value)
        return written

    def delete(self, name: str) -> bool:
        """Removes a skill from whichever writable layer defines it."""
        skill = self.get((name or "").strip().lower())
        if skill is None:
            return False
        if not skill.layer.writable:
            raise SkillNotWritable(
                f"'{skill.name}' ships with Wizard and cannot be removed. Shadow it with a skill of the same name instead."
            )

        source = Path(skill.path)
        try:
            source.unlink(missing_ok=True)
            # The directory form leaves an empty folder behind; anything else in
            # it was put there by the user, so it is only removed when bare.
            parent = source.parent
            if source.name == SKILL_FILENAME and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError as exc:
            raise SkillError(f"Could not remove the skill: {exc}") from exc

        self.reload()
        logger.info("Skill removed", skill=skill.name, layer=skill.layer.value)
        return True

    def clear_user_skills(self) -> None:
        """Removes every user-layer skill. For the test suite's teardown.

        Skills persist on purpose, which without this means they persist *between
        tests* too -- the same cross-test leak ``ConnectionStore.clear`` exists
        for, surfacing as a name conflict in whichever test happens to run second.

        Shadowed skills are included, and removal goes by **path rather than
        name**: a user skill hidden behind a project one of the same name is not
        in the resolved map at all, and ``delete(name)`` would resolve that name
        to the project copy -- missing the file it was asked to remove and taking
        a different one instead.
        """
        for skill in self.list(include_shadowed=True):
            if skill.layer is not SkillLayer.USER:
                continue
            source = Path(skill.path)
            try:
                source.unlink(missing_ok=True)
                parent = source.parent
                if source.name == SKILL_FILENAME and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:  # pragma: no cover - teardown must not raise
                pass
        self.reload()


skill_registry = SkillRegistry()


__all__ = ["ROOTS", "SkillRegistry", "builtin_root", "project_root", "skill_registry", "user_root"]
