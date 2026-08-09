"""Reading, writing, installing and promoting skills.

Reading and writing a local skill is **not** gated by the permission profile, and
deliberately so. Every layer is a local file the user put there, and promotion is
a REST action the user initiated -- the same reasoning that leaves *saving* a
connection ungated while *opening* one is not.

**Installing from GitHub is gated, under ``network``.** That is the point where
something arrives from outside this machine, and it is the only route here that
reaches anything. It is gated rather than refused under ``local-only``: no
session data, schema or rows leave -- this is a download of instruction text, the
same shape as ``POST /api/models/download``, which the mode does not block either.
``OUTBOUND_TOOLS`` is scoped to tools the *agent* invokes mid-analysis, where the
query itself is derived from the user's data.

**Install is reachable from here and from the CLI, never as an agent action.** A
fetched skill is untrusted text that goes into the manager's prompt; if the
manager could also install skills, a fetched skill could instruct the agent to
fetch more, and a consent prompt does not close that -- the prompt's wording
would be written by the thing under review.

A write aimed at a built-in skill returns **409 with the reason** rather than
succeeding into a file the next ``git pull`` discards.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_session, require_api_key
from src.api.schemas import (
    GitHubTokenRequest,
    SkillCandidateListResponse,
    SkillDetail,
    SkillDraftRequest,
    SkillInstallPreviewResponse,
    SkillInstallRequest,
    SkillListResponse,
    SkillPendingListResponse,
    SkillRoot,
    SkillSummary,
    SkillUpdateRequest,
    SkillWriteRequest,
)
from src.config import settings
from src.core.database import db_mgr
from src.core.permissions import authorize
from src.core.session import Session
from src.core.skills import install, promotion
from src.core.skills.fetch import FetchError, save_token, token_saved
from src.core.skills.index import install_index
from src.core.skills.registry import skill_registry
from src.core.skills.spec import SkillError, SkillLayer, SkillNotWritable
from src.utils.logging import logger


router = APIRouter(prefix="/api/skills", tags=["skills"])


def _permit_network(session: Session, subject: str) -> None:
    """Applies the permission profile to a user-initiated fetch, or 403s.

    The same REST rule Milestone 4 established: an authenticated request from the
    user answers an ``ask``, and ``deny`` stays terminal. The subject is the
    canonical source URL, so approving one repository is not approving every
    repository.
    """
    ruling = authorize(session.permissions, "network", subject)
    if not ruling.allowed:
        raise HTTPException(status_code=403, detail=ruling.reason)


def _registry_status() -> dict:
    return {
        "api_root": settings.SKILLS_REGISTRY_API,
        "token_saved": token_saved(),
        "pending_root": str(install.pending_root()),
    }


def _roots() -> list[SkillRoot]:
    return [
        SkillRoot(layer=layer.value, label=layer.label, path=str(path), writable=layer.writable)
        for layer, path in skill_registry.roots().items()
    ]


@router.get("", response_model=SkillListResponse)
async def list_skills(session: Session = Depends(get_session)) -> SkillListResponse:
    """Every installed skill, the roots they came from, and any pending offer.

    Shadowed skills are included: a built-in overridden by a user copy still
    exists, and hiding it is what makes "I edited it and nothing changed"
    unanswerable.

    Requires a session, like the rest of the app's discovery routes: an id
    that doesn't resolve is a 404 here rather than a silent fresh session, so
    this isn't reachable with a forged or expired one.
    """
    skills = await asyncio.to_thread(skill_registry.list, include_shadowed=True)
    usage = await asyncio.to_thread(db_mgr.skill_usage_summary)
    return SkillListResponse(
        skills=[
            SkillSummary(
                **skill.summary(),
                uses=usage.get(skill.name, {}).get("uses", 0),
                last_used=usage.get(skill.name, {}).get("last_used"),
            )
            for skill in skills
        ],
        roots=_roots(),
        candidates=[candidate.to_dict() for candidate in promotion.pending()],
        enabled=settings.SKILLS_ENABLED,
        pending=[item.to_dict() for item in await asyncio.to_thread(install.pending)],
        registry=_registry_status(),
    )


# ------------------------------------------------------------------ #
# Installing from GitHub
# ------------------------------------------------------------------ #
@router.post("/install/preview", response_model=SkillInstallPreviewResponse, dependencies=[Depends(require_api_key)])
async def preview_install(
    request: SkillInstallRequest, session: Session = Depends(get_session)
) -> SkillInstallPreviewResponse:
    """Fetches a repository or gist, pins it to a commit, and stages it for review.

    Installs nothing. The staged copy sits in a directory the registry does not
    scan, so it is not retrievable by the agent and cannot inform a plan until
    somebody has read it and approved it — which is what the milestone means by
    "never silent-install-and-run".
    """
    _permit_network(session, request.url.strip())
    try:
        staged = await asyncio.to_thread(install.preview, request.url)
    except FetchError as exc:
        # 502: the request was fine and the upstream host is what failed. A 400
        # would send the user back to re-read a URL that is not the problem.
        raise HTTPException(status_code=502, detail=str(exc))
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sha = staged[0].sha if staged else ""
    return SkillInstallPreviewResponse(
        pending=[item.to_dict() for item in staged],
        sha=sha,
        short_sha=sha[:7],
        source=staged[0].source.to_dict() if staged else {},
        message=(f"Fetched {len(staged)} skill(s) at commit {sha[:7]}. Nothing is installed until you approve it."),
    )


@router.get("/pending", response_model=SkillPendingListResponse)
async def list_pending() -> SkillPendingListResponse:
    """Fetched skills waiting to be read.

    Served as its own route as well as on the list response, because a review
    interrupted by a closed tab has to be findable again — the fetch is pinned
    and on disk, and losing track of it would mean fetching it a second time.
    """
    staged = await asyncio.to_thread(install.pending)
    return SkillPendingListResponse(
        pending=[item.to_dict() for item in staged],
        root=str(install.pending_root()),
    )


@router.post("/pending/{pending_id}/approve", response_model=SkillDetail, dependencies=[Depends(require_api_key)])
async def approve_pending(pending_id: str) -> SkillDetail:
    """Installs a reviewed skill into the user-global layer."""
    try:
        skill = await asyncio.to_thread(install.approve, pending_id)
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SkillDetail(**skill.to_dict())


@router.delete("/pending/{pending_id}", dependencies=[Depends(require_api_key)])
async def discard_pending(pending_id: str) -> dict:
    if not await asyncio.to_thread(install.discard, pending_id):
        raise HTTPException(status_code=404, detail=f"Nothing staged under id {pending_id!r}.")
    return {"message": "Discarded. Nothing was installed."}


@router.post("/token", dependencies=[Depends(require_api_key)])
async def set_github_token(request: GitHubTokenRequest) -> dict:
    """Saves or clears the GitHub token used for installs.

    Stored through ``credential_store`` under ``registry:github`` — namespaced
    with a colon on purpose, because ``providers_with_keys()`` filters on one and
    a bare ``github`` key would be reported on the models page as a configured
    model provider. Never returned by any route; only whether one exists.
    """
    saved = await asyncio.to_thread(save_token, request.token)
    if request.token.strip():
        if not saved:
            raise HTTPException(status_code=500, detail="Could not save the token.")
        return {"message": "Saved. It raises the rate limit and reaches private repositories.", "token_saved": True}
    return {"message": "Removed the stored GitHub token.", "token_saved": False}


@router.post("/reload", dependencies=[Depends(require_api_key)])
async def reload_skills() -> dict:
    """Re-scans the roots after an edit made outside the app.

    The point of skills being plain files is that a text editor is a valid way to
    change one, and the registry caches. Without this the answer to "I edited the
    file" would be "restart the backend".
    """
    await asyncio.to_thread(skill_registry.reload)
    count = await asyncio.to_thread(len, skill_registry)
    return {"message": f"Reloaded {count} skill(s).", "count": count}


@router.get("/candidates", response_model=SkillCandidateListResponse)
async def list_candidates(session: Session = Depends(get_session)) -> SkillCandidateListResponse:
    """Analyses that have recurred enough to be worth naming.

    Also served here, not only as a live frame, so an offer missed in the chat is
    still findable rather than being a one-shot card.

    Requires a session for the same reason as `list_skills`.
    """
    candidates = await asyncio.to_thread(promotion.pending)
    return SkillCandidateListResponse(
        candidates=[candidate.to_dict() for candidate in candidates],
        threshold=settings.SKILL_PROMOTION_THRESHOLD,
    )


@router.get("/candidates/{candidate_id}/draft", dependencies=[Depends(require_api_key), Depends(get_session)])
async def draft_candidate(candidate_id: int) -> dict:
    """A first draft of the skill this candidate would become.

    Built from the plan and code that actually ran, not asked of a model: the
    grounding layer's rule is that what is reported comes from what happened, and
    a model asked to summarise its own past work would describe an analysis it is
    not reading.
    """
    candidate = await asyncio.to_thread(promotion.get, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"No skill candidate with id {candidate_id}.")
    return {
        "name": candidate.suggested_name(),
        "description": f"How to answer questions like: {candidate.instruction}"[:200],
        "body": promotion.draft_body(candidate),
        "candidate": candidate.to_dict(),
    }


@router.post("/draft", dependencies=[Depends(require_api_key)])
async def draft_from_analysis(request: SkillDraftRequest) -> dict:
    """A draft for an analysis the user picked, rather than one that recurred.

    The milestone asks for both routes into promotion: an offer the agent makes
    when something has recurred, and an explicit "save this one" about an answer
    already on screen. This is the second, and it needs no threshold — every
    successful turn already records a candidate, so the plan and code that ran
    are there to draft from.

    A question with no recorded candidate still gets a draft, from the question
    itself. Refusing would mean the button works or not depending on bookkeeping
    the user cannot see.
    """
    candidate = await asyncio.to_thread(promotion.find, request.instruction)
    if candidate is None:
        candidate = promotion.Candidate(
            id=0, kind=promotion.KIND_RECURRING, instruction=request.instruction.strip(), occurrences=1
        )

    return {
        "name": candidate.suggested_name(),
        "description": f"How to answer questions like: {candidate.instruction}"[:200],
        "body": promotion.draft_body(candidate),
        # Null when nothing was recorded, which the client passes straight back:
        # `POST /api/skills` only settles a candidate when it is given one.
        "candidate_id": candidate.id or None,
        "candidate": candidate.to_dict() if candidate.id else None,
    }


@router.post("/candidates/{candidate_id}/dismiss", dependencies=[Depends(require_api_key)])
async def dismiss_candidate(candidate_id: int) -> dict:
    """Stops offering this analysis for promotion.

    Persisted rather than held for the session: declining once must not mean
    being asked again on the next turn, which is how a useful prompt becomes one
    people learn to click away.
    """
    if not await asyncio.to_thread(promotion.dismiss, candidate_id):
        raise HTTPException(status_code=404, detail=f"No skill candidate with id {candidate_id}.")
    return {"message": "Dismissed. This analysis will not be offered again."}


@router.get("/{name}", response_model=SkillDetail)
async def get_skill(name: str, session: Session = Depends(get_session)) -> SkillDetail:
    """One skill's full text, and the analyses it has informed.

    The usage list is the browser half of "see which analyses used which skill".
    The live ``skill`` frame answers it during a turn; by the time this page is
    open that frame is gone, so it is read back from what was recorded.

    Requires a session for the same reason as `list_skills`.
    """
    skill = await asyncio.to_thread(skill_registry.get, name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"No skill called '{name}'.")
    recent = await asyncio.to_thread(db_mgr.get_skill_usage, skill.name)
    usage = await asyncio.to_thread(db_mgr.skill_usage_summary)
    return SkillDetail(
        **skill.to_dict(),
        recent_uses=recent,
        uses=usage.get(skill.name, {}).get("uses", 0),
        last_used=usage.get(skill.name, {}).get("last_used"),
    )


@router.post("", response_model=SkillDetail, dependencies=[Depends(require_api_key)])
async def create_skill(request: SkillWriteRequest) -> SkillDetail:
    """Writes a new skill into the user-global layer.

    Also the promotion endpoint: passing ``candidate_id`` marks that recurring
    analysis as promoted, which is what stops it being offered again. The
    candidate is settled only *after* the file is written, so a failed write
    leaves the offer standing rather than silently consuming it.
    """
    try:
        skill = await asyncio.to_thread(
            skill_registry.write,
            request.name,
            request.description,
            request.body,
            layer=SkillLayer.USER,
            tags=request.tags,
        )
    except SkillNotWritable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if request.candidate_id is not None:
        await asyncio.to_thread(promotion.mark_promoted, request.candidate_id, skill.name)
        logger.info("Promoted an analysis to a skill", skill=skill.name, candidate=request.candidate_id)

    return SkillDetail(**skill.to_dict())


@router.put("/{name}", response_model=SkillDetail, dependencies=[Depends(require_api_key)])
async def update_skill(name: str, request: SkillWriteRequest) -> SkillDetail:
    """Rewrites a skill in place, in whichever writable layer defines it."""
    existing = await asyncio.to_thread(skill_registry.get, name)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"No skill called '{name}'.")
    if not existing.layer.writable:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{existing.name}' ships with Wizard and is replaced on update. "
                "Save a copy under the same name to override it — the user layer takes precedence."
            ),
        )

    try:
        skill = await asyncio.to_thread(
            skill_registry.write,
            existing.name,
            request.description,
            request.body,
            layer=existing.layer,
            tags=request.tags,
        )
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SkillDetail(**skill.to_dict())


@router.post("/{name}/update", dependencies=[Depends(require_api_key)])
async def update_installed_skill(
    name: str, request: SkillUpdateRequest, session: Session = Depends(get_session)
) -> dict:
    """Re-resolves the stored ref and reports — or applies — what changed.

    **Pin, don't track.** The ref re-resolved is the one recorded at install, never
    a branch chosen now, and with ``apply`` false (the default) nothing is written:
    the response carries a unified diff against the file currently on disk. So a
    skill cannot change under someone between two questions, and the diff is a
    step rather than a courtesy.
    """
    record = await asyncio.to_thread(install_index.get, name)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"'{name}' was not installed from a repository, so there is nothing to update it from.",
        )
    _permit_network(session, record.source.url)

    try:
        action = install.apply_update if request.apply else install.check_update
        result = await asyncio.to_thread(action, name)
    except FetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result.to_dict()


@router.delete("/{name}", dependencies=[Depends(require_api_key)])
async def delete_skill(name: str) -> dict:
    try:
        # Goes through `install.uninstall` so the index entry goes with the file.
        # A record left behind would offer an update for a skill that is not there.
        removed = await asyncio.to_thread(install.uninstall, name)
    except SkillNotWritable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not removed:
        raise HTTPException(status_code=404, detail=f"No skill called '{name}'.")
    return {"message": f"Removed '{name}'."}
