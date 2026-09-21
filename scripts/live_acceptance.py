"""Live acceptance run: the v1.0.14 routing flow against a real cloud model.

Drives the real app (`src.api.api:app`) over its real `/ws/chat` socket with a
real provider, in `cloud-only` data mode, on one synthetic dataset. One socket,
one session, in the order that used to fail: an analysis, then `hi`.

The key is read from **stdin** and never from argv, a file or a flag, so it is
not in the process list, shell history or the repo:

    pbpaste | .venv/bin/python scripts/live_acceptance.py --model gemini-2.5-flash

Each turn is checked against what the router should have chosen and against what
ran (frame types), never against the wording of a model's answer. Exit status is
0 only if every turn met its expectation.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

#: Frames that only an analysis produces. A schema question is answered from the frame itself,
#: so it may open an iteration but must not plan, write code, run anything or verify.
ANALYSIS_FRAMES = {"plan_delta", "step_start", "code", "stdout", "verification"}
#: A greeting does not even open an iteration.
CONVERSATION_FRAMES = ANALYSIS_FRAMES | {"iteration_start"}

#: (message, expected workflow, frames that must appear, frames that must not appear)
TURNS: list[tuple[str, str, set[str], set[str]]] = [
    ("what columns are in this dataset?", "inspect", set(), ANALYSIS_FRAMES),
    ("what is the average salary?", "direct", {"code"}, set()),
    (
        "Why do employees churn? Compare departments, tenure and satisfaction, "
        "and tell me which factors matter most, with evidence.",
        "agentic",
        {"code"},
        set(),
    ),
    ("hi", "converse", set(), CONVERSATION_FRAMES),
    ("thanks, that helps", "converse", set(), CONVERSATION_FRAMES),
    ("how many rows are there?", "inspect", set(), ANALYSIS_FRAMES),
    (
        "Make a plan for analysing churn by department. Do not run anything yet.",
        "plan_only",
        set(),
        {"code", "stdout"},
    ),
]


def dataset_csv() -> bytes:
    """A small employee table with a known average salary, so the direct answer can be checked."""
    import io

    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(14)
    rows = 400
    tenure = rng.uniform(0, 15, rows).round(1)
    satisfaction = rng.uniform(1, 5, rows).round(1)
    department = rng.choice(["Sales", "Engineering", "Support", "Finance"], rows)
    salary = (45_000 + tenure * 1_800 + rng.normal(0, 4_000, rows)).round(0)
    churn_probability = np.clip(0.55 - 0.09 * satisfaction - 0.015 * tenure, 0.03, 0.9)
    churned = (rng.uniform(0, 1, rows) < churn_probability).astype(int)
    frame = pd.DataFrame(
        {
            "employee_id": range(1, rows + 1),
            "department": department,
            "salary": salary,
            "tenure_years": tenure,
            "satisfaction": satisfaction,
            "churned": churned,
        }
    )
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    return buffer.getvalue().encode()


def configure_environment(key: str, model: str, backend: str, timeout: int) -> None:
    """Set before `src` is imported: settings are read once, at import."""
    os.environ.update(
        {
            "API_PROVIDER": "gemini",
            "GEMINI_API_KEY": key,
            "MODEL_NAME": model,
            # Left empty, the registry picks its own preferred worker model, so the run would test two models.
            "WORKER_MODEL_NAME": model,
            "DATA_MODE": "cloud-only",
            "EXECUTION_BACKEND": backend,
            "SANDBOX_ENABLED": "false",
            "EMBEDDINGS_FORCE_FALLBACK": "true",
            "AGENT_TURN_TIMEOUT": str(timeout),
            "WIZARD_CONFIG_DIR": tempfile.mkdtemp(prefix="wizard-live-"),
            "SKILLS_REGISTRY_API": "http://127.0.0.1:1",
            "OLLAMA_BASE_URL": "http://127.0.0.1:1",
        }
    )


def run_turn(websocket, message: str, approve: bool = False) -> tuple[list[dict], float]:
    """One turn. A mid-turn permission prompt (`approval_required` with an id) is answered
    like the UI would, so the turn is not left waiting for its consent timeout."""
    started = time.monotonic()
    websocket.send_json({"type": "message", "content": message, "mode": "auto"})
    frames: list[dict] = []
    while True:
        frame = websocket.receive_json()
        frames.append(frame)
        if frame.get("type") == "approval_required" and frame.get("id"):
            websocket.send_json(
                {
                    "type": "approval",
                    "approved": approve,
                    "id": frame["id"],
                    "tool": frame.get("tool", ""),
                    "content": frame.get("content", ""),
                }
            )
            continue
        if frame.get("type") in {"final", "error", "cancelled"} or (
            frame.get("type") == "approval_required" and not frame.get("id")
        ):
            return frames, time.monotonic() - started


def frame_text(frame: dict) -> str:
    """What a terminal frame says: `content` on an error frame, `response` on a final one."""
    return str(frame.get("response") or frame.get("content") or frame.get("message") or "").strip()


def is_quota_error(frames: list[dict]) -> bool:
    last = frames[-1]
    return last.get("type") == "error" and any(t in frame_text(last) for t in ("429", "RESOURCE_EXHAUSTED", "quota"))


def is_provider_outage(frames: list[dict]) -> bool:
    """The provider was unavailable (overloaded, or stopped streaming). Not something Wizard did."""
    last = frames[-1]
    text = frame_text(last).lower()
    return last.get("type") == "error" and any(
        t in text for t in ("503", "unavailable", "overloaded", "no streaming chunk", "timed out", "connection")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--backend", default="host", choices=["host", "inprocess", "docker"])
    parser.add_argument("--pace", type=float, default=20.0, help="seconds between turns (free-tier quota)")
    parser.add_argument("--timeout", type=int, default=240, help="per-turn agent timeout in seconds")
    parser.add_argument("--approve", action="store_true", help="grant mid-turn permission prompts (default: deny)")
    parser.add_argument("--only", default="", help="comma-separated turn numbers to run, e.g. 2,3,4 (default: all)")
    args = parser.parse_args()

    key = sys.stdin.read().strip()
    if not key:
        print("No key on stdin. Copy it, then:  pbpaste | python scripts/live_acceptance.py")
        return 2

    configure_environment(key, args.model, args.backend, args.timeout)
    sys.path.insert(0, str(REPO_ROOT / "backend"))

    from fastapi.testclient import TestClient

    from src.api.api import app
    from src.core.session import session_manager

    only = {int(n) for n in args.only.split(",") if n.strip()}
    failures: list[str] = []
    outages: list[int] = []
    with TestClient(app) as client:
        upload = client.post("/api/datasets?clean=false", files={"file": ("employees.csv", dataset_csv(), "text/csv")})
        upload.raise_for_status()
        session_id = upload.json()["session_id"]
        print(
            f"model={args.model} provider=gemini data_mode=cloud-only backend={args.backend} session={session_id[:8]}"
        )

        with client.websocket_connect(f"/ws/chat?session={session_id}") as websocket:
            websocket.receive_json()
            for index, (message, workflow, must_have, must_not) in enumerate(TURNS, start=1):
                if only and index not in only:
                    continue
                frames, elapsed = run_turn(websocket, message, args.approve)
                if is_quota_error(frames):
                    print("  (rate limited, waiting 65s and retrying once)")
                    time.sleep(65)
                    frames, elapsed = run_turn(websocket, message, args.approve)

                last = frames[-1]
                # Announced once, first, as its own frame, so it exists even for a turn that errors or stops at the plan gate.
                route = next((f for f in frames if f.get("type") == "route"), {})
                types = {f.get("type") for f in frames}
                problems = []
                if last.get("type") not in {"final", "approval_required"}:
                    problems.append(f"turn ended with {last.get('type')!r}")
                if route.get("workflow") != workflow:
                    problems.append(f"routed {route.get('workflow')!r}, expected {workflow!r}")
                if not must_have <= types:
                    problems.append(f"missing frames {sorted(must_have - types)}")
                if types & must_not:
                    problems.append(f"unexpected frames {sorted(types & must_not)}")

                outage = bool(problems) and is_provider_outage(frames)
                verdict = "PASS" if not problems else "PROVIDER" if outage else "FAIL"
                print(f"\n[{index}/{len(TURNS)}] {verdict}  {message[:70]!r}")
                shown = {k: route.get(k) for k in ("workflow", "complexity", "plan", "verify", "escalate")}
                print(f"  route={shown}  frames={len(frames)}  {elapsed:.1f}s")
                for asked in (f for f in frames if f.get("type") == "approval_required" and f.get("id")):
                    detail = {k: str(v)[:110] for k, v in asked.items() if k not in {"type", "id", "at"}}
                    print(f"  permission asked ({'granted' if args.approve else 'denied'}): {detail}")
                text = frame_text(last).replace("\n", " ")
                print(f"  reply: {text[:260]}")
                for problem in problems:
                    print(f"  problem: {problem}")
                if outage:
                    outages.append(index)
                else:
                    failures.extend(f"turn {index}: {problem}" for problem in problems)

                time.sleep(args.pace)

    session_manager.shutdown()
    ran = len(only) if only else len(TURNS)
    failed_turns = {failure.split(":")[0] for failure in failures}
    print(f"\n{ran - len(failed_turns) - len(outages)}/{ran} turns met their expectation", end="")
    print(f", {len(outages)} lost to the provider being unavailable (turns {outages})" if outages else "")
    return 1 if failures else 3 if outages else 0


if __name__ == "__main__":
    raise SystemExit(main())
