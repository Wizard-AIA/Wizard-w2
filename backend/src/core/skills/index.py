"""What this machine installed, from where, pinned at which commit.

The spec asks for "a simple local index of installed skills + their source URL +
pinned commit + when last updated", and this is it: ``installed.json`` in the
platform config directory, beside ``credentials.json`` and ``connections.json``,
restricted through the same shared :func:`~src.utils.fileperms.restrict`.

Why a sidecar rather than extra frontmatter
-------------------------------------------
Writing ``source_url`` and ``pinned_sha`` into the fetched ``SKILL.md`` would be
self-describing and would survive being copied to another machine, which is
genuinely attractive. It is still wrong, for one reason that outweighs both:

    **a provenance claim written by the payload is not provenance.**

The file being described arrived from a stranger. Nothing stops it declaring
``pinned_sha: <any commit at all>``, and a UI reading that field would render an
unearned badge next to text it has no basis for trusting. So the origin of a
skill is recorded only in a file this machine wrote itself, and
:meth:`InstallIndex.overlay` is what puts it back onto the skill after a scan.

It also keeps the installed ``SKILL.md`` byte-identical to upstream, which is
what makes the update diff exact rather than approximately right.

The index is a *record*, never a source of truth about what exists. A skill
deleted with a text editor leaves an entry behind; the entry is joined to the
registry by name and simply matches nothing, which is why nothing here checks
the filesystem.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from src.utils.appdirs import config_dir
from src.utils.fileperms import restrict
from src.utils.logging import logger

from .source import InstallRecord


INSTALLED_FILENAME = "installed.json"


class InstallIndex:
    """Every skill this install pulled from somewhere else, read once and cached."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._cache: dict[str, InstallRecord] | None = None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        # Resolved per access, not at construction: this is a singleton and the
        # test suite pins the config directory after `src` is imported.
        return self._path or (config_dir() / "skills" / INSTALLED_FILENAME)

    # ------------------------------------------------------------------ #
    def _load(self) -> dict[str, InstallRecord]:
        if self._cache is not None:
            return self._cache
        with self._lock:
            if self._cache is None:
                self._cache = self._read()
            return self._cache

    def _read(self) -> dict[str, InstallRecord]:
        path = self.path
        try:
            if not path.exists():
                return {}
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A corrupt index means "nothing is known to have been installed",
            # never a backend that will not start. The same rule the credential
            # store and the connection store follow.
            logger.warning("Could not read the installed-skill index", path=str(path), error=str(exc))
            return {}

        rows = payload.get("installed") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return {}

        records: dict[str, InstallRecord] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                record = InstallRecord.from_dict(row)
            except (TypeError, ValueError) as exc:
                logger.warning("Skipped an unreadable install record", error=str(exc))
                continue
            records[record.name] = record
        return records

    def _write(self, records: dict[str, InstallRecord]) -> bool:
        path = self.path
        payload = {"installed": [record.to_dict() for record in records.values()]}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            restrict(path, "installed-skill index")
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            restrict(path, "installed-skill index")
        except OSError as exc:
            logger.error("Could not save the installed-skill index", path=str(path), error=str(exc))
            return False
        return True

    # ------------------------------------------------------------------ #
    def list(self) -> list[InstallRecord]:
        return sorted(self._load().values(), key=lambda record: record.name)

    def get(self, name: str) -> InstallRecord | None:
        return self._load().get((name or "").strip().lower())

    def record(self, entry: InstallRecord) -> bool:
        with self._lock:
            records = dict(self._read())
            previous = records.get(entry.name)
            if previous is not None:
                # An update keeps the original install time. When it was first
                # obtained and when it last changed are different questions, and
                # the UI shows both.
                entry.installed_at = previous.installed_at
            records[entry.name] = entry
            if not self._write(records):
                return False
            self._cache = records
        logger.info("Recorded an installed skill", skill=entry.name, sha=entry.short_sha)
        return True

    def forget(self, name: str) -> bool:
        key = (name or "").strip().lower()
        with self._lock:
            records = dict(self._read())
            if records.pop(key, None) is None:
                self._cache = records
                return False
            if not self._write(records):
                return False
            self._cache = records
        return True

    def overlay(self, skills: Sequence[Any]) -> None:
        """Stamps provenance onto skills that were installed from somewhere.

        Called by the registry after a scan. Mutates in place because a
        :class:`~src.core.skills.spec.Skill` is already the object every caller
        holds, and returning copies would mean the shadowed list and the resolved
        map disagreed about the same file.

        Only the user layer is stamped. Nothing installs into the built-in layer
        (it is the checkout) or the project layer (that is the repository being
        analysed, and its skills came with it), so a matching name in either is a
        different skill that happens to share a name — exactly the case the
        shadowing rules already handle.
        """
        records = self._load()
        if not records:
            return
        for skill in skills:
            if getattr(skill, "layer", None) is None or skill.layer.value != "user":
                continue
            record = records.get(skill.name)
            if record is None:
                continue
            for field_name, value in record.summary().items():
                setattr(skill, field_name, value)

    def reload(self) -> None:
        with self._lock:
            self._cache = None

    def clear(self) -> None:
        """Empties the index. For the test suite's teardown.

        The index persists in the config directory on purpose, which without this
        means it persists *between tests* too -- the same cross-test leak
        ``ConnectionStore.clear`` and ``clear_user_skills`` exist for.
        """
        with self._lock:
            self._write({})
            self._cache = {}


install_index = InstallIndex()


__all__ = ["INSTALLED_FILENAME", "InstallIndex", "install_index"]
