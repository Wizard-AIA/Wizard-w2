"""Cloud API keys, stored on this machine and nowhere else.

Nothing is hosted and nothing is synced, so a key can only live on the user's own
disk. It also cannot live only in ``backend/.env``, which is inside the checkout:
typing a key into a settings field has to persist without editing a source file.

This is not encryption at rest. The file is protected by the operating system's
access control and nothing else — the same guarantee ``~/.aws/credentials`` has.
Encrypting it would need a passphrase at every backend start, which breaks the
unattended start Milestone 8 is built around, or a key stored beside the
ciphertext, which protects nothing. The OS keychain is the stronger option and is
deliberately not taken: three platform backends plus a dependency, and Secret
Service is often absent on headless Linux, so a file fallback would be needed
anyway. Everything goes through ``credential_store``, so a keychain backend can be
added later without touching a caller.

Permissions are enforced on all three platforms rather than documented on two.
An unreadable store degrades to "no stored keys"; a question never fails because
a credentials file has odd permissions.

Lives under ``core/`` rather than ``core/llm/`` because ``settings`` reads it, and
because Milestone 4's connection strings belong in the same store.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from src.utils.appdirs import config_dir
from src.utils.fileperms import restrict
from src.utils.logging import logger


CREDENTIALS_FILENAME = "credentials.json"

#: Characters of a key shown back to the user. Enough to tell two keys apart.
HINT_CHARS = 4


def _restrict(path: Path) -> None:
    """Shared with ``connections.json``, which needs the identical treatment.

    See ``utils/fileperms.py`` for why the Windows half reads the SID from the
    process token rather than from ``%USERNAME%``.
    """
    restrict(path, "credentials file")


class CredentialStore:
    """Provider API keys on local disk, read once and cached in memory.

    Cached because ``available_providers()`` renders on every page load and asks
    whether each provider has a key.
    """

    def __init__(self, path: Path | None = None):
        self._path = path
        self._cache: dict[str, str] | None = None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        # Resolved per access: this is a singleton, and tests pin the config dir
        # after import.
        return self._path or (config_dir() / CREDENTIALS_FILENAME)

    # ------------------------------------------------------------------ #
    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        with self._lock:
            if self._cache is None:
                self._cache = self._read()
            return self._cache

    def _read(self) -> dict[str, str]:
        path = self.path
        try:
            if not path.exists():
                return {}
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A corrupt store means "no stored keys", never a failed request.
            logger.warning("Could not read stored credentials", path=str(path), error=str(exc))
            return {}
        keys = payload.get("api_keys") if isinstance(payload, dict) else None
        if not isinstance(keys, dict):
            return {}
        return {str(name): str(value) for name, value in keys.items() if isinstance(value, str) and value.strip()}

    def _write(self, keys: dict[str, str]) -> bool:
        path = self.path
        is_new = not path.exists()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Restricted before anything secret is in it, not just after.
            path.touch(exist_ok=True)
            _restrict(path)
            path.write_text(json.dumps({"api_keys": keys}, indent=2) + "\n", encoding="utf-8")
            _restrict(path)
        except OSError as exc:
            logger.error("Could not save credentials", path=str(path), error=str(exc))
            return False
        if is_new:
            # Stated once, at the moment it becomes true, rather than left for
            # the docstring above to say on the user's behalf: this store is
            # plaintext on disk, protected only by OS file permissions -- the
            # same guarantee `~/.aws/credentials` has, not encryption at rest.
            logger.warning(
                "Created a new credentials store. Keys are saved in plaintext, "
                "protected only by OS file permissions -- not encrypted at rest.",
                path=str(path),
            )
        return True

    # ------------------------------------------------------------------ #
    def get(self, provider: str) -> str:
        """The stored key for ``provider``, or ``""``."""
        return self._load().get((provider or "").strip().lower(), "")

    def has(self, provider: str) -> bool:
        return bool(self.get(provider))

    def hint(self, provider: str) -> str:
        """A masked form of the key. The only representation that leaves the process."""
        key = self.get(provider)
        if not key:
            return ""
        return f"…{key[-HINT_CHARS:] if len(key) > HINT_CHARS else key}"

    def set(self, provider: str, key: str) -> bool:
        name = (provider or "").strip().lower()
        cleaned = (key or "").strip()
        if not name or not cleaned:
            return False
        with self._lock:
            keys = dict(self._read())
            keys[name] = cleaned
            if not self._write(keys):
                return False
            self._cache = keys
        logger.info("Stored an API key", provider=name)
        return True

    def delete(self, provider: str) -> bool:
        name = (provider or "").strip().lower()
        with self._lock:
            keys = dict(self._read())
            if name not in keys:
                self._cache = keys
                return False
            keys.pop(name)
            if not self._write(keys):
                return False
            self._cache = keys
        logger.info("Removed a stored API key", provider=name)
        return True

    def providers_with_keys(self) -> list[str]:
        """Provider ids with a stored key -- and *only* provider ids.

        The keyspace is shared: Milestone 4's connections store their secrets
        here too, namespaced ``connection:<id>``. A provider id never contains a
        colon, so filtering on one keeps a saved database password from being
        reported as a configured model provider.
        """
        return sorted(name for name in self._load() if ":" not in name)

    def names(self, prefix: str) -> list[str]:
        """Every stored key under ``prefix``, for a namespaced caller."""
        return sorted(name for name in self._load() if name.startswith(prefix))

    def reload(self) -> None:
        """Drops the cache so the next read goes back to disk."""
        with self._lock:
            self._cache = None


credential_store = CredentialStore()


__all__ = ["CREDENTIALS_FILENAME", "CredentialStore", "credential_store"]
