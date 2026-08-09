"""On-demand re-export of a finished turn as a runnable script or notebook.

Distinct from the always-on `analysis.py` `orchestrator._write_script` drops
into the workspace on every turn: that file is overwritten by the next turn,
so it cannot answer "export the analysis from two questions ago." This route
rebuilds from what was persisted on the message itself (`chat_messages.meta`),
using `core.agent.export`'s builders -- the same ones the always-on artifact
uses -- so both stay identical in what they consider "what actually ran."

Scoped to the live session, like everything else in this API: a session's
datasets only exist in memory while the session is alive, and a connector
lookup by name only works with the current session's dataset provenance. A
reaped session 404s here exactly as `require_session` already does everywhere
else.
"""

from __future__ import annotations

import json
import zipfile
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from src.api.deps import get_session_for_link
from src.core.agent import export
from src.core.database import db_mgr
from src.core.session import Session


router = APIRouter(prefix="/api/export", tags=["export"])

MEDIA_TYPES = {
    "script": "text/x-python",
    "notebook": "application/x-ipynb+json",
}


@router.get("/{message_id}")
async def export_message(
    message_id: int,
    format: str = Query(default="script", pattern="^(script|notebook)$"),
    session: Session = Depends(get_session_for_link),
) -> Response:
    """Rebuilds one turn's analysis as a downloadable script, notebook, or zip.

    A zip is returned instead of the bare file whenever a file-based table
    needs to travel with it -- a connector-sourced table never does, since it
    is re-fetched by name instead.
    """
    message = db_mgr.get_chat_message(session.id, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="No such message in this session.")

    meta = message.get("meta") or {}
    steps = meta.get("steps") or []
    if not steps:
        raise HTTPException(
            status_code=404,
            detail="This answer has no recorded execution steps to export (it may have come from cache or answered without running code).",
        )

    instruction = str(meta.get("instruction") or "")
    bundle = export.bundle_files(session)

    if format == "notebook":
        content = export.build_notebook(instruction, steps, session, bundle=bool(bundle))
        if not content:
            raise HTTPException(status_code=404, detail="Nothing to export for this message.")
        payload = json.dumps(content, indent=1).encode("utf-8")
        filename = "analysis.ipynb"
    else:
        text = export.build_script(instruction, steps, session, bundle=bool(bundle))
        if not text:
            raise HTTPException(status_code=404, detail="Nothing to export for this message.")
        payload = text.encode("utf-8")
        filename = "analysis.py"

    if not bundle:
        return Response(
            content=payload,
            media_type=MEDIA_TYPES[format],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, payload)
        for path, data in bundle.items():
            archive.writestr(path, data)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="analysis-{message_id}.zip"'},
    )


__all__ = ["router"]
