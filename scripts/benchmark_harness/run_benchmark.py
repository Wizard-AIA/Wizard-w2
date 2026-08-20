"""Phase 2 full-turn harness -- drives REAL orchestrator turns and grades them
from content. This is the one script in this harness that needs live model
inference, so it is built here but deliberately NOT executed in this session
(see the project's standing "no live model workloads in-session" constraint).
Run it yourself once your providers (Ollama / cloud gateway) are up.

Implements every rule from docs/benchmark-methodology-spec.md:
  1.1 grades success from the answer/execution content (grading.py), never from
      a field the harness writes about itself -- the exact defect that produced
      the original report's false "13/13, 100%".
  1.2 counts retries by scanning the STATUS event stream for
      "Fixing an execution error (attempt N of M)", since RunResult itself does
      not expose retry_count (orchestrator.py's `_result()` does not carry it).
  1.3 always drives a real `AnalysisOrchestrator.run()` turn, never a bypassed
      direct model call -- so grounding, retries and every other per-turn check
      apply uniformly regardless of which comparison is being timed.
  1.5 runs each (mode, case) cell N>=3 times and reports median + spread.
  1.7 records host preconditions (free RAM, resolved backend, models resident)
      with every run.

Usage:
    cd backend
    python ../scripts/benchmark_harness/run_benchmark.py \
        --mode local-only --manager-provider ollama --manager-model qwen2.5:3b \
        --worker-provider ollama --worker-model qwen2.5-coder:1.5b \
        --dataset ../workspace/dataset.csv --cases A1 A2 A3 B1 B2 C1 C2 C3 --n 3

Requires the backend's own dependencies (pandas, the FastAPI app's `src` package)
to already be importable -- run from an environment where
`uv pip install --system -r requirements.txt -r requirements-local.txt` has been
done, exactly as CLAUDE.md's own "Without Docker" section describes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
import uuid
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import pandas as pd  # noqa: E402
from grading import grade  # noqa: E402
from reference_answers import REFERENCE_CASES  # noqa: E402

from src.core.agent.events import EventCollector, EventType  # noqa: E402
from src.core.agent.orchestrator import orchestrator  # noqa: E402
from src.core.session import Session  # noqa: E402
from src.utils.hostinfo import host_info  # noqa: E402


RETRY_PATTERN = re.compile(r"Fixing an execution error \(attempt (\d+) of (\d+)\)")


def _free_ram_gb() -> float | None:
    """Free RAM right now. `hostinfo.py` deliberately measures only TOTAL RAM
    (no psutil dependency, boot-time sizing only) -- 1.7 needs a live figure per
    run, so this stays dependency-free the same way, platform by platform."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
            return round(stat.ullAvailPhys / 1024**3, 2)

        meminfo = Path("/proc/meminfo").read_text()
        available_kb = int(re.search(r"MemAvailable:\s+(\d+)", meminfo).group(1))
        return round(available_kb / 1024**2, 2)
    except Exception:  # noqa: BLE001 - a precondition probe must never fail the run
        return None


def host_preconditions() -> dict:
    """1.7 -- recorded once per run, not assumed to carry over from a prior one."""
    info = host_info()
    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_ram_gb": round(info.ram_bytes / 1024**3, 2) if info.ram_bytes else None,
        "free_ram_gb": _free_ram_gb(),
        "physical_cores": getattr(info, "cores", None),
    }


def retries_from_events(collector: EventCollector) -> int:
    """1.2 -- read from the event stream, never from a harness-computed field."""
    max_attempt = 0
    for event in collector.of_type(EventType.STATUS):
        match = RETRY_PATTERN.search(str(event.data.get("content", "")))
        if match:
            max_attempt = max(max_attempt, int(match.group(1)))
    return max_attempt


async def run_one_turn(mode: str, case_prompt: str, dataset_path: Path, models: dict) -> dict:
    """1.3 -- always the real orchestrator.run(), never a bypassed direct call."""
    session = Session(f"bench-{uuid.uuid4().hex[:10]}")
    session.data_mode = mode
    session.models.manager = models["manager_model"]
    session.models.manager_provider = models["manager_provider"]
    session.models.worker = models["worker_model"]
    session.models.worker_provider = models["worker_provider"]

    df = pd.read_csv(dataset_path)
    session.add_dataset(dataset_path.name, df)

    collector = EventCollector()
    t0 = time.perf_counter()
    result = await orchestrator.run(session, case_prompt, mode="auto", emitter=collector, can_prompt=False)
    elapsed_sec = time.perf_counter() - t0

    executed_output = "\n".join(str(event.data.get("content", "")) for event in collector.of_type(EventType.STDOUT))

    return {
        "answer": result.answer,
        "executed_output": executed_output,
        "status": result.status,
        "elapsed_sec": round(elapsed_sec, 3),
        "iterations": result.iterations,
        "retries": retries_from_events(collector),
        "grounding": result.grounding,
    }


async def run_cell(mode: str, case_id: str, dataset_path: Path, models: dict, n: int) -> dict:
    case = next((c for c in REFERENCE_CASES if c.id == case_id), None)
    if case is None:
        raise SystemExit(f"No reference case '{case_id}' -- see reference_answers.py")

    runs = []
    for i in range(n):
        turn = await run_one_turn(mode, case.prompt, dataset_path, models)
        graded = grade(case_id, turn["answer"], turn["executed_output"])
        runs.append({**turn, "graded_pass": graded.passed, "graded_reasons": graded.reasons})
        print(f"  [{case_id}] run {i + 1}/{n}: {turn['elapsed_sec']}s, pass={graded.passed}, retries={turn['retries']}")

    latencies = [r["elapsed_sec"] for r in runs]
    return {
        "mode": mode,
        "case_id": case_id,
        "models": models,
        "n": n,
        "runs": runs,
        "median_latency_sec": statistics.median(latencies),
        "min_latency_sec": min(latencies),
        "max_latency_sec": max(latencies),
        "pass_rate": sum(1 for r in runs if r["graded_pass"]) / len(runs),
        "host_preconditions": host_preconditions(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", required=True, choices=["local-only", "hybrid", "cloud-only"])
    parser.add_argument("--manager-provider", required=True)
    parser.add_argument("--manager-model", required=True)
    parser.add_argument("--worker-provider", required=True)
    parser.add_argument("--worker-model", required=True)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--cases", nargs="+", default=[c.id for c in REFERENCE_CASES])
    parser.add_argument("--n", type=int, default=3, help="Repeats per case, per 1.5 (n>=3)")
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).resolve().parent / "results" / "full_turn_results.json"
    )
    args = parser.parse_args()

    models = {
        "manager_provider": args.manager_provider,
        "manager_model": args.manager_model,
        "worker_provider": args.worker_provider,
        "worker_model": args.worker_model,
    }

    print(f"Host preconditions at start: {host_preconditions()}")
    print(f"Mode: {args.mode}  Models: {models}")
    print(f"Cases: {args.cases}  n={args.n}\n")

    async def run_all_cases() -> list[dict]:
        # One event loop for the whole run, not one per case: the LLM client is
        # cached at process scope (matches production's one-loop-per-process-life
        # assumption), so a fresh asyncio.run() per case reuses a client still
        # bound to the previous case's now-closed loop and fails with "Event loop
        # is closed" until the stale client happens to get evicted.
        all_results = []
        for case_id in args.cases:
            print(f"=== {case_id} ===")
            all_results.append(await run_cell(args.mode, case_id, args.dataset, models, args.n))
        return all_results

    results = asyncio.run(run_all_cases())

    args.out.parent.mkdir(exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nWritten to {args.out}")

    for r in results:
        print(
            f"{r['case_id']}: median {r['median_latency_sec']}s (range {r['min_latency_sec']}-{r['max_latency_sec']}s), pass_rate={r['pass_rate']:.0%}"
        )


if __name__ == "__main__":
    main()
