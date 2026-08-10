"""Dump the FastAPI app's OpenAPI schema to a JSON file.

Run from `backend/`: `python scripts/generate_openapi.py`. The frontend's
`pnpm generate:api-types` reads the output to regenerate
`frontend/lib/api-types.generated.ts` via `openapi-typescript`.

Env vars are pinned *before* importing `src.api.api` (which builds the real
app, including its lifespan) so this never touches a real Docker daemon,
Ollama instance, or the developer's on-disk credentials -- the same
constraint `backend/tests/conftest.py` documents for imports of `src`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = BACKEND_ROOT / "openapi.json"

os.environ.setdefault("ENV", "test")
os.environ.setdefault("SANDBOX_ENABLED", "false")
os.environ.setdefault("EXECUTION_BACKEND", "inprocess")
os.environ.setdefault("HOST_SANDBOX", "off")
os.environ.setdefault("API_PROVIDER", "ollama")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="wizard-openapi-data-"))
os.environ.setdefault("WORKSPACE_DIR", tempfile.mkdtemp(prefix="wizard-openapi-ws-"))
os.environ.setdefault("WIZARD_CONFIG_DIR", tempfile.mkdtemp(prefix="wizard-openapi-config-"))
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("API_KEY", "")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:1")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://127.0.0.1:1")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:1")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")

sys.path.insert(0, str(BACKEND_ROOT))


def main() -> None:
    from src.api.api import app

    schema = app.openapi()
    OUTPUT_PATH.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
