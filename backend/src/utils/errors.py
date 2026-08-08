"""Turns an internal exception into something safe to hand back to a client.

An exception's own text can carry file paths, hostnames, driver internals and
library versions -- useful in a server log, a map for an attacker probing a
deployment that is reachable beyond localhost. ``safe_error_message`` logs the
real detail server-side under a short correlation id and returns only that id
in ``dev``/``test`` mode. ``dev`` still shows the real text, because it is
what a developer running the stack locally needs to see.
"""

from __future__ import annotations

import uuid

from src.config import settings
from src.utils.logging import logger


def safe_error_message(exc: Exception, event: str, *, detail: str | None = None, **context) -> str:
    """Logs ``exc`` with a correlation id and returns the client-facing text.

    ``event`` names what was being attempted (e.g. "Chat run failed"), and
    ``context`` is forwarded to the logger as structured fields (session id,
    connection name, ...). Nothing in ``context`` reaches the client.

    ``detail`` overrides what is logged and returned in non-prod mode --
    ``str(exc)`` alone loses information for exceptions (like
    ``ConnectorError``) that carry their real message in a separate attribute.
    """
    text = str(exc) if detail is None else detail
    error_id = uuid.uuid4().hex[:8]
    logger.error(event, error_id=error_id, error=text, **context)
    if settings.ENV == "prod":
        return f"An internal error occurred. Error ID: {error_id}"
    return text


__all__ = ["safe_error_message"]
