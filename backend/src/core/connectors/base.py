"""The interface every connector implements.

A ``Protocol`` rather than a base class: a connector is defined by what it can
do, and nothing here has behaviour worth inheriting. It also means a test can
register an object that answers these four methods without importing a driver or
subclassing anything -- which is how most of this package is tested with no
database running.

**Every method here runs in the parent process.** Generated code never holds a
connector and never opens a socket; a connection is read here, materialised to
Feather by ``Session.add_dataset``, and the daemon loads it from the workspace.
That is what keeps Milestone 3's sandbox network seal intact -- see
``core/security/sandbox/`` -- and it is why ``fetch`` is safe to offer at all.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import pandas as pd

from .spec import ConnectionSchema, ConnectionSpec


#: What a `sample()` returns when the caller does not say. Small enough to be
#: cheap on a large table and large enough for `CatalogEngine.analyze` to detect
#: semantic types from, which needs a spread rather than a handful of rows.
DEFAULT_SAMPLE_ROWS = 1000


@runtime_checkable
class Connector(Protocol):
    """One live connection to one data source."""

    spec: ConnectionSpec

    def test(self) -> None:
        """Reaches the source and returns, or raises ``ConnectorError``.

        Separate from ``discover`` because "can I reach this at all" is the
        question the user asks while typing a host name, and on a large warehouse
        enumerating every table to answer it is both slow and beside the point.
        """

    def discover(self) -> ConnectionSchema:
        """Lists what can be read, without reading it."""

    def sample(self, target: str, limit: int = DEFAULT_SAMPLE_ROWS) -> pd.DataFrame:
        """Reads at most ``limit`` rows from one target.

        Bounded in the *engine* wherever the driver allows it. Fetching a table
        and slicing it in pandas gives the same answer having already paid for
        the whole table, which is the thing this signature exists to avoid.
        """

    def fetch(self, query: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        """Runs an engine-native query and returns the result.

        ``params`` carries bind values for engines that support them (SQL's
        ``:name`` placeholders). A caller with a value to vary the query by must
        bind it here, never format it into ``query`` -- that is the difference
        between a parameterized query and a SQL-injection vector.

        Called only from the parent process, and only for something the user
        asked for -- never handed to generated code. Defined now because a
        connector that cannot express "the rows matching this" forces the whole
        table through memory to answer a question about part of it.
        """

    def write(self, target: str, df: pd.DataFrame) -> None:
        """Writes a frame back to the source.

        Implementations must refuse when ``spec.read_only`` is set. That check is
        the connector's own, not only the caller's: write-back is the one
        operation whose blast radius is outside this machine, so it is guarded
        at every layer that could reach it.
        """

    def close(self) -> None:
        """Releases whatever the driver is holding. Must be safe to call twice."""


def refuse_write(spec: ConnectionSpec) -> None:
    """Raises unless ``spec`` has been explicitly opened for write-back.

    Shared so all three reference connectors refuse in the same words, and so a
    contributed connector gets the check by calling one function rather than by
    remembering the rule.
    """
    from .spec import ConnectorError

    if spec.read_only:
        raise ConnectorError(
            f"The connection '{spec.name}' is read-only.",
            detail="Enable write-back for this connection first. It is off until you turn it on, per connection.",
        )


__all__ = ["DEFAULT_SAMPLE_ROWS", "Connector", "refuse_write"]
