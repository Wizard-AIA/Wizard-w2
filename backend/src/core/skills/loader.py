"""Reading one skill off disk, and refusing the ones that are not skills.

Two layouts are accepted, because both exist in the wild:

* a **directory** containing ``SKILL.md`` -- the convention, and the shape
  Milestone 6 fetches from a repository;
* a bare ``<name>.md`` -- what somebody writing their first skill actually does.

Both resolve to the same :class:`~src.core.skills.spec.Skill`, so nothing
downstream knows which was on disk.

The executable-payload boundary
-------------------------------
**A skill directory containing a script is refused, with the reason stated.**
Not ignored -- refused, so the user finds out at load rather than discovering
later that half their skill never ran. This is the Milestone 6 trust boundary
asserted in code today: a skill is instruction text the manager reads, and the
only way anything derived from it executes is by the worker writing code that
then passes ``CodeGuard.scan`` and runs in the sandbox, exactly as it would for a
question typed by hand. Deciding this now means the registry format, the review
UI and the pull flow never have to grow a second, weaker path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.embeddings import embedding_service
from src.core.ingest.documents import chunk_text
from src.utils.logging import logger

from .frontmatter import parse
from .spec import SKILL_FILENAME, InvalidSkill, Skill, SkillChunk, SkillLayer, is_valid_skill_name


#: Suffixes that make a skill directory a code bundle rather than instructions.
#: Checked by extension rather than by content: this runs on every scan, and the
#: question is what the *author* intended to ship, which the extension states.
EXECUTABLE_SUFFIXES = frozenset(
    {".py", ".pyw", ".pyc", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".exe", ".dll", ".so", ".dylib"}
)

#: Extensions a skill file may have when it is not in a directory.
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})


def _read_text(path: Path) -> str:
    """Decodes a skill file, tolerating whatever encoding it arrived in.

    Same tolerance ``documents._read_text`` has, and for the same reason: a skill
    written in Notepad on Windows is cp1252 often enough that failing the load
    over a smart quote would be absurd.
    """
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def offending_names(names: list[str]) -> list[str]:
    """Which of these filenames make a skill a code bundle rather than instructions.

    The rule itself, separated from where the names came from. Milestone 6 applies
    it to a **directory listing fetched from GitHub, before any content is
    downloaded** -- so the same function decides for a skill on disk and for one
    still on a stranger's server. Two implementations of a security boundary is
    two chances for them to stop agreeing.
    """
    return [name for name in names if Path(name).suffix.lower() in EXECUTABLE_SUFFIXES]


def executable_payload(directory: Path) -> list[str]:
    """Names any script shipped alongside a skill's instructions.

    Returns filenames rather than a bool so the refusal can say *which* file is
    the problem -- "contains executable files" sends someone hunting through a
    directory they did not write.
    """
    try:
        entries = [str(entry.relative_to(directory)) for entry in sorted(directory.rglob("*")) if entry.is_file()]
    except OSError as exc:
        raise InvalidSkill(f"Could not read the skill directory: {exc}") from exc
    return offending_names(entries)


def skill_paths(root: Path) -> list[Path]:
    """Every candidate skill under one layer root, without parsing any of them.

    A directory takes priority over a same-named markdown file, so a skill being
    promoted from one form to the other cannot briefly appear twice.
    """
    if not root.is_dir():
        return []

    resolved = root.resolve()
    if resolved.parent == resolved:
        # `SKILLS_BUILTIN_DIR`/`SKILLS_PROJECT_DIR` are deploy-time overrides,
        # not request input, but a misconfigured value of `/` or `C:\` here
        # would make every markdown file and every `SKILL.md`-bearing
        # directory on the machine load as a skill -- and a skill's body is
        # injected straight into the planning prompt. A path that resolves to
        # a filesystem root is never a legitimate skills directory.
        logger.warning("Refusing to index a skills directory that resolves to a filesystem root", root=str(resolved))
        return []

    paths: list[Path] = []
    seen: set[str] = set()
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        logger.warning("Could not list a skills directory", root=str(root), error=str(exc))
        return []

    for entry in entries:
        if entry.is_dir() and (entry / SKILL_FILENAME).is_file():
            paths.append(entry)
            seen.add(entry.name.lower())
    for entry in entries:
        if entry.is_file() and entry.suffix.lower() in MARKDOWN_SUFFIXES and entry.stem.lower() not in seen:
            paths.append(entry)
    return paths


def load_skill(path: Path, layer: SkillLayer, *, embed: bool = True) -> Skill:
    """Parses, validates, chunks and embeds one skill.

    ``embed=False`` skips the encoder, for callers that only need the metadata --
    the list view renders on every page load and has no use for vectors.
    """
    if path.is_dir():
        source = path / SKILL_FILENAME
        if not source.is_file():
            raise InvalidSkill(f"'{path.name}' has no {SKILL_FILENAME}.")
        payload = executable_payload(path)
        if payload:
            raise InvalidSkill(
                f"'{path.name}' ships executable files ({', '.join(payload[:5])}), which a skill may not do. "
                "A skill is instruction text; code it suggests is written and sandboxed like any other."
            )
        default_name = path.name
    elif path.is_file():
        source = path
        default_name = path.stem
    else:
        raise InvalidSkill(f"'{path}' does not exist.")

    data, body = parse(_read_text(source))

    name = str(data.get("name") or default_name).strip().lower()
    if not is_valid_skill_name(name):
        raise InvalidSkill(
            f"'{name or path.name}' is not a usable skill name. Use lowercase letters, digits, '-', '_' or '.'."
        )

    description = str(data.get("description") or "").strip()
    if not description:
        raise InvalidSkill(f"Skill '{name}' has no `description` in its frontmatter, so nothing can retrieve it.")
    if not body.strip():
        raise InvalidSkill(f"Skill '{name}' has frontmatter but no instructions under it.")

    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]

    # `source_url` and `pinned_sha` are deliberately *not* read from the
    # frontmatter, though they are fields on `Skill`. Milestone 6 fetches this
    # file from a stranger's repository, and a provenance claim written by the
    # payload is not provenance: a hostile SKILL.md could assert any commit it
    # liked and the UI would render an unearned badge beside it. They are stamped
    # on afterwards by `install_index.overlay`, from a file this machine wrote.
    skill = Skill(
        name=name,
        description=description,
        body=body.strip(),
        layer=layer,
        path=str(source),
        tags=[str(tag).strip() for tag in tags if str(tag).strip()],
        version=str(data.get("version") or "").strip(),
    )

    if embed:
        _attach_chunks(skill)
    return skill


def _attach_chunks(skill: Skill) -> None:
    """Chunks the body and embeds each passage.

    The description is prepended to the first chunk: a skill is most often
    matched by *what it is for*, and that sentence lives in the frontmatter
    rather than the body, so without this the one line written to be searchable
    is the one line not searched.
    """
    passages = chunk_text(f"{skill.description}\n\n{skill.body}")
    for index, text in enumerate(passages):
        embedding = None
        try:
            embedding = embedding_service.encode(text)
        except Exception as exc:  # pragma: no cover - the encoder degrades on its own
            logger.debug("Skill chunk embedding failed; lexical fallback will be used", error=str(exc))
        skill.chunks.append(SkillChunk(skill=skill.name, index=index, text=text, embedding=embedding))


def render_skill_file(name: str, description: str, body: str, extra: dict[str, Any] | None = None) -> str:
    """The on-disk text for a skill being written by promotion or by the editor."""
    from .frontmatter import render

    header: dict[str, Any] = {"name": name, "description": description}
    header.update(extra or {})
    return render(header, body)


__all__ = [
    "EXECUTABLE_SUFFIXES",
    "MARKDOWN_SUFFIXES",
    "executable_payload",
    "load_skill",
    "offending_names",
    "render_skill_file",
    "skill_paths",
]
