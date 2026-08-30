"""Comprehensive User Story Validation Script for Wizard.

Executes a complete 3-turn data analysis lifecycle against the running backend:
1. Ingests housing.csv dataset.
2. Turn 1: Dataset summary & data quality triage.
3. Turn 2: Distribution plotting & descriptive statistics.
4. Turn 3: Regression modeling & top price driver analysis.
"""

import asyncio
import json
import os
import sys
import websockets
import httpx

BACKEND_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/chat"
DATASET_PATH = "workspace/housing.csv"


async def run_user_story():
    print("=" * 70, flush=True)
    print("🧙‍♂️ WIZARD END-TO-END USER STORY VALIDATION", flush=True)
    print("=" * 70, flush=True)

    # 1. Health check
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{BACKEND_URL}/health")
        print(f"[*] Health Check: {res.status_code} -> {res.json()}", flush=True)
        assert res.status_code == 200, "Backend is not healthy"

    # 2. Ingest housing.csv
    print(f"\n[*] Ingesting dataset from {DATASET_PATH}...", flush=True)
    with open(DATASET_PATH, "rb") as f:
        file_bytes = f.read()

    async with httpx.AsyncClient(timeout=30.0) as client:
        files = {"file": ("housing.csv", file_bytes, "text/csv")}
        res = await client.post(f"{BACKEND_URL}/api/datasets?clean=true", files=files)
        print(f"[*] Ingestion Status: {res.status_code} -> {res.json()}", flush=True)
        assert res.status_code == 200, "Dataset ingestion failed"
        session_id = res.json()["session_id"]
        print(f"[*] Active Session ID: {session_id}", flush=True)

    # Connect WebSocket
    ws_uri = f"{WS_URL}?session={session_id}"
    print(f"\n[*] Connecting to WebSocket: {ws_uri}", flush=True)

    async with websockets.connect(ws_uri) as ws:
        # Wait for initial session frame
        init_frame = json.loads(await ws.recv())
        print(f"[*] Received Initial Frame: {init_frame.get('type')}", flush=True)

        turns = [
            {
                "name": "Turn 1: Exploration & Data Quality Triage",
                "instruction": "Summarise this housing dataset, check data quality, and compute summary statistics for price and area.",
            },
            {
                "name": "Turn 2: Distribution & Outlier Analysis",
                "instruction": "Plot the distribution of price and area, check for skewness and outliers, and describe what each distribution looks like.",
            },
            {
                "name": "Turn 3: Predictive Regression & Driver Analysis",
                "instruction": "Run a regression to predict house price from area, bedrooms, bathrooms, and stories. Report the R-squared and top price drivers.",
            },
        ]

        for i, turn in enumerate(turns, 1):
            print("\n" + "=" * 70, flush=True)
            print(f"🚀 EXECUTING {turn['name']}", flush=True)
            print(f"📝 Prompt: \"{turn['instruction']}\"", flush=True)
            print("=" * 70, flush=True)

            # Send prompt with content and mode
            await ws.send(json.dumps({
                "content": turn["instruction"],
                "mode": "auto"
            }))

            # Collect stream events
            code_generated = []
            execution_outputs = []
            answer_chunks = []
            reasoning_chunks = []
            turn_finished = False

            while not turn_finished:
                try:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=90.0)
                    event = json.loads(raw_msg)
                    etype = event.get("type")
                    content = event.get("content", "")

                    if etype == "status":
                        phase = event.get("phase", "")
                        print(f"  [Status] {content} (phase: {phase})", flush=True)
                    elif etype == "code_delta":
                        code_generated.append(content)
                    elif etype == "stdout":
                        execution_outputs.append(content)
                    elif etype == "reasoning_delta":
                        reasoning_chunks.append(content)
                    elif etype == "content_delta":
                        answer_chunks.append(content)
                    elif etype in ("final", "response"):
                        if event.get("response") and not answer_chunks:
                            answer_chunks.append(event["response"])
                        if event.get("code") and not code_generated:
                            code_generated.append(event["code"])
                        turn_finished = True
                    elif etype == "error":
                        print(f"  ❌ [Error] {content}", flush=True)
                        turn_finished = True
                    elif etype == "done":
                        turn_finished = True
                except asyncio.TimeoutError:
                    print("  ⚠️ [Timeout] Turn took longer than 90s", flush=True)
                    turn_finished = True

            full_code = "".join(code_generated).strip()
            full_obs = "".join(execution_outputs).strip()
            full_answer = "".join(answer_chunks).strip()
            full_reasoning = "".join(reasoning_chunks).strip()

            print("\n--- TURN SUMMARY ---", flush=True)
            print(f"Code Generated Length: {len(full_code)} chars", flush=True)
            if full_code:
                print("Code Preview:\n" + "\n".join(full_code.splitlines()[:8]) + "\n...", flush=True)
            print(f"Execution Output Length: {len(full_obs)} chars", flush=True)
            if full_obs:
                print("Observation Preview:\n" + "\n".join(full_obs.splitlines()[:6]) + "\n...", flush=True)
            print(f"Final Answer Length: {len(full_answer)} chars", flush=True)
            print("Answer:\n" + full_answer, flush=True)
            print("-" * 70, flush=True)

            # Verification assertions
            assert len(full_code) > 0 or len(full_answer) > 0, f"{turn['name']} produced neither code nor answer!"
            assert "were not computed" not in full_answer, f"{turn['name']} returned disclaimer: {full_answer}"
            print(f"✅ {turn['name']} PASSED!\n", flush=True)

    print("\n🎉 ALL USER STORIES COMPLETED AND VERIFIED SUCCESSFULLY!", flush=True)


if __name__ == "__main__":
    asyncio.run(run_user_story())
