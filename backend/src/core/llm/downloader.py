"""Installing models from inside the app.

Wizard could already *list* what a provider had installed and let you pick one,
but getting a model in the first place meant leaving for a terminal or for the
LM Studio window. That is the one step in setup where a local-first tool sends
you somewhere else, and it is the step a first-time user hits first.

The two local providers disagree about how this is done, so this module holds
the disagreement in one place:

* **Ollama** has a real HTTP API. ``POST /api/pull`` streams NDJSON with byte
  counts, and ``DELETE /api/delete`` removes a model. Nothing else is needed.
* **LM Studio** does not. Its native ``/api/v0`` surface is read-only, so the
  only scriptable route is the ``lms`` CLI that ships with the app. We spawn it
  and read its output. It reports a percentage rather than byte counts, and it
  has no delete verb at all -- both of which are reported as limits rather than
  faked.
* **Gateways** host their models; there is nothing to download. Asking says so.

Downloads run on their own thread and are polled, not streamed. A model pull is
minutes long and survives page reloads, so a socket that has to stay open for
the duration would be the fragile choice; the frontend already polls
``/api/models``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal
from urllib.parse import urlsplit

from src.config import settings
from src.core.llm.registry import model_registry
from src.providers import LOCAL_PROVIDERS, label_for
from src.utils.hostinfo import host_info
from src.utils.logging import logger


DownloadStatus = Literal["queued", "downloading", "completed", "failed", "cancelled"]

#: How long a finished entry stays visible before it is swept. Long enough that
#: a poll on a slow connection still sees the transition to "completed", short
#: enough that the list does not become a history log.
COMPLETED_TTL_SECONDS = 120.0

#: One at a time, per provider. Two concurrent multi-gigabyte pulls on a laptop
#: contend for the same disk and the same uplink and both finish later than they
#: would have done in sequence.
MAX_CONCURRENT_PER_PROVIDER = 1

# A model name is interpolated into an argv for LM Studio, so it is validated
# rather than escaped. Requiring an alphanumeric first character is what stops a
# name like "--help" or "-o/etc/passwd" being read as an option; no separate
# flag-injection guard is needed because a leading dash cannot get through.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
# `lms get` also accepts a full Hugging Face URL, which is how anything outside
# LM Studio's own curated catalogue is installed. Restricted to that one host:
# this reaches the network on the user's behalf, so it may not be pointed
# anywhere the caller likes.
_HF_URL_RE = re.compile(r"^https://huggingface\.co/[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}/?$")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")


def is_valid_model_name(name: str) -> bool:
    """Whether ``name`` is safe to put in an argv and hand to a provider.

    URLs are checked *first and only* against the Hugging Face pattern. Testing
    the two alternatives the other way round looks equivalent and is not: `:`
    and `/` are both legal in a bare model name, so the general pattern happily
    matches ``https://evil.example.com/a/b`` and the host restriction below it
    never gets a say.
    """
    if "://" in name:
        return _is_valid_huggingface_url(name)
    return bool(_MODEL_NAME_RE.match(name))


def _is_valid_huggingface_url(url: str) -> bool:
    """Whether ``url`` is a plain, unadorned ``huggingface.co/<org>/<repo>`` link.

    The regex alone rejects the character classes that let a URL smuggle a
    pip/CLI-style option or a second scheme, but ``urlsplit`` is what actually
    proves the string parses to the host and scheme it appears to have --
    parsing and pattern-matching agreeing is a stronger guarantee than either
    one alone, and cheap enough that there is no reason to rely on just one.
    """
    if not _HF_URL_RE.match(url):
        return False
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.netloc.lower() == "huggingface.co"
        and not parts.query
        and not parts.fragment
        and "@" not in parts.netloc
    )


def lms_executable() -> str | None:
    """Path to the ``lms`` CLI, or None if this machine has no LM Studio.

    Checked on PATH first, then at the fixed location the LM Studio installer
    uses -- it does not add itself to PATH on Windows, so PATH alone would
    report "no LM Studio" on a machine that plainly has it.
    """
    found = shutil.which("lms")
    if found:
        return found
    candidate = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if sys.platform == "win32" else "lms")
    return str(candidate) if candidate.exists() else None


@dataclass
class DownloadState:
    """One in-flight or recently finished download."""

    provider: str
    model: str
    status: DownloadStatus = "queued"
    completed_bytes: int = 0
    total_bytes: int = 0
    #: Whatever the provider last said it was doing, verbatim.
    detail: str = ""
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    #: Set when the provider reports a percentage but no byte counts, which is
    #: all LM Studio gives us.
    percent_override: float | None = None

    @property
    def percent(self) -> float | None:
        if self.status == "completed":
            return 100.0
        if self.percent_override is not None:
            return round(min(100.0, max(0.0, self.percent_override)), 1)
        if self.total_bytes > 0:
            return round(min(100.0, self.completed_bytes * 100.0 / self.total_bytes), 1)
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    def finish(self, status: DownloadStatus, error: str | None = None) -> None:
        """Marks this download done. **Write order matters.**

        ``finished_at`` is set before ``status`` because the poller decides a
        download is over by reading ``status``, and the sweep treats a missing
        ``finished_at`` as zero -- i.e. finished long ago. Setting status first
        leaves a window in which a failed download is both terminal and
        instantly sweepable, so the row vanishes before the UI ever shows the
        error that caused it.
        """
        if error is not None:
            self.error = error
        self.finished_at = time.time()
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "completed_bytes": self.completed_bytes,
            "total_bytes": self.total_bytes,
            "percent": self.percent,
            "detail": self.detail,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class ProviderNotDownloadable(Exception):
    """Raised when a provider cannot install models, with the reason why."""


class ModelDownloader:
    """Starts, tracks and cancels model installs, per provider."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], DownloadState] = {}
        self._cancels: dict[tuple[str, str], threading.Event] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- #
    # What this machine can do
    # ---------------------------------------------------------------- #
    def capability(self, provider: str | None = None) -> dict[str, Any]:
        """Whether ``provider`` can install and remove models here, and why not."""
        name = settings.resolve_provider(provider)
        if name not in LOCAL_PROVIDERS:
            return {
                "provider": name,
                "can_download": False,
                "can_delete": False,
                "reason": f"{label_for(name)} hosts its own models; there is nothing to download.",
            }
        if name == "ollama":
            return {
                "provider": name,
                "can_download": True,
                "can_delete": True,
                "reason": "",
            }

        # LM Studio: needs its CLI, which is a local binary. Inside a container
        # the API process cannot see the host's LM Studio install even when the
        # *server* is reachable over the network, so say that rather than
        # letting the button fail.
        if lms_executable() is None:
            reason = (
                "The LM Studio CLI is not reachable from the server process, which is running in a container."
                if host_info().containerised
                else "LM Studio's CLI (`lms`) was not found. Install LM Studio, or run `lms bootstrap` once."
            )
            return {"provider": name, "can_download": False, "can_delete": False, "reason": reason}
        return {
            "provider": name,
            "can_download": True,
            # `lms` has no delete verb; models are removed from the LM Studio app.
            "can_delete": False,
            "reason": "",
        }

    # ---------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------- #
    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._sweep_locked()
            return [state.to_dict() for state in sorted(self._states.values(), key=lambda s: s.started_at)]

    def get(self, provider: str, model: str) -> DownloadState | None:
        with self._lock:
            return self._states.get((settings.resolve_provider(provider), model))

    def start(self, provider: str | None, model: str) -> DownloadState:
        name = settings.resolve_provider(provider)
        model = model.strip()
        if not is_valid_model_name(model):
            raise ValueError("That does not look like a model name.")

        capability = self.capability(name)
        if not capability["can_download"]:
            raise ProviderNotDownloadable(capability["reason"])

        key = (name, model)
        with self._lock:
            self._sweep_locked()
            existing = self._states.get(key)
            if existing is not None and not existing.is_terminal:
                return existing
            active = sum(1 for (p, _), s in self._states.items() if p == name and not s.is_terminal)
            if active >= MAX_CONCURRENT_PER_PROVIDER:
                raise ProviderNotDownloadable(
                    f"A {name} download is already running. Downloads are run one at a time so they do not "
                    "compete for the same disk."
                )
            state = DownloadState(provider=name, model=model)
            cancel = threading.Event()
            self._states[key] = state
            self._cancels[key] = cancel

        worker = self._pull_ollama if name == "ollama" else self._pull_lmstudio
        thread = threading.Thread(
            target=self._run,
            args=(worker, state, cancel),
            name=f"model-pull-{name}",
            daemon=True,
        )
        thread.start()
        return state

    def cancel(self, provider: str | None, model: str) -> bool:
        key = (settings.resolve_provider(provider), model)
        with self._lock:
            cancel = self._cancels.get(key)
            state = self._states.get(key)
        if cancel is None or state is None or state.is_terminal:
            return False
        cancel.set()
        return True

    def remove(self, provider: str | None, model: str) -> None:
        """Deletes an installed model. Ollama only -- see ``capability``."""
        name = settings.resolve_provider(provider)
        if not is_valid_model_name(model):
            raise ValueError("That does not look like a model name.")
        capability = self.capability(name)
        if not capability["can_delete"]:
            raise ProviderNotDownloadable(
                capability["reason"] or "LM Studio models are removed from the LM Studio app, not from here."
            )

        import httpx

        root = settings.provider_root_url(name).rstrip("/")
        response = httpx.request(
            "DELETE",
            f"{root}/api/delete",
            json={"model": model},
            timeout=30.0,
        )
        if response.status_code == 404:
            raise ValueError(f"{model} is not installed.")
        response.raise_for_status()
        model_registry.invalidate(name)

    # ---------------------------------------------------------------- #
    def _run(
        self,
        worker: Callable[[DownloadState, threading.Event], None],
        state: DownloadState,
        cancel: threading.Event,
    ) -> None:
        state.status = "downloading"
        error: str | None = None
        try:
            worker(state, cancel)
        except Exception as exc:  # noqa: BLE001 - a failed pull must not kill the thread silently
            error = str(exc)[:400] or exc.__class__.__name__
            logger.warning("Model download failed", provider=state.provider, model=state.model, error=str(exc))

        # Only this method decides the terminal state, so there is one place
        # where `finished_at` and `status` are written in the right order.
        if error is not None:
            state.finish("failed", error)
        elif cancel.is_set():
            state.detail = "Cancelled"
            state.finish("cancelled")
        else:
            state.detail = "Installed"
            # Invalidated before the status flips, so a poll that sees
            # "completed" and immediately re-lists models gets the new one
            # rather than a cached list without it.
            model_registry.invalidate(state.provider)
            state.finish("completed")

    def _sweep_locked(self) -> None:
        cutoff = time.time() - COMPLETED_TTL_SECONDS
        for key, state in list(self._states.items()):
            if state.is_terminal and (state.finished_at or 0) < cutoff:
                self._states.pop(key, None)
                self._cancels.pop(key, None)

    # ---------------------------------------------------------------- #
    # Ollama: a real streaming API
    # ---------------------------------------------------------------- #
    def _pull_ollama(self, state: DownloadState, cancel: threading.Event) -> None:
        import httpx

        root = settings.provider_root_url(state.provider).rstrip("/")
        # No read timeout: Ollama goes quiet between layers while it verifies a
        # digest, and a pull that is working is not a pull that has hung.
        timeout = httpx.Timeout(connect=15.0, read=None, write=30.0, pool=None)
        with (
            httpx.Client(timeout=timeout) as client,
            client.stream(
                "POST",
                f"{root}/api/pull",
                json={"model": state.model, "stream": True},
            ) as response,
        ):
            if response.status_code >= 400:
                response.read()
                raise RuntimeError(_ollama_error(response.text, state.model))

            for line in response.iter_lines():
                if cancel.is_set():
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Ollama reports an error in-band, with HTTP 200 already sent.
                if error := payload.get("error"):
                    raise RuntimeError(str(error))

                status = str(payload.get("status") or "")
                if status:
                    state.detail = status
                total = payload.get("total")
                completed = payload.get("completed")
                if isinstance(total, int) and total > 0:
                    state.total_bytes = total
                if isinstance(completed, int) and completed >= 0:
                    state.completed_bytes = completed
                if status == "success":
                    return

        # The stream closed without Ollama ever saying "success". A truncated
        # pull leaves a partial blob that is not a usable model, so this is a
        # failure -- reporting it as done would put a broken name in the picker.
        if not cancel.is_set():
            raise RuntimeError("The connection to Ollama closed before the download finished.")

    # ---------------------------------------------------------------- #
    # LM Studio: its CLI, because there is no download API
    # ---------------------------------------------------------------- #
    def _pull_lmstudio(self, state: DownloadState, cancel: threading.Event) -> None:
        executable = lms_executable()
        if executable is None:
            raise RuntimeError("LM Studio's CLI is no longer reachable.")

        # `--yes` takes the variant LM Studio would have preselected for this
        # hardware. Choosing a quantization for the user from the server would
        # mean guessing at their GPU; LM Studio already knows.
        argv = [executable, "get", state.model, "--yes"]
        state.detail = "Resolving"
        process = subprocess.Popen(  # noqa: S603 - fixed executable, name validated against _MODEL_NAME_RE
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=os.getcwd(),
            text=True,
            errors="replace",
            bufsize=1,
        )

        tail: list[str] = []
        try:
            assert process.stdout is not None
            # The CLI redraws one progress line with carriage returns rather
            # than emitting whole lines, so this reads by character and splits
            # on either terminator. Iterating the file object would block until
            # a newline that never comes.
            for chunk in _iter_progress_lines(process.stdout):
                if cancel.is_set():
                    process.terminate()
                    return
                text = _ANSI_RE.sub("", chunk).strip()
                if not text:
                    continue
                tail.append(text)
                del tail[:-12]
                if match := _PERCENT_RE.search(text):
                    state.percent_override = float(match.group(1))
                    state.detail = "Downloading"
                else:
                    state.detail = text[:120]
        finally:
            if process.stdout:
                process.stdout.close()

        code = process.wait()
        if code != 0 and not cancel.is_set():
            raise RuntimeError(_lms_error(tail))


def _iter_progress_lines(stream: IO[str]) -> Iterator[str]:
    """Yields on ``\\n`` or ``\\r``, so a redrawn progress line is still seen."""
    buffer: list[str] = []
    while True:
        char = stream.read(1)
        if not char:
            break
        if char in ("\n", "\r"):
            if buffer:
                yield "".join(buffer)
                buffer.clear()
            continue
        buffer.append(char)
    if buffer:
        yield "".join(buffer)


def _ollama_error(body: str, model: str) -> str:
    """Turns Ollama's HTTP error body into something a user can act on."""
    try:
        message = str(json.loads(body).get("error") or body)
    except (json.JSONDecodeError, AttributeError):
        message = body
    if "file does not exist" in message or "not found" in message.lower():
        return f"Ollama has no model called '{model}'. Check the name at ollama.com/library."
    return message[:300] or f"Ollama refused to pull '{model}'."


def _lms_error(tail: list[str]) -> str:
    """The most informative line the CLI printed before it gave up."""
    for line in reversed(tail):
        if line.lower().startswith("error"):
            return line[:300]
    return (tail[-1][:300] if tail else "") or "LM Studio could not download that model."


model_downloader = ModelDownloader()
