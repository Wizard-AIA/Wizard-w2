"""Renders a cosmic-ray.toml for one CI matrix shard, from its file list.

The repo's own cosmic-ray.toml (module-path = ["backend/src"]) is the config
for a manual/local run over everything at once -- see
docs/analysis/mutation-testing.md. CI instead needs one config per shard,
scoped to the files plan_shards.py assigned it, which is what this renders.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


TEMPLATE = """\
[cosmic-ray]
module-path = [
{files}
]
test-command = "python -m pytest -x -q backend/tests"
timeout = {timeout}
excluded-modules = []

[cosmic-ray.distributor]
name = "local"
"""


def render(files: list[str], timeout: float) -> str:
    body = "\n".join(f'    "{f}",' for f in files)
    return TEMPLATE.format(files=body, timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files-json", required=True, help="JSON array of file paths for this shard.")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    files = json.loads(args.files_json)
    if not files:
        raise SystemExit("--files-json is empty; nothing to render")
    Path(args.out).write_text(render(files, args.timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
