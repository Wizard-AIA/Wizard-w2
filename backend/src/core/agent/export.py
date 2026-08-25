"""Turning a turn's real executed steps into a runnable script or notebook.

Shared by the always-on per-turn artifact (`orchestrator._write_script`) and
the on-demand export route (`routes/export.py`), so both agree on what "what
actually ran" means -- steps pulled straight from the investigation, never
reconstructed from the model's description of what it did. That is the same
grounding rule `check_grounding` applies to an answer, applied here to code.

Two loader shapes, not one, because the two callers ship to different places:
the always-on artifact stays inside the session workspace, next to the
per-table Feather files `Session._materialize` already wrote there, so it
reads those in place. The on-demand export leaves the workspace -- it is
downloaded and may be opened on a machine with no Wizard session at all -- so
it ships its own CSV copies (`bundle_files`) and reads those instead.

A connector-sourced table is never embedded either way. It is looked up by
name at run time through `ConnectionStore.by_name`, which exists for exactly
this: the script names the connection, never a credential.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.config import settings
from src.core.security.code_guard import CodeGuard
from src.core.session import Session
from src.core.analysis.runs import DatasetManifestEntry


HEADER_IMPORTS = [
    "import matplotlib.pyplot as plt",
    "import numpy as np",
    "import pandas as pd",
    "import seaborn as sns",
]

CONNECTOR_IMPORTS = [
    "from src.core.connectors.registry import build as build_connector",
    "from src.core.connectors.store import connection_store",
]


def dataset_loader_lines(
    session: Session,
    *,
    file_template: str,
    reader: str,
    manifest: Sequence[DatasetManifestEntry] | None = None,
    active_table_key: str = "",
) -> tuple[list[str], bool]:
    """Lines that rebuild ``tables`` (and ``df``) the way generated code expects.

    ``file_template`` takes one ``{key}`` placeholder -- e.g. ``"tables/{key}.feather"``
    for the in-workspace artifact, ``"data/{key}.csv"`` for a downloaded bundle.
    ``reader`` is the matching pandas call, e.g. ``"pd.read_feather"``.

    Returns the lines plus whether a connector-sourced table was involved, so
    the caller only adds the connector imports when something needs them.
    """
    lines: list[str] = ["tables = {}"]
    needs_connector = False
    entries = list(manifest) if manifest is not None else [
        DatasetManifestEntry(
            name=handle.name,
            table_key=handle.table_key,
            content_hash=handle.content_hash,
            rows=len(handle.df),
            columns=tuple(map(str, handle.df.columns)),
            origin=handle.origin,
            target=str(handle.profile.get("target", "")),
        )
        for handle in session.datasets.values()
    ]
    for entry in entries:
        key = entry.table_key
        if entry.origin:
            needs_connector = True
            target = entry.target or key
            lines.append(f"# '{entry.name}' is read from the connection '{entry.origin}', looked up by name")
            lines.append("# at run time -- this script never carries a credential.")
            lines.append(f"_spec = connection_store.by_name({entry.origin!r})")
            lines.append("if _spec is None:")
            lines.append(f'    raise RuntimeError("No saved connection named {entry.origin!r} on this machine.")')
            lines.append("_conn = build_connector(_spec, connection_store.secret_for(_spec))")
            lines.append("try:")
            lines.append(f"    tables[{key!r}] = _conn.sample({target!r}, limit={settings.CONNECTOR_MAX_ROWS})")
            lines.append("finally:")
            lines.append("    _conn.close()")
        else:
            path = file_template.format(key=key)
            lines.append(f'tables[{key!r}] = {reader}("{path}")')
        lines.append("")

    active_key = active_table_key
    if not active_key:
        active = session.active_handle
        active_key = active.table_key if active is not None else (entries[0].table_key if entries else "")
    if active_key:
        lines.append(f"df = tables[{active_key!r}]")
    return lines, needs_connector


def _guard_warning(code: str) -> list[str]:
    """Flags a step whose code would not pass the guard outside its session.

    `CodeExecutor.execute` already guarded this code once, when it actually ran
    -- but that scan widened the allowed roots with the session's own
    `extra_roots` (`workspace_write` grants), which do not travel with an
    exported file. Re-scanning without them surfaces the same warning a human
    running the export standalone would want, rather than presenting
    already-executed code as unconditionally safe to re-run anywhere.
    """
    verdict = CodeGuard.scan(code)
    if verdict.ok:
        return []
    return [
        f"# WARNING: this step did not pass the execution guard outside its original session ({verdict.reason}).",
        "# Review before running.",
    ]


def build_script(
    instruction: str,
    steps: list[dict[str, str]],
    session: Session,
    *,
    bundle: bool = False,
    manifest: Sequence[DatasetManifestEntry] | None = None,
    active_table_key: str = "",
) -> str:
    """Assembles the runnable ``.py`` from the steps that actually executed.

    ``bundle=True`` targets a downloaded zip (CSV copies ship alongside);
    ``bundle=False`` targets the in-workspace artifact (reads the Feather
    files already sitting next to it).
    """
    if not steps:
        return ""

    loader_lines, needs_connector = dataset_loader_lines(
        session,
        file_template="data/{key}.csv" if bundle else "tables/{key}.feather",
        reader="pd.read_csv" if bundle else "pd.read_feather",
        manifest=manifest,
        active_table_key=active_table_key,
    )

    header = [
        '"""Analysis generated by Wizard.',
        "",
        f"Question: {instruction}",
        "",
        (
            "Run this from inside the extracted bundle -- the data/ folder ships alongside it."
            if bundle
            else "Run this from inside the session workspace -- tables/ already holds this analysis's data."
        ),
        '"""',
        "",
        *HEADER_IMPORTS,
        *(CONNECTOR_IMPORTS if needs_connector else []),
        "",
        *loader_lines,
        "",
    ]

    body: list[str] = []
    for index, step in enumerate(steps, start=1):
        body.append(f"# --- Step {index}: {step.get('goal') or 'analysis'} " + "-" * 20)
        body.extend(_guard_warning(step["code"]))
        body.append(step["code"])
        body.append("")

    return "\n".join(header + body)


def build_notebook(
    instruction: str,
    steps: list[dict[str, str]],
    session: Session,
    *,
    bundle: bool = False,
    manifest: Sequence[DatasetManifestEntry] | None = None,
    active_table_key: str = "",
) -> dict[str, Any]:
    """The same content as `build_script`, split into nbformat-4 cells.

    Built by hand rather than through the `nbformat` package -- this is a
    plain JSON shape, not worth a new dependency for.
    """
    if not steps:
        return {}

    loader_lines, needs_connector = dataset_loader_lines(
        session,
        file_template="data/{key}.csv" if bundle else "tables/{key}.feather",
        reader="pd.read_csv" if bundle else "pd.read_feather",
        manifest=manifest,
        active_table_key=active_table_key,
    )

    intro = [
        "# Analysis generated by Wizard",
        "",
        f"**Question:** {instruction}",
        "",
        (
            "Run this from inside the extracted bundle -- the `data/` folder ships alongside it."
            if bundle
            else "Run this from inside the session workspace -- `tables/` already holds this analysis's data."
        ),
    ]
    import_lines = [*HEADER_IMPORTS, *(CONNECTOR_IMPORTS if needs_connector else []), "", *loader_lines]

    cells = [_markdown_cell(intro), _code_cell(import_lines)]
    for index, step in enumerate(steps, start=1):
        cells.append(_markdown_cell([f"### Step {index}: {step.get('goal') or 'analysis'}"]))
        cells.append(_code_cell([*_guard_warning(step["code"]), *step["code"].splitlines()]))

    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "pygments_lexer": "ipython3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def bundle_files(session: Session) -> dict[str, bytes]:
    """CSV bytes for every file-based table, keyed by the path the loader reads.

    Empty when every table in the session came from a connection -- nothing to
    bundle, since a connector-sourced table is never embedded.
    """
    files: dict[str, bytes] = {}
    for handle in session.datasets.values():
        if handle.origin:
            continue
        files[f"data/{handle.table_key}.csv"] = handle.df.to_csv(index=False).encode("utf-8")
    return files


def _code_cell(lines: list[str]) -> dict[str, Any]:
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": _source(lines)}


def _markdown_cell(lines: list[str]) -> dict[str, Any]:
    return {"cell_type": "markdown", "metadata": {}, "source": _source(lines)}


def _source(lines: list[str]) -> list[str]:
    """nbformat's line-list convention: every line but the last keeps its newline."""
    if not lines:
        return []
    return [f"{line}\n" for line in lines[:-1]] + [lines[-1]]


__all__ = ["build_notebook", "build_script", "bundle_files", "dataset_loader_lines"]
